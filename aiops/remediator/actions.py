"""What each action does to the cluster, as the `aiops-remediator` account.

An action is planned first (read the objects, decide the patch) and executed
second, so the plan can be shown for approval and dry-run against the API
server (`--dry-run=server`) before anything changes:

  rollback <service>
    - a canary in progress (the Rollout's current pods are not its stable ones):
      abort it. Argo Rollouts sends all traffic back to the stable version at once.
    - otherwise: put the service's ArgoCD Application back to the Helm parameters
      of its previous release. Releases here are parameter changes on that
      Application; a release that came from a Git commit can only be reverted
      in Git, and the plan says so instead of guessing.
  restart <component>
    - a service: the Rollout's restartAt (a rolling restart, no downtime)
    - redis: the Deployment's pod template annotation, as `kubectl rollout restart`
    - postgres: CloudNativePG's restart annotation on the Cluster
  scale_out <service>
    - raise the HPA's minReplicas by one, up to its maxReplicas. ArgoCD ignores
      that field (gitops/apps workload.yaml), so self-heal does not undo it.

There is no delete, no exec, no free-form kubectl. This module and the fake in
the tests are the only places that build kubectl arguments.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from detector.alerts import DATA_STORES, SERVICES
from recorder.kube import Kube, KubeError

REMEDIATOR_IDENTITY = "system:serviceaccount:aiops:aiops-remediator"
WORKLOAD_NS = "shopsearch"
ARGOCD_NS = "argocd"
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"


class PlanError(ValueError):
    """The action cannot be carried out as asked; a human has to decide."""


@dataclass
class Step:
    describe: str
    kind: str
    name: str
    namespace: str
    patch: Any
    patch_type: str = "merge"           # merge | json
    subresource: str | None = None

    def args(self, dry_run: bool) -> list[str]:
        a = ["-n", self.namespace, "patch", self.kind, self.name, "--type", self.patch_type,
             "-p", json.dumps(self.patch, separators=(",", ":"))]
        if self.subresource:
            a.append(f"--subresource={self.subresource}")
        if dry_run:
            a.append("--dry-run=server")
        return a


@dataclass
class Plan:
    action: str
    target: str
    how: str                            # abort-canary | argocd-previous-release | restart | scale-out
    steps: list[Step]
    before: dict[str, Any] = field(default_factory=dict)  # what it was, for the audit log and a human undo

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "commands": [" ".join(["kubectl", *s.args(False)]) for s in self.steps]}


class Cluster:
    """kubectl as the remediator. `run` is replaceable for the tests."""

    def __init__(self, context: str = "kind-incidentlens", kubectl: str = "kubectl",
                 identity: str | None = REMEDIATOR_IDENTITY, run: Callable[..., str] | None = None):
        self.kube = Kube(context, kubectl, identity)
        self._run = run or (lambda *args: self.kube.kubectl(*args))

    def get(self, kind: str, name: str, namespace: str) -> dict[str, Any]:
        return json.loads(self._run("get", kind, name, "-n", namespace, "-o", "json"))

    def apply(self, step: Step, dry_run: bool) -> str:
        return self._run(*step.args(dry_run)).strip()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canary_in_progress(rollout: dict[str, Any]) -> bool:
    st = rollout.get("status", {})
    current, stable = st.get("currentPodHash"), st.get("stableRS")
    return bool(current and stable and current != stable and not st.get("abort"))


def plan_rollback(c: Cluster, service: str) -> Plan:
    if service not in SERVICES:
        raise PlanError(f"{service} has no rollout to roll back")
    rollout = c.get("rollouts.argoproj.io", service, WORKLOAD_NS)
    st = rollout.get("status", {})
    if canary_in_progress(rollout):
        return Plan("rollback", service, "abort-canary",
                    [Step(f"abort the {service} canary: traffic back to the stable pods {st.get('stableRS')}",
                          "rollouts.argoproj.io", service, WORKLOAD_NS, {"status": {"abort": True}}, subresource="status")],
                    {"current_hash": st.get("currentPodHash"), "stable_hash": st.get("stableRS")})
    app = c.get("applications.argoproj.io", service, ARGOCD_NS)
    history = app.get("status", {}).get("history") or []
    if len(history) < 2:
        raise PlanError(f"ArgoCD has no earlier release of {service} to go back to")
    current, previous = history[-1], history[-2]
    if current.get("revision") != previous.get("revision"):
        raise PlanError(f"the last {service} release came from Git ({str(previous.get('revision'))[:8]} -> "
                        f"{str(current.get('revision'))[:8]}): revert that commit instead")
    params_now = (app.get("spec", {}).get("source", {}).get("helm") or {}).get("parameters") or []
    params_before = ((previous.get("source") or {}).get("helm") or {}).get("parameters") or []
    if params_now == params_before:
        raise PlanError(f"{service}'s Application already has the parameters of its previous release")
    if params_before:
        step = Step(f"set {service}'s Application back to release {previous.get('id')}'s parameters",
                    "applications.argoproj.io", service, ARGOCD_NS, {"spec": {"source": {"helm": {"parameters": params_before}}}})
    else:
        step = Step(f"remove the parameters release {current.get('id')} added to {service}'s Application",
                    "applications.argoproj.io", service, ARGOCD_NS,
                    [{"op": "remove", "path": "/spec/source/helm/parameters"}], patch_type="json")
    return Plan("rollback", service, "argocd-previous-release", [step],
                {"parameters": params_now, "history_id": current.get("id"), "back_to": previous.get("id")})


def plan_restart(c: Cluster, component: str) -> Plan:
    now = _now()
    if component in SERVICES:
        c.get("rollouts.argoproj.io", component, WORKLOAD_NS)  # exists, and we may read it
        step = Step(f"rolling restart of {component}", "rollouts.argoproj.io", component, WORKLOAD_NS,
                    {"spec": {"restartAt": now}})
    elif component == "redis":
        c.get("deployments.apps", "redis", WORKLOAD_NS)
        step = Step("restart redis (its pod is replaced; it comes back empty and the indexer reindexes)",
                    "deployments.apps", "redis", WORKLOAD_NS,
                    {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: now}}}}})
    elif component == "postgres":
        c.get("clusters.postgresql.cnpg.io", "postgres", WORKLOAD_NS)
        step = Step("restart the PostgreSQL instances (CloudNativePG restarts them one by one)",
                    "clusters.postgresql.cnpg.io", "postgres", WORKLOAD_NS,
                    {"metadata": {"annotations": {RESTART_ANNOTATION: now}}})
    else:
        raise PlanError(f"{component} cannot be restarted (one of {', '.join(SERVICES + DATA_STORES)})")
    return Plan("restart", component, "restart", [step], {"at": now})


def plan_scale_out(c: Cluster, service: str) -> Plan:
    if service not in SERVICES:
        raise PlanError(f"{service} has no autoscaler to scale out")
    hpa = c.get("horizontalpodautoscalers.autoscaling", service, WORKLOAD_NS)
    lo, hi = hpa["spec"].get("minReplicas", 1), hpa["spec"]["maxReplicas"]
    current = hpa.get("status", {}).get("currentReplicas", lo)
    new = min(hi, max(lo, current) + 1)
    if new <= lo:
        raise PlanError(f"{service} already runs at its maximum of {hi} replicas")
    return Plan("scale_out", service, "scale-out",
                [Step(f"raise {service}'s minimum from {lo} to {new} replicas (maximum {hi})",
                      "horizontalpodautoscalers.autoscaling", service, WORKLOAD_NS, {"spec": {"minReplicas": new}})],
                {"min_replicas": lo, "max_replicas": hi, "current_replicas": current})


PLANNERS = {"rollback": plan_rollback, "restart": plan_restart, "scale_out": plan_scale_out}


def plan(c: Cluster, action: str, target: str | None) -> Plan:
    if action not in PLANNERS:
        raise PlanError(f"{action} changes nothing in the cluster")
    if not target:
        raise PlanError(f"{action} needs a target")
    try:
        return PLANNERS[action](c, target)
    except KubeError as exc:
        raise PlanError(f"could not read what {action} {target} needs: {exc}") from exc


def execute(c: Cluster, p: Plan, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Apply the plan's steps in order; stop at the first failure."""
    results = []
    for step in p.steps:
        try:
            out = c.apply(step, dry_run)
            results.append({"step": step.describe, "ok": True, "output": out[:500], "dry_run": dry_run})
        except KubeError as exc:
            results.append({"step": step.describe, "ok": False, "output": str(exc)[:500], "dry_run": dry_run})
            break
    return results
