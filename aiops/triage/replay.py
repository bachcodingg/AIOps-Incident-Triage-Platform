"""Replay mode: a recording as it looked at one moment.

The agent triages a recorded incident as if it were live, at `now` (by default
two minutes after the incident opened). Everything it can see goes through
`cut()`, which drops whatever was not known yet at `now`:

  - metric, alert and log samples, events, trace summaries and ArgoCD history
    entries after `now`
  - the rollout and application status a chunk holds, unless it was observed
    by `now`: the recorder takes it about 90 s after the chunk ends, so the
    status in the chunk that contains `now` could already show the recovery
  - full traces that started after `now`
  - the end of an alert that resolves after `now`: at `now` it is still firing

The recording itself is never changed, and the same `now` always gives the same
view, so runs are repeatable.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from correlator.graph import ServiceGraph
from detector.alerts import Alert
from detector.series import Frame, load
from recorder.record import iso, parse_iso
from recorder.store import Recording

HISTORY_S = 2 * 3600      # how far back the agent can look
TRIAGE_DELAY_S = 120      # triage starts this long after the incident opens


def _cut_series(series: list[dict[str, Any]] | None, now: float) -> list[dict[str, Any]] | None:
    if series is None:
        return None
    out = []
    for s in series:
        values = [[t, v] for t, v in s["values"] if t <= now]
        if values:
            out.append({"labels": s["labels"], "values": values})
    return out


def cut(chunk: dict[str, Any], now: float) -> dict[str, Any] | None:
    """The chunk as far as it was known at `now`; None if it starts later."""
    if parse_iso(chunk["start"]) > now:
        return None
    c = copy.deepcopy(chunk)
    c["metrics"] = {name: _cut_series(s, now) for name, s in (c.get("metrics") or {}).items()}
    c["alerts"] = _cut_series(c.get("alerts"), now)
    if c.get("events") is not None:
        c["events"] = [e for e in c["events"] if parse_iso(e["time"]) <= now]
    if c.get("deploys") is not None:
        d = c["deploys"]
        d["history"] = [h for h in d.get("history", []) if parse_iso(h["deployed_at"]) <= now]
        if not d.get("observed_at") or parse_iso(d["observed_at"]) > now:
            d["applications"], d["rollouts"], d["observed_at"] = None, None, None
    if c.get("logs") is not None:
        logs = c["logs"]
        logs["lines"] = [entry for entry in logs.get("lines", []) if parse_iso(entry["time"]) <= now]
        logs["volume"] = _cut_series(logs.get("volume"), now) or []
    if c.get("traces") is not None:
        tr = c["traces"]
        tr["summaries"] = [s for s in tr.get("summaries", []) if s.get("start") and parse_iso(s["start"]) <= now]
        visible = {s["trace_id"] for s in tr["summaries"]}
        tr["full"] = [t for t in tr.get("full", []) if t.get("trace_id") in visible]
    # A chunk still in progress ends at `now`: alerts firing then count as still firing.
    c["end"] = iso(min(parse_iso(chunk["end"]), now))
    return c


def visible_alert(a: Alert, now: float) -> Alert | None:
    if a.start > now:
        return None
    if a.end is not None and a.end > now:
        return Alert(**{**a.__dict__, "end": None})
    return a


@dataclass
class View:
    """What the AIOps layer could see at `now`: metrics as a Frame, the rest as chunk sections."""

    now: float
    frame: Frame
    chunks: list[dict[str, Any]]
    alerts: list[Alert]          # static and burn-rate (recorded) plus anomaly (detector output), as of now
    graph: ServiceGraph

    @classmethod
    def at(cls, recording: Recording, now: float, anomaly_alerts: Iterable[Alert] = (),
           history_s: float = HISTORY_S, graph: ServiceGraph | None = None) -> View:
        return cls.from_chunks(recording.chunks(now - history_s, now + 1), now, anomaly_alerts, graph)

    @classmethod
    def from_chunks(cls, chunks: Iterable[dict[str, Any]], now: float, anomaly_alerts: Iterable[Alert] = (),
                    graph: ServiceGraph | None = None) -> View:
        """The same view from chunks in memory: the live loop records them as it goes."""
        chunks = [c for c in (cut(ch, now) for ch in chunks) if c is not None]
        if not chunks:
            raise ValueError(f"no telemetry before {now}")
        frame, recorded = load(chunks)
        alerts = [v for a in list(recorded) + list(anomaly_alerts) if (v := visible_alert(a, now))]
        graph = graph or ServiceGraph.load()
        graph.add_observed(frame)
        return cls(now, frame, chunks, sorted(alerts, key=lambda a: (a.start, a.name)), graph)

    def section(self, name: str) -> Iterable[tuple[dict[str, Any], Any]]:
        """(chunk, its section) for every chunk where the section was recorded, oldest first."""
        for c in self.chunks:
            if c.get(name) is not None:
                yield c, c[name]

    def failed_sections(self) -> list[str]:
        return sorted({k.split(".")[0] for c in self.chunks for k in (c.get("errors") or {})})
