"""One injector per way of causing a fault (the `injector` field in the catalog).

Each has start(), which causes the fault and returns details for the label, and
stop(), which ends it. stop() must work after a start() that failed halfway,
because the runner always calls it.
"""

from __future__ import annotations

import math
import secrets
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

import yaml

from .catalog import Fault, render
from .cluster import Cluster, ClusterError

CHAOS_NAMESPACE = "chaos"
MANAGED = "app.kubernetes.io/managed-by=chaos-runner"
CHAOS_KINDS = "stresschaos,networkchaos,podchaos,iochaos,dnschaos"
FILL_TABLE = "catalog_audit_log"
# Below the gateway's 600 requests a minute per client, with room to spare.
LOAD_RPS_PER_POD = 8.0


@dataclass
class Injection:
    name: str  # object names and the label's id, e.g. "i007-f05"
    target: str
    intensity: str | None
    duration_s: int
    variables: dict[str, str]
    fault: Fault | None = None  # None for a benign release
    env: dict[str, str] = field(default_factory=dict)


class Injector(Protocol):
    def start(self, inj: Injection) -> dict[str, Any]: ...
    def stop(self, inj: Injection, details: dict[str, Any]) -> None: ...


def new_tag() -> str:
    """Looks like the short commit hash a release tag usually is."""
    return secrets.token_hex(4)[:7]


class _Release:
    """A release made the way ArgoCD makes them: change the service's
    Application, let the Rollout start the new version, then promote it fully,
    as a hotfix that skips the canary would be. Week 3 already shows the canary
    catching bad releases; the dataset needs releases that got past it."""

    def __init__(self, cluster: Cluster):
        self.c = cluster

    def _deploy(self, service: str, params: dict[str, str], wait_promoted: bool) -> dict[str, Any]:
        before = self.c.rollout(service).get("currentPodHash")
        self.c.hold_root()
        self.c.set_parameters(service, params)
        pod_hash = self.c.wait_new_hash(service, before)
        self.c.promote_full(service)
        promoted = self.c.wait_stable(service, pod_hash) if wait_promoted else None
        return {"pod_hash": pod_hash, "previous_hash": before, "promoted": promoted}

    def _revert(self, service: str, details: dict[str, Any]) -> None:
        try:
            self.c.clear_parameters(service)
            if details.get("pod_hash"):
                back = self.c.wait_new_hash(service, details["pod_hash"])
                # An aborted release (F01) is already back on its stable pods.
                if self.c.rollout(service).get("stableRS") != back:
                    self.c.promote_full(service)
                    self.c.wait_stable(service, back)
        finally:
            self.c.release_root()


class ConfigRelease(_Release):
    """F01: a typo in an environment variable, committed to the service's values."""

    def start(self, inj: Injection) -> dict[str, Any]:
        params = {f"env.{k}": v for k, v in inj.env.items()}
        return {"release": "config", "params": params, **self._deploy(inj.target, params, wait_promoted=False)}

    def stop(self, inj: Injection, details: dict[str, Any]) -> None:
        self._revert(inj.target, details)


class ImageRelease(_Release):
    """A new image tag: the service's normal image with the fault's variables
    baked in, so the release history shows only a tag change. Benign releases
    use the same path with a harmless variable."""

    def start(self, inj: Injection) -> dict[str, Any]:
        tag = new_tag()
        image = self.c.build_release_image(inj.target, tag, inj.env)
        details: dict[str, Any] = {"release": "image", "image": image, "tag": tag}
        details.update(self._deploy(inj.target, {"image.tag": tag}, wait_promoted=True))
        return details

    def stop(self, inj: Injection, details: dict[str, Any]) -> None:
        try:
            self._revert(inj.target, details)
        finally:
            if details.get("image"):
                self.c.remove_image(details["image"])


class ChaosMesh:
    """An experiment from chaos/experiments, created in the `chaos` namespace.
    The experiment's own `duration` ends it even if the runner dies."""

    def __init__(self, cluster: Cluster):
        self.c = cluster

    def start(self, inj: Injection) -> dict[str, Any]:
        assert inj.fault is not None
        manifest = render(inj.fault.experiment_text(), {**inj.variables, "name": inj.name})
        kind = yaml.safe_load(manifest)["kind"]
        self.c.ensure_namespace(CHAOS_NAMESPACE)
        self.c.apply(manifest, CHAOS_NAMESPACE)
        return {"object": f"{kind.lower()}/{inj.name}", "namespace": CHAOS_NAMESPACE}

    def stop(self, inj: Injection, details: dict[str, Any]) -> None:
        if "object" in details:
            self.c.delete(*details["object"].split("/", 1), CHAOS_NAMESPACE)


