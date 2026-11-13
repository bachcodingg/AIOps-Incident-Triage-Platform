"""Everything chaos-runner does to a cluster, through kubectl, docker and kind.

This is the only module with side effects, so the injectors and the runner can
be tested against a fake that records the calls (tests/fakes.py).
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Sequence
from typing import Any

PROMETHEUS = "monitoring/services/http:kube-prometheus-stack-prometheus:9090"
GATEWAY = "shopsearch/services/http:gateway:8080"
ROOT_AUTOMATION = {"prune": True, "selfHeal": True}


class ClusterError(RuntimeError):
    pass


class Cluster:
    def __init__(
        self,
        context: str = "kind-incidentlens",
        kubectl: str = "kubectl",
        docker: str = "docker",
        kind: str = "kind",
        kind_cluster: str = "incidentlens",
        base_tag: str = "dev",
        log: Callable[[str], None] = print,
    ):
        self.context = context
        self.kubectl_bin = kubectl
        self.docker_bin = docker
        self.kind_bin = kind
        self.kind_cluster = kind_cluster
        self.base_tag = base_tag
        self.log = log

    # --- plumbing -------------------------------------------------------------
    def run(self, args: Sequence[str], *, input: str | None = None, check: bool = True, timeout: int = 180) -> str:
        try:
            p = subprocess.run(list(args), input=input, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ClusterError(f"timed out after {timeout}s: {' '.join(args)}") from None
        if check and p.returncode != 0:
            raise ClusterError(f"{' '.join(args[:6])} ... failed ({p.returncode}): {p.stderr.strip()[:2000]}")
        return p.stdout

    def kubectl(self, *args: str, input: str | None = None, check: bool = True, timeout: int = 180) -> str:
        return self.run([self.kubectl_bin, "--context", self.context, *args], input=input, check=check, timeout=timeout)

    def get_json(self, *args: str) -> dict[str, Any]:
        return json.loads(self.kubectl("get", *args, "-o", "json"))

    def raw(self, path: str, timeout: int = 30) -> str:
        return self.kubectl("get", "--raw", path, timeout=timeout)

    # --- objects --------------------------------------------------------------
    def ensure_namespace(self, name: str) -> None:
        manifest = self.kubectl("create", "namespace", name, "--dry-run=client", "-o", "yaml")
        self.kubectl("apply", "-f", "-", input=manifest)

    def apply(self, manifest: str, namespace: str | None = None) -> None:
        self.kubectl("apply", *(["-n", namespace] if namespace else []), "-f", "-", input=manifest)

    def delete(self, kind: str, name: str, namespace: str, wait: bool = True) -> None:
        self.kubectl("delete", kind, name, "-n", namespace, "--ignore-not-found", f"--wait={str(wait).lower()}", timeout=300)

    def delete_all(self, kinds: str, namespace: str) -> None:
        self.kubectl("delete", kinds, "--all", "-n", namespace, "--ignore-not-found", timeout=300)

    # --- what the cluster looks like --------------------------------------------
    def prom(self, query: str) -> list[dict[str, Any]]:
        body = self.raw(f"/api/v1/namespaces/{PROMETHEUS}/proxy/api/v1/query?query={urllib.parse.quote(query, safe='')}")
        data = json.loads(body)
        if data.get("status") != "success":
            raise ClusterError(f"Prometheus query failed: {data}")
        return data["data"]["result"]

    def prom_true(self, query: str) -> bool:
        return bool(self.prom(query))

    def prom_scalar(self, query: str, default: float = 0.0) -> float:
        result = self.prom(query)
        return float(result[0]["value"][1]) if result else default

    def search_works(self) -> bool:
        """A search through the gateway returns results: users are served, and
        the search index is there (it is not after Redis came back empty)."""
        try:
            body = json.loads(self.raw(f"/api/v1/namespaces/{GATEWAY}/proxy/api/search?q=laptop", timeout=15))
        except (ClusterError, json.JSONDecodeError):
            return False
        return bool(body.get("results"))

    def unready_pods(self, namespace: str = "shopsearch") -> list[str]:
        pods = self.get_json("pods", "-n", namespace)["items"]
        bad = []
        for p in pods:
            if p["status"].get("phase") == "Succeeded":
                continue
            ready = any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
            if not ready:
                bad.append(p["metadata"]["name"])
        return bad

    # --- ArgoCD: releases are made on a service's Application ------------------
    def hold_root(self) -> None:
        """Stop the root app from putting the service Applications back while a
        release is being made on one of them (as scripts/canary-test.sh does)."""
        self.kubectl("-n", "argocd", "patch", "application", "root", "--type", "json",
                     "-p", '[{"op":"remove","path":"/spec/syncPolicy/automated"}]', check=False)

    def release_root(self) -> None:
        patch = json.dumps({"spec": {"syncPolicy": {"automated": ROOT_AUTOMATION}}})
        self.kubectl("-n", "argocd", "patch", "application", "root", "--type", "merge", "-p", patch)

    def set_parameters(self, app: str, params: dict[str, str]) -> None:
        items = [{"name": k, "value": v} for k, v in params.items()]
        patch = json.dumps({"spec": {"source": {"helm": {"parameters": items}}}})
        self.kubectl("-n", "argocd", "patch", "application", app, "--type", "merge", "-p", patch)

    def clear_parameters(self, app: str) -> None:
        self.kubectl("-n", "argocd", "patch", "application", app, "--type", "json",
                     "-p", '[{"op":"remove","path":"/spec/source/helm/parameters"}]', check=False)

    # --- Argo Rollouts ----------------------------------------------------------
    def rollout(self, name: str, namespace: str = "shopsearch") -> dict[str, Any]:
        return self.get_json("rollout", name, "-n", namespace).get("status", {})

    def wait_new_hash(self, name: str, old: str | None, timeout: int = 240) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.rollout(name).get("currentPodHash")
            if current and current != old:
                return current
            time.sleep(3)
        raise ClusterError(f"rollout {name} did not start a new release within {timeout}s")

    def wait_stable(self, name: str, pod_hash: str, timeout: int = 300) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.rollout(name).get("stableRS") == pod_hash:
                return True
            time.sleep(5)
        return False

    def promote_full(self, name: str, namespace: str = "shopsearch") -> None:
        """`kubectl argo rollouts promote --full`: skip the remaining canary steps."""
        self.kubectl("-n", namespace, "patch", "rollout", name, "--subresource=status", "--type", "merge",
                     "-p", '{"status":{"promoteFull":true}}')

    def restart_rollout(self, name: str, namespace: str = "shopsearch") -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.kubectl("-n", namespace, "patch", "rollout", name, "--type", "merge",
                     "-p", json.dumps({"spec": {"restartAt": now}}))

    # --- images (kind: built on this machine, loaded into the nodes) ------------
    def build_release_image(self, service: str, tag: str, env: dict[str, str]) -> str:
        image = f"incidentlens/{service}:{tag}"
        lines = [f"FROM incidentlens/{service}:{self.base_tag}"]
        lines += [f"ENV {k}={json.dumps(v)}" for k, v in env.items()]
        self.run([self.docker_bin, "build", "-q", "-t", image, "-"], input="\n".join(lines) + "\n", timeout=300)
        self.run([self.kind_bin, "load", "docker-image", "--name", self.kind_cluster, image], timeout=600)
        return image

    def remove_image(self, image: str) -> None:
        self.run([self.docker_bin, "image", "rm", "-f", image], check=False)

    # --- nodes ------------------------------------------------------------------
    def node_container(self, node: str) -> str:
        return f"{self.kind_cluster}-{node}"

    def stop_node(self, node: str) -> None:
        self.run([self.docker_bin, "stop", "-t", "0", self.node_container(node)])

    def start_node(self, node: str) -> None:
        self.run([self.docker_bin, "start", self.node_container(node)])

    def node_ready(self, node: str) -> bool:
        out = self.kubectl("get", "node", self.node_container(node), "-o",
                           'jsonpath={.status.conditions[?(@.type=="Ready")].status}', check=False)
        return out.strip() == "True"

    # --- PostgreSQL (CloudNativePG) ---------------------------------------------
    def postgres_primary(self, namespace: str = "shopsearch") -> str:
        for selector in ("cnpg.io/cluster=postgres,cnpg.io/instanceRole=primary", "cnpg.io/cluster=postgres,role=primary"):
            out = self.kubectl("get", "pods", "-n", namespace, "-l", selector, "-o", "name").split()
            if out:
                return out[0].removeprefix("pod/")
        raise ClusterError("no PostgreSQL primary pod found")

    def psql_args(self, sql: str, database: str = "catalog", namespace: str = "shopsearch") -> list[str]:
        return [self.kubectl_bin, "--context", self.context, "-n", namespace, "exec", self.postgres_primary(namespace),
                "-c", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", "-qAt", "-d", database, "-c", sql]

    def psql(self, sql: str, database: str = "catalog") -> str:
        return self.run(self.psql_args(sql, database), timeout=120)

    def psql_background(self, sql: str, database: str = "catalog") -> subprocess.Popen:
        return subprocess.Popen(self.psql_args(sql, database), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
