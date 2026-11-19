import copy
import json

import pytest
from fastapi.testclient import TestClient

from loop.api import create_app, grafana_links
from loop.live import LiveSource, Loop, _same
from recorder.kube import KubeError
from recorder.record import iso, parse_iso
from remediator.actions import Cluster, PlanError, execute, plan
from remediator.policy import decide
from remediator.store import Store

from .synth import T0, make_chunks

FAULT = T0 + 2 * 3600


# --- a fake kubectl ------------------------------------------------------------------
def rollout(current="aaa", stable="aaa"):
    return {"status": {"currentPodHash": current, "stableRS": stable, "phase": "Healthy"}}


def application(params_now, history):
    return {"spec": {"source": {"helm": {"parameters": params_now} if params_now is not None else {}}},
            "status": {"history": history}}


class FakeKubectl:
    def __init__(self, objects):
        self.objects, self.patches = objects, []
        self.fail_patch = False

    def __call__(self, *args):
        if "get" in args:
            kind, name, ns = args[1], args[2], args[4]
            if (kind, name, ns) not in self.objects:
                raise KubeError(f"{kind} {name} not found")
            return json.dumps(self.objects[(kind, name, ns)])
        self.patches.append(args)
        if self.fail_patch:
            raise KubeError("forbidden")
        return f"{args[3]}/{args[4]} patched"


def cluster(**objects):
    base = {
        ("rollouts.argoproj.io", "catalog", "shopsearch"): rollout(),
        ("applications.argoproj.io", "catalog", "argocd"): application(
            [{"name": "image.tag", "value": "bad1234"}],
            [{"id": 6, "revision": "abc", "source": {"helm": {}}},
             {"id": 7, "revision": "abc", "source": {"helm": {"parameters": [{"name": "image.tag", "value": "bad1234"}]}}}]),
        ("horizontalpodautoscalers.autoscaling", "gateway", "shopsearch"):
            {"spec": {"minReplicas": 1, "maxReplicas": 4}, "status": {"currentReplicas": 2}},
        ("deployments.apps", "redis", "shopsearch"): {},
        ("clusters.postgresql.cnpg.io", "postgres", "shopsearch"): {},
        ("rollouts.argoproj.io", "gateway", "shopsearch"): rollout(),
    }
    base.update(objects)
    fake = FakeKubectl(base)
    return Cluster(run=fake), fake


# --- plans -----------------------------------------------------------------------------
def test_rollback_aborts_a_canary_in_progress():
    c, fake = cluster()
    fake.objects[("rollouts.argoproj.io", "catalog", "shopsearch")] = rollout("new", "old")
    p = plan(c, "rollback", "catalog")
    assert p.how == "abort-canary" and p.steps[0].subresource == "status"
    assert p.steps[0].args(dry_run=True)[-1] == "--dry-run=server"


def test_rollback_goes_back_to_the_previous_argocd_release():
    c, fake = cluster()
    p = plan(c, "rollback", "catalog")
    assert p.how == "argocd-previous-release" and p.steps[0].patch_type == "json"
    assert p.before["back_to"] == 6
    results = execute(c, p)
    assert results[0]["ok"] and fake.patches[0][:5] == ("-n", "argocd", "patch", "applications.argoproj.io", "catalog")


def test_rollback_refuses_what_only_git_can_undo():
    c, fake = cluster()
    app = fake.objects[("applications.argoproj.io", "catalog", "argocd")]
    app["status"]["history"][0]["revision"] = "older"
    with pytest.raises(PlanError, match="revert that commit"):
        plan(c, "rollback", "catalog")
    app["status"]["history"] = app["status"]["history"][-1:]
    with pytest.raises(PlanError, match="no earlier release"):
        plan(c, "rollback", "catalog")
    with pytest.raises(PlanError):
        plan(c, "rollback", "postgres")
    with pytest.raises(PlanError, match="could not read"):
        plan(c, "rollback", "indexer")  # not in the fake: as if RBAC said no


def test_restart_and_scale_out_plans():
    c, _ = cluster()
    assert plan(c, "restart", "catalog").steps[0].patch["spec"]["restartAt"]
    assert "restartedAt" in json.dumps(plan(c, "restart", "redis").steps[0].patch)
    assert plan(c, "restart", "postgres").steps[0].kind == "clusters.postgresql.cnpg.io"
    with pytest.raises(PlanError):
        plan(c, "restart", "node")
    p = plan(c, "scale_out", "gateway")
    assert p.steps[0].patch == {"spec": {"minReplicas": 3}} and p.before["min_replicas"] == 1
    c2, fake = cluster()
    fake.objects[("horizontalpodautoscalers.autoscaling", "gateway", "shopsearch")]["status"]["currentReplicas"] = 4
    fake.objects[("horizontalpodautoscalers.autoscaling", "gateway", "shopsearch")]["spec"]["minReplicas"] = 4
    with pytest.raises(PlanError, match="maximum"):
        plan(c2, "scale_out", "gateway")
    with pytest.raises(PlanError):
        plan(c, "none_escalate", None)