class Load:
    """F06: extra k6 pods sending `intensity - 1` times the current gateway rate."""

    def __init__(self, cluster: Cluster):
        self.c = cluster

    def start(self, inj: Injection) -> dict[str, Any]:
        assert inj.fault is not None and inj.intensity is not None
        baseline = self.c.prom_scalar(
            'sum(rate(http_server_request_duration_seconds_count{job="shopsearch/gateway"}[10m]))', default=4.0)
        baseline = max(baseline, 1.0)
        extra = (float(inj.intensity) - 1) * baseline
        pods = max(1, math.ceil(extra / LOAD_RPS_PER_POD))
        rate_per_minute = max(1, round(extra / pods * 60))
        manifest = render(inj.fault.experiment_text(), {
            **inj.variables, "name": inj.name, "pods": pods, "rate_per_minute": rate_per_minute})
        self.c.ensure_namespace(CHAOS_NAMESPACE)
        self.c.apply(manifest, CHAOS_NAMESPACE)
        return {"job": inj.name, "baseline_rps": round(baseline, 2), "extra_rps": round(extra, 2), "pods": pods}

    def stop(self, inj: Injection, details: dict[str, Any]) -> None:
        self.c.delete("job", inj.name, CHAOS_NAMESPACE)
        self.c.delete("configmap", inj.name, CHAOS_NAMESPACE)


class SqlFill:
    """F13: a table in the catalog database that grows by `intensity` MB a
    minute, the way an audit log nobody rotates does.

    On kind the volume is a host directory (local-path), whose size is not
    enforced, so the database never actually runs out of space: the fault is
    the growth, which is what a disk-filling alert predicts from. On EBS or OCI
    block volumes the same growth would end in a full disk."""

    STEP_S = 5
    ROW_BYTES = 1100  # 992 characters of payload, kept inline (under the 2 kB TOAST threshold), plus overhead

    def __init__(self, cluster: Cluster):
        self.c = cluster
        self._procs: dict[str, subprocess.Popen] = {}

    def start(self, inj: Injection) -> dict[str, Any]:
        assert inj.intensity is not None
        mb_per_min = float(inj.intensity)
        rows = max(1, int(mb_per_min * 1024 * 1024 / (60 / self.STEP_S) / self.ROW_BYTES))
        steps = max(1, inj.duration_s // self.STEP_S)
        self.c.psql(f"CREATE TABLE IF NOT EXISTS {FILL_TABLE} (at timestamptz NOT NULL DEFAULT now(), payload text NOT NULL)")
        loop = (
            f"DO $$ BEGIN FOR i IN 1..{steps} LOOP "
            f"INSERT INTO {FILL_TABLE} (payload) SELECT repeat(md5(random()::text), 31) FROM generate_series(1, {rows}); "
            f"COMMIT; PERFORM pg_sleep({self.STEP_S}); END LOOP; END $$;"
        )
        self._procs[inj.name] = self.c.psql_background(loop)
        return {"table": FILL_TABLE, "mb_per_minute": mb_per_min, "rows_per_step": rows, "step_s": self.STEP_S}

    def stop(self, inj: Injection, details: dict[str, Any]) -> None:
        proc = self._procs.pop(inj.name, None)
        if proc is not None:
            proc.terminate()
        self.c.psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE pid <> pg_backend_pid() AND query LIKE '%{FILL_TABLE}%'")
        self.c.psql(f"DROP TABLE IF EXISTS {FILL_TABLE}")


class NodeStop:
    """F14: the kind worker's container is stopped, like an instance that dies,
    and started again when the fault ends. Pods on it are lost until it is back,
    or until Kubernetes evicts them after 5 minutes."""

    def __init__(self, cluster: Cluster):
        self.c = cluster

    def start(self, inj: Injection) -> dict[str, Any]:
        self.c.stop_node(inj.target)
        return {"node": self.c.node_container(inj.target)}

    def stop(self, inj: Injection, details: dict[str, Any]) -> None:
        self.c.start_node(inj.target)


def injector_for(kind: str, cluster: Cluster) -> Injector:
    classes = {
        "config-release": ConfigRelease,
        "image-release": ImageRelease,
        "chaos-mesh": ChaosMesh,
        "load": Load,
        "sql-fill": SqlFill,
        "node-stop": NodeStop,
    }
    try:
        return classes[kind](cluster)
    except KeyError:
        raise ClusterError(f"no injector {kind!r}") from None


def cleanup(cluster: Cluster, services: list[str], worker: str = "worker") -> list[str]:
    """Undo whatever a crashed run may have left behind. Safe to run any time."""
    done = []
    cluster.kubectl("delete", f"{CHAOS_KINDS},job,configmap", "-n", CHAOS_NAMESPACE, "-l", MANAGED,
                    "--ignore-not-found", check=False, timeout=300)
    done.append("deleted chaos objects")
    for s in services:
        cluster.clear_parameters(s)
    cluster.release_root()
    done.append("cleared release parameters, root app syncs again")
    try:
        cluster.start_node(worker)
        done.append(f"started node {worker}")
    except ClusterError:
        pass
    try:
        cluster.psql(f"DROP TABLE IF EXISTS {FILL_TABLE}")
        done.append(f"dropped {FILL_TABLE}")
    except ClusterError:
        pass
    return done
