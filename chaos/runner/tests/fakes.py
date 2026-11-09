"""A cluster that records what it is asked to do, and a clock that sleep() moves."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import yaml

from chaos_runner.cluster import Cluster, ClusterError


class FakeClock:
    def __init__(self, start: datetime = datetime(2026, 12, 7, 20, 0, tzinfo=timezone.utc)):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeCluster(Cluster):
    """Releases get a new pod hash as soon as parameters change; promote_full
    makes it stable. Prometheus answers come from `symptom`, a function of the
    query and the fake time."""

    def __init__(self, clock: FakeClock | None = None):
        super().__init__(log=lambda _: None)
        self.clock = clock or FakeClock()
        self.calls: list[tuple] = []
        self.applied: list[dict] = []
        self.hashes: dict[str, dict[str, str]] = {}
        self.params: dict[str, dict[str, str]] = {}
        self.root_held = False
        self.symptom = lambda query, now: False
        self.healthy = lambda now: True
        self.gateway_rps = 6.0
        self.nodes_down: set[str] = set()
        self.fail_on: str | None = None
        self._n = 0

    def _record(self, *call):
        self.calls.append(call)
        if self.fail_on and call[0] == self.fail_on:
            raise ClusterError(f"{call[0]} failed")

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # objects
    def run(self, args, *, input=None, check=True, timeout=180):
        self._record("run", tuple(args))
        return ""

    def ensure_namespace(self, name):
        self._record("ensure_namespace", name)

    def apply(self, manifest, namespace=None):
        self._record("apply", namespace)
        self.applied.extend(d for d in yaml.safe_load_all(manifest) if d)

    def delete(self, kind, name, namespace, wait=True):
        self._record("delete", kind, name, namespace)

    def kubectl(self, *args, input=None, check=True, timeout=180):
        self._record("kubectl", args)
        return ""

    # observations
    def prom(self, query):
        self._record("prom", query)
        if "job=\"shopsearch/gateway\"}[10m]" in query and "sum(rate(" in query and ">" not in query:
            return [{"value": [0, str(self.gateway_rps)]}]
        return [{"value": [0, "1"]}] if self.symptom(query, self.clock()) else []

    def search_works(self):
        return self.healthy(self.clock())

    def unready_pods(self, namespace="shopsearch"):
        return [] if self.healthy(self.clock()) else ["search-api-abc"]

    # ArgoCD and rollouts
    def hold_root(self):
        self._record("hold_root")
        self.root_held = True

    def release_root(self):
        self._record("release_root")
        self.root_held = False

    def _status(self, app):
        return self.hashes.setdefault(app, {"currentPodHash": "stable0", "stableRS": "stable0"})

    def set_parameters(self, app, params):
        self._record("set_parameters", app, dict(params))
        self.params[app] = dict(params)
        self._n += 1
        self._status(app)["currentPodHash"] = f"h{self._n}"

    def clear_parameters(self, app):
        self._record("clear_parameters", app)
        if self.params.pop(app, None) is not None:
            status = self._status(app)
            status["currentPodHash"] = status.get("previous", "stable0")

    def rollout(self, name, namespace="shopsearch"):
        return dict(self._status(name))

    def wait_new_hash(self, name, old, timeout=240):
        current = self._status(name)["currentPodHash"]
        if current == old:
            raise ClusterError(f"rollout {name} did not start a new release")
        return current

    def wait_stable(self, name, pod_hash, timeout=300):
        return self._status(name)["stableRS"] == pod_hash

    def promote_full(self, name, namespace="shopsearch"):
        self._record("promote_full", name)
        status = self._status(name)
        status["previous"] = status["stableRS"]
        status["stableRS"] = status["currentPodHash"]

    def restart_rollout(self, name, namespace="shopsearch"):
        self._record("restart_rollout", name)

    # images, nodes, postgres
    def build_release_image(self, service, tag, env):
        self._record("build_release_image", service, tag, dict(env))
        return f"incidentlens/{service}:{tag}"

    def remove_image(self, image):
        self._record("remove_image", image)

    def stop_node(self, node):
        self._record("stop_node", node)
        self.nodes_down.add(node)

    def start_node(self, node):
        self._record("start_node", node)
        self.nodes_down.discard(node)

    def node_ready(self, node):
        return node not in self.nodes_down

    def postgres_primary(self, namespace="shopsearch"):
        return "postgres-1"

    def psql(self, sql, database="catalog"):
        self._record("psql", sql)
        return ""

    def psql_background(self, sql, database="catalog"):
        self._record("psql_background", sql)

        class Proc:
            terminated = False

            def terminate(self):
                Proc.terminated = True

        return Proc()