def test_execute_stops_at_the_first_failure():
    c, fake = cluster()
    fake.fail_patch = True
    results = execute(c, plan(c, "restart", "catalog"))
    assert results == [{"step": "rolling restart of catalog", "ok": False, "output": "forbidden", "dry_run": False}]


# --- policy ---------------------------------------------------------------------------
def test_policy():
    v = {"action": "rollback", "action_target": "catalog"}
    assert decide(v, canary_in_progress=True, executed=[], now=1e6).mode == "auto"
    assert decide(v, canary_in_progress=False, executed=[], now=1e6).mode == "approval"
    assert decide({"action": "none_escalate"}, canary_in_progress=False, executed=[], now=1e6).mode == "notify"
    assert decide({**v, "fallback": "x"}, canary_in_progress=True, executed=[], now=1e6).mode == "notify"
    ran = [{"action": "rollback", "target": "catalog", "at": 1e6 - 600}]
    assert decide(v, canary_in_progress=True, executed=ran, now=1e6).mode == "refused"
    many = [{"action": "restart", "target": t, "at": 1e6 - 60} for t in ("redis", "gateway", "indexer")]
    assert decide(v, canary_in_progress=False, executed=many, now=1e6).mode == "refused"
    assert decide(v, canary_in_progress=False, executed=ran, now=1e6 + 1900).mode == "approval"


# --- store ----------------------------------------------------------------------------
def test_audit_log_is_hash_chained(tmp_path):
    s = Store(tmp_path / "state.db")
    s.audit("loop", "a", "inc-1", {"x": 1})
    s.audit("alice", "approved", "inc-1", {"proposal": 1})
    s.audit("loop", "b", None)
    assert s.verify_audit()[0] and len(s.audit_log("inc-1")) == 2
    s.db.execute("UPDATE audit SET actor = 'mallory' WHERE seq = 2")
    s.db.commit()
    ok, msg = s.verify_audit()
    assert not ok and "entry 2" in msg


# --- the live loop -------------------------------------------------------------------
class SynthSource:
    """Two and a half hours of synthetic telemetry: catalog 5xx after a catalog release."""

    def __init__(self, end=FAULT + 900):
        def modify(metric, comp, t, v):
            if metric == "errors" and FAULT <= t < end and comp in ("catalog", "gateway"):
                return 0.4 if comp == "catalog" else 1.2
            return v
        self.all = make_chunks(2.6, modify=modify)
        for c in self.all:
            c["deploys"] = {"history": [{"app": "catalog", "deployed_at": iso(FAULT - 60), "id": 7, "revision": "abc",
                                         "parameters": {"image.tag": "bad1234"}}]
                            if c["start"] <= iso(FAULT - 60) < c["end"] else [],
                            "applications": [], "rollouts": [], "observed_at": None}

    def chunks(self, now):
        return [copy.deepcopy(c) for c in self.all if parse_iso(c["start"]) < now]


def make_loop(tmp_path, **kw):
    c, fake = cluster()
    store = Store(tmp_path / "state.db")
    loop = Loop(SynthSource(), store, c, variant="B2", runs_dir=tmp_path / "runs", started=FAULT - 600,
                log=lambda s: None, **kw)
    return loop, store, fake


def test_loop_opens_triages_proposes_and_acts_after_one_approval(tmp_path):
    loop, store, fake = make_loop(tmp_path)
    loop.tick(FAULT + 90)
    [inc] = store.incidents()
    assert inc["status"] == "open"                     # triage waits two minutes
    loop.tick(FAULT + 300)
    inc = store.incident(inc["id"])
    assert inc["status"] == "awaiting-approval" and inc["verdict"]["action"] == "rollback"
    [prop] = store.proposals(inc["id"])
    assert prop["mode"] == "approval" and prop["status"] == "pending" and not fake.patches
    assert (tmp_path / "runs" / f"{inc['id']}.json").exists()
    done = loop.approve(prop["id"], "alice")
    assert done["status"] == "executed" and done["decided_by"] == "alice"
    assert fake.patches and fake.patches[0][4] == "catalog"
    with pytest.raises(ValueError):
        loop.approve(prop["id"], "alice")             # once only
    loop.tick(FAULT + 1500)                           # the errors stopped at +900
    assert store.incident(inc["id"])["status"] == "resolved"
    events = [e["event"] for e in store.audit_log(inc["id"])]
    assert events == ["incident-opened", "triaged", "proposed", "approved", "executed", "resolved"]
    assert store.verify_audit()[0]


