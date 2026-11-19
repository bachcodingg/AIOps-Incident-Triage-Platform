"""The live loop: detect, correlate, triage, decide, act, on a running cluster.

Every tick (30 s by default):

  1. LiveSource brings the telemetry of the last two hours up to date: settled
     5-minute chunks are recorded once and kept (and written to disk, if a
     directory is given, as an ordinary recording); the chunk in progress is
     recorded fresh each tick. Same recorder, same aiops account, same
     queries as the dataset, so the live agent sees exactly what the
     evaluated one saw.
  2. The detectors and the correlator run over that window (variant A1's
     detectors: burn-rate and anomaly). New incidents are stored.
  3. An incident open for `triage_delay_seconds` is triaged: the agent (or B2)
     on a View at that moment, through the same code as in replay.
  4. The policy decides; an allowed action is planned as the remediator and
     either run at once (auto), or left for one approval in the console.
  5. An incident whose alerts have all resolved is marked resolved, with its
     time to recover.

The loop never sees labels, and it runs beside the cluster like the recorder
and chaos-runner do, not in it (docs/design.md section 13).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from correlator.correlate import VARIANTS, correlate, select
from correlator.graph import ServiceGraph
from detector.alerts import ANOMALY, SERVICES, Alert, iso
from detector.cli import detect, load_config as load_detector_config
from recorder.kube import Kube, KubeError
from recorder.record import CHUNK_SECONDS, LAG_SECONDS, chunk_name, load_queries, record_chunk, write_chunk
from recorder.store import Recording
from remediator.actions import Cluster, PlanError, canary_in_progress, execute, plan
from remediator.policy import decide
from remediator.store import Store
from triage.prompt import brief
from triage.replay import View, visible_alert
from triage.rule_rca import rule_rca
from triage.run import load_config as load_triage_config, make_agent

HISTORY_S = 2 * 3600  # the MAD detector scores a point against the hour before it: keep that hour whole for the last hour


class LiveSource:
    """The recent past as chunks, recorded as the aiops account."""

    def __init__(self, kube: Kube, queries: dict[str, Any] | None = None, directory: Path | None = None,
                 history_s: float = HISTORY_S, chunk_s: int = CHUNK_SECONDS,
                 record: Callable[..., dict[str, Any]] = record_chunk):
        self.kube, self.q = kube, queries or load_queries()
        self.directory, self.history_s, self.chunk_s = directory, history_s, chunk_s
        self._record = record
        self.cache: dict[float, dict[str, Any]] = {}

    def _settled(self, t0: float) -> dict[str, Any]:
        if t0 in self.cache:
            return self.cache[t0]
        path = self.directory / "chunks" / chunk_name(t0) if self.directory else None
        if path is not None and path.exists():
            chunk = Recording.load(path)
        else:
            chunk = self._record(self.kube, self.q, t0, t0 + self.chunk_s)
            if self.directory:
                write_chunk(self.directory, chunk)
        self.cache[t0] = chunk
        return chunk

    def chunks(self, now: float) -> list[dict[str, Any]]:
        step = self.q["step_seconds"]
        current = math.floor(now / self.chunk_s) * self.chunk_s
        first = math.floor((now - self.history_s) / self.chunk_s) * self.chunk_s
        out = []
        for t0 in range(int(first), int(current), self.chunk_s):
            if now >= t0 + self.chunk_s + LAG_SECONDS:
                out.append(self._settled(t0))
            else:  # ended, but its data has not settled: record it again next tick
                out.append(self._record(self.kube, self.q, t0, t0 + self.chunk_s))
        if now - current >= 2 * step:
            out.append(self._record(self.kube, self.q, current, now))
        self.cache = {t: c for t, c in self.cache.items() if t >= first}
        return out


def alert_key(a: dict[str, Any] | Alert) -> str:
    d = a.to_dict() if isinstance(a, Alert) else a
    return f"{d['detector']}/{d['name']}/{d['component']}/{d['start']}"


@dataclass
class Loop:
    source: Any                     # LiveSource, or anything with chunks(now)
    store: Store
    cluster: Cluster                # as the remediator
    variant: str = "B2"
    dry_run: bool = False
    runs_dir: Path | None = None
    models: dict | None = None      # Isolation Forests, if trained
    started: float = field(default_factory=time.time)
    triage_cfg: dict[str, Any] = field(default_factory=load_triage_config)
    detector_cfg: dict[str, Any] = field(default_factory=load_detector_config)
    llm: Any = None                 # tests replace the model
    log: Callable[[str], None] = print

    # --- one tick ------------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        chunks = self.source.chunks(now)
        if not chunks:
            return
        alerts = detect(chunks, self.detector_cfg, self.models)
        view = View.from_chunks(chunks, now, [a for a in alerts if a.detector == ANOMALY], ServiceGraph.load())
        incidents = correlate(select(view.alerts, VARIANTS[self.variant if self.variant in VARIANTS else "A1"]), view.graph)
        self._sync_incidents(incidents, now)
        delay = self.triage_cfg["triage_delay_seconds"]
        for inc in self.store.incidents(status="open"):
            if now >= inc["opened"] + delay:
                self.triage(inc, view, now)
        self._resolve(incidents, now)

    def _sync_incidents(self, incidents: list, now: float) -> None:
        known = {alert_key(a): i["id"] for i in self.store.incidents() if i["status"] != "resolved" for a in i["alerts"]}
        for inc in incidents:
            if inc.opened < self.started:
                continue  # it began before the loop did: not ours to act on
            alerts = [a.to_dict() for a in sorted(inc.alerts, key=lambda a: a.start)]
            ids = {known[k] for a in alerts if (k := alert_key(a)) in known}
            first = alerts[0]
            iid = sorted(ids)[0] if ids else f"inc-{first['start'].replace('-', '').replace(':', '')}-{first['component']}"
            if self.store.upsert_incident({"id": iid, "opened": inc.opened, "components": inc.components,
                                           "alerts": alerts}, now):
                self.store.audit("loop", "incident-opened", iid, {"components": inc.components,
                                                                  "alerts": [f"{a['name']} on {a['component']}" for a in alerts]}, at=now)
                self.log(f"{iid}: opened ({', '.join(inc.components)})")

    def _resolve(self, incidents: list, now: float) -> None:
        by_key = {alert_key(a): inc for inc in incidents for a in inc.alerts}
        for stored in self.store.incidents():
            if stored["status"] in ("resolved", "open"):
                continue
            live = [by_key[k] for a in stored["alerts"] if (k := alert_key(a)) in by_key]
            if live and all(inc.closed is not None for inc in live):
                closed = max(inc.closed for inc in live)
                self.store.set_incident(stored["id"], status="resolved", closed=closed)
                self.store.audit("loop", "resolved", stored["id"],
                                 {"time_to_recover_s": round(closed - stored["opened"]), "was": stored["status"]}, at=now)
                self.log(f"{stored['id']}: resolved after {(closed - stored['opened']) / 60:.1f} min")

    # --- triage and decision ---------------------------------------------------------
    def triage(self, stored: dict[str, Any], view: View, now: float) -> None:
        iid = stored["id"]
        alerts = [v for a in stored["alerts"] if (v := visible_alert(Alert.from_dict(a), now))]
        text = brief(iid, alerts, now)
        if self.variant == "B2":
            verdict, run = rule_rca(view, alerts).model_dump(), {"variant": "B2", "verdict": None, "valid": True, "cost_usd": 0.0}
            run["verdict"] = verdict
        else:
            agent = make_agent(self.variant, self.triage_cfg, llm=self.llm)
            run = agent.triage(view, iid, text).to_dict()
            verdict = run["verdict"]
        path = None
        if self.runs_dir:
            path = Path(self.runs_dir) / f"{iid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(run, indent=1, default=str) + "\n", encoding="utf-8")
        self.store.set_incident(iid, verdict=verdict, status="triaged", run_path=str(path) if path else None)
        top = (verdict.get("hypotheses") or [{}])[0]
        self.store.audit("agent" if self.variant != "B2" else "rule-baseline", "triaged", iid, {
            "variant": self.variant, "valid": run.get("valid"), "root_cause": f"{top.get('component')} / {top.get('category')}",
            "action": verdict["action"], "target": verdict.get("action_target"), "cost_usd": run.get("cost_usd"),
            "summary": verdict.get("summary")}, at=now)
        self.log(f"{iid}: {top.get('component')} / {top.get('category')} -> {verdict['action']} {verdict.get('action_target') or ''}")
        self.propose(iid, verdict, now)

    def _canary(self, action: str, target: str | None) -> bool:
        if action != "rollback" or target not in SERVICES:
            return False
        try:
            return canary_in_progress(self.cluster.get("rollouts.argoproj.io", target, "shopsearch"))
        except (KubeError, ValueError):
            return False

    def propose(self, iid: str, verdict: dict[str, Any], now: float) -> int | None:
        action, target = verdict["action"], verdict.get("action_target")
        d = decide(verdict, canary_in_progress=self._canary(action, target), executed=self.store.executed(), now=now)
        if d.mode in ("notify", "refused"):
            pid = self.store.add_proposal(iid, action, target, d.mode, d.reason, d.mode) if d.mode == "refused" else None
            self.store.set_incident(iid, status="escalated")
            self.store.audit("policy", "escalated" if d.mode == "notify" else "refused", iid, {"reason": d.reason}, at=now)
            return pid
        try:
            p = plan(self.cluster, action, target)
        except PlanError as exc:
            pid = self.store.add_proposal(iid, action, target, d.mode, str(exc), "unplannable")
            self.store.set_incident(iid, status="escalated")
            self.store.audit("remediator", "unplannable", iid, {"action": action, "target": target, "reason": str(exc)}, at=now)
            return pid
        pid = self.store.add_proposal(iid, action, target, d.mode, d.reason, "pending", p.to_dict())
        self.store.audit("policy", "proposed", iid, {"proposal": pid, "mode": d.mode, "reason": d.reason,
                                                     "commands": p.to_dict()["commands"]}, at=now)
        if d.mode == "auto":
            self.run_proposal(pid, "policy (auto)")
        else:
            self.store.set_incident(iid, status="awaiting-approval")
        return pid

    # --- humans ----------------------------------------------------------------------------
    def approve(self, pid: int, by: str) -> dict[str, Any]:
        prop = self.store.proposal(pid)
        if prop is None or prop["status"] != "pending":
            raise ValueError(f"proposal {pid} is not waiting for approval")
        self.store.audit(by, "approved", prop["incident"], {"proposal": pid, "action": prop["action"], "target": prop["target"]})
        return self.run_proposal(pid, by)

    def reject(self, pid: int, by: str, reason: str = "") -> dict[str, Any]:
        prop = self.store.proposal(pid)
        if prop is None or prop["status"] != "pending":
            raise ValueError(f"proposal {pid} is not waiting for approval")
        self.store.set_proposal(pid, status="rejected", decided_by=by, decided_at=time.time())
        self.store.set_incident(prop["incident"], status="rejected")
        self.store.audit(by, "rejected", prop["incident"], {"proposal": pid, "reason": reason})
        return self.store.proposal(pid)

    def run_proposal(self, pid: int, by: str) -> dict[str, Any]:
        prop = self.store.proposal(pid)
        iid = prop["incident"]
        self.store.set_incident(iid, status="acting")
        # Plan again: the cluster may have moved since the proposal. Run only if
        # it would still do what was approved.
        try:
            fresh = plan(self.cluster, prop["action"], prop["target"])
        except PlanError as exc:
            fresh, problem = None, str(exc)
        else:
            problem = None if _same(fresh.to_dict(), prop["plan"]) else "the system changed since the proposal"
        if problem:
            self.store.set_proposal(pid, status="stale", decided_by=by, decided_at=time.time(), result={"reason": problem})
            self.store.set_incident(iid, status="escalated")
            self.store.audit("remediator", "not-run", iid, {"proposal": pid, "reason": problem})
            return self.store.proposal(pid)
        results = execute(self.cluster, fresh, dry_run=self.dry_run)
        ok = all(r["ok"] for r in results)
        self.store.set_proposal(pid, status="executed" if ok else "failed", decided_by=by, decided_at=time.time(),
                                result={"steps": results, "plan": fresh.to_dict()})
        self.store.set_incident(iid, status="acted" if ok else "escalated")
        self.store.audit("remediator", "executed" if ok else "failed", iid,
                         {"proposal": pid, "by": by, "dry_run": self.dry_run, "steps": results})
        self.log(f"{iid}: {prop['action']} {prop['target']} {'done' if ok else 'FAILED'}{' (dry run)' if self.dry_run else ''}")
        return self.store.proposal(pid)


def _same(a: dict[str, Any], b: dict[str, Any] | None) -> bool:
    """The same change: same objects and patches, a restart's timestamp aside."""
    if b is None or a["how"] != b["how"]:
        return False
    def shape(p: dict[str, Any]) -> list:
        return [(s["kind"], s["name"], s["namespace"], s["subresource"],
                 None if p["how"] == "restart" else json.dumps(s["patch"], sort_keys=True)) for s in p["steps"]]
    return shape(a) == shape(b)


def iso_or_none(t: float | None) -> str | None:
    return iso(t) if t else None
