"""Everything the recorder reads, through kubectl as the `aiops` service account.

Telemetry backends are reached through the API server's service proxy, as in
scripts/obs-test.sh, so nothing needs a port-forward and RBAC decides what is
readable (gitops/aiops/templates/rbac.yaml). This is the only module with side
effects; the tests replace it with a fake.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
from typing import Any

AIOPS_IDENTITY = "system:serviceaccount:aiops:aiops"
PROMETHEUS = "/api/v1/namespaces/monitoring/services/http:kube-prometheus-stack-prometheus:9090/proxy"
LOKI = "/api/v1/namespaces/monitoring/services/http:loki:3100/proxy"
TEMPO = "/api/v1/namespaces/monitoring/services/http:tempo:3200/proxy"


class KubeError(RuntimeError):
    pass


def _qs(params: dict[str, Any]) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, quote_via=urllib.parse.quote)


class Kube:
    def __init__(self, context: str = "kind-incidentlens", kubectl: str = "kubectl",
                 identity: str | None = AIOPS_IDENTITY):
        self.context = context
        self.kubectl_bin = kubectl
        self.identity = identity or None

    def kubectl(self, *args: str, timeout: int = 60) -> str:
        cmd = [self.kubectl_bin, "--context", self.context]
        if self.identity:
            cmd.append(f"--as={self.identity}")
        try:
            p = subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise KubeError(f"timed out after {timeout}s: kubectl {' '.join(args[:3])}") from None
        if p.returncode != 0:
            raise KubeError(f"kubectl {' '.join(args[:3])} failed: {p.stderr.strip()[:500]}")
        return p.stdout

    def raw(self, path: str, timeout: int = 60) -> Any:
        return json.loads(self.kubectl("get", "--raw", path, timeout=timeout))

    def get(self, kind: str, namespace: str) -> list[dict[str, Any]]:
        return json.loads(self.kubectl("get", kind, "-n", namespace, "-o", "json"))["items"]

    # --- backends -----------------------------------------------------------------
    def prom_range(self, query: str, start: float, end: float, step: float) -> list[dict[str, Any]]:
        body = self.raw(f"{PROMETHEUS}/api/v1/query_range?{_qs({'query': query, 'start': start, 'end': end, 'step': step})}")
        if body.get("status") != "success":
            raise KubeError(f"Prometheus: {body.get('error', body)}")
        return body["data"]["result"]

    def loki_range(self, query: str, start: float, end: float, *, limit: int | None = None,
                   step: float | None = None) -> dict[str, Any]:
        params = {"query": query, "start": int(start * 1e9), "end": int(end * 1e9), "limit": limit,
                  "step": step, "direction": "forward"}
        body = self.raw(f"{LOKI}/loki/api/v1/query_range?{_qs(params)}")
        if body.get("status") != "success":
            raise KubeError(f"Loki: {body}")
        return body["data"]

    def tempo_search(self, traceql: str, start: float, end: float, limit: int) -> list[dict[str, Any]]:
        body = self.raw(f"{TEMPO}/api/search?{_qs({'q': traceql, 'start': int(start), 'end': int(end), 'limit': limit})}")
        return body.get("traces") or []

    def tempo_trace(self, trace_id: str) -> dict[str, Any]:
        return self.raw(f"{TEMPO}/api/traces/{trace_id}")