def test_loop_aborts_a_failing_canary_by_itself(tmp_path):
    loop, store, fake = make_loop(tmp_path)
    fake.objects[("rollouts.argoproj.io", "catalog", "shopsearch")] = rollout("new", "old")
    loop.tick(FAULT + 90)
    loop.tick(FAULT + 300)
    [prop] = store.proposals()
    assert prop["mode"] == "auto" and prop["status"] == "executed" and prop["decided_by"] == "policy (auto)"
    assert "--subresource=status" in fake.patches[0]


def test_loop_does_not_run_a_stale_plan_or_act_twice(tmp_path):
    loop, store, fake = make_loop(tmp_path, dry_run=True)
    loop.tick(FAULT + 90)
    loop.tick(FAULT + 300)
    [prop] = store.proposals()
    # The release was reverted by someone else before the approval came.
    app = fake.objects[("applications.argoproj.io", "catalog", "argocd")]
    app["spec"]["source"]["helm"] = {}
    assert loop.approve(prop["id"], "bob")["status"] == "stale" and not fake.patches
    assert loop.store.incident(prop["incident"])["status"] == "escalated"


def test_loop_ignores_incidents_from_before_it_started(tmp_path):
    loop, store, _ = make_loop(tmp_path)
    loop.started = FAULT + 3600
    loop.tick(FAULT + 300)
    assert store.incidents() == []


def test_live_source_records_settled_chunks_once(tmp_path):
    calls = []

    def record(kube, q, t0, t1, now=None):
        calls.append((t0, t1))
        return {"start": iso(t0), "end": iso(t1), "step": 15, "metrics": {}, "alerts": [], "errors": {}}

    src = LiveSource(kube=None, queries={"step_seconds": 15}, directory=tmp_path / "rec", history_s=1800, record=record)
    now = T0 + 3600 + 60           # 60 s into a chunk: the one before ended less than LAG_SECONDS ago
    first = src.chunks(now)
    assert len(first) == 7 and calls[-1] == (T0 + 3600, now)  # 5 settled, 1 not yet settled, the one in progress
    n = len(calls)
    src.chunks(now + 30)            # the one before has settled now: recorded for good, and the current one
    assert len(calls) == n + 2
    src.chunks(now + 60)
    assert len(calls) == n + 3      # only the chunk in progress
    assert len(list((tmp_path / "rec" / "chunks").glob("*.json.gz"))) == 6


def test_same_plan_ignores_restart_time_only():
    a = {"how": "restart", "steps": [{"kind": "k", "name": "n", "namespace": "s", "subresource": None, "patch": {"t": 1}}]}
    b = {"how": "restart", "steps": [{"kind": "k", "name": "n", "namespace": "s", "subresource": None, "patch": {"t": 2}}]}
    assert _same(a, b)
    assert not _same({**a, "how": "scale-out"}, {**b, "how": "scale-out"})


# --- API ----------------------------------------------------------------------------
def test_api_lists_shows_and_approves(tmp_path):
    loop, store, fake = make_loop(tmp_path)
    loop.tick(FAULT + 90)
    loop.tick(FAULT + 300)
    client = TestClient(create_app(store, loop, grafana="http://g:3000", console_dist=tmp_path / "none"))
    [row] = client.get("/api/incidents").json()
    assert row["pending"] and row["action"] == "rollback" and row["root_cause"] == "catalog / bad-release"
    detail = client.get(f"/api/incidents/{row['id']}").json()
    assert detail["verdict"]["hypotheses"] and detail["timeline"][0]["event"] == "incident-opened"
    assert any("/d/shopsearch-red?" in g["url"] and "var-service=catalog" in g["url"] for g in detail["grafana"])
    pid = detail["proposals"][0]["id"]
    assert client.post(f"/api/proposals/{pid}/approve", json={"by": ""}).status_code == 422
    r = client.post(f"/api/proposals/{pid}/approve", json={"by": "alice"})
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert client.post(f"/api/proposals/{pid}/reject", json={"by": "alice"}).status_code == 409
    audit = client.get("/api/audit").json()
    assert audit["ok"] and any(e["event"] == "approved" and e["actor"] == "alice" for e in audit["entries"])
    assert client.get("/api/incidents/nope").status_code == 404
    assert client.get("/api/config").json()["audit_ok"]


def test_api_without_a_loop_cannot_act(tmp_path):
    store = Store(tmp_path / "s.db")
    from loop.cli import main
    assert main(["demo", "--db", str(tmp_path / "s.db")]) == 0
    client = TestClient(create_app(store, None, console_dist=tmp_path / "none"))
    pid = client.get("/api/incidents/inc-demo-catalog").json()["proposals"][0]["id"]
    assert client.post(f"/api/proposals/{pid}/approve", json={"by": "x"}).status_code == 409


def test_grafana_links_cover_services_and_the_platform():
    links = grafana_links("http://g", ["catalog->postgres", "gateway"], 1000.0, None)
    titles = [l["title"] for l in links]
    assert titles == ["catalog: RED", "gateway: RED", "Platform: nodes, DNS, Redis, PostgreSQL", "SLOs"]
