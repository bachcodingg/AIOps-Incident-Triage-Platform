"""Time to recover, with and without the agent (week 9's live test).

Two short chaos runs on the same faults: one with `make console` running (the
loop triages, proposes, and someone approves), one without (the fault lasts its
planned duration, which stands for the time a human takes). For each fault:

  time to recover = when its incident closed (every alert resolved) - the fault's start

The incident is the first one the detectors opened within 10 minutes of the
start, as in the triage scores. A fault no incident caught has no time.
Labels come from each run's sealed labels; this is not the frozen dataset, so
no split and no seal check, and the result is reported as a live test of
about ten incidents, not as an evaluation result.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from chaos_runner.labels import read
from chaos_runner.schedule import FAULT
from detector.alerts import parse_time

from .triage import match


def recover_times(labels_path: Path, incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [r for r in read(labels_path) if r.get("kind") == FAULT and not r.get("aborted") and not r.get("error")]
    rows = []
    for item, inc in match(items, incidents):
        closed = parse_time(inc["closed"]) if inc and inc.get("closed") else None
        rows.append({"seq": item["seq"], "fault": item["fault"], "target": item.get("target"),
                     "detected": inc is not None, "incident": inc and inc["id"],
                     "recover_s": None if closed is None else round(closed - parse_time(item["start"]))})
    return rows


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    times = [r["recover_s"] for r in rows if r["recover_s"] is not None]
    return {"faults": len(rows), "recovered": len(times),
            "median_s": statistics.median(times) if times else None, "mean_s": statistics.mean(times) if times else None}


def markdown(results: dict[str, dict[str, Any]]) -> str:
    def m(v: float | None) -> str:
        return "–" if v is None else f"{v / 60:.1f} min"
    lines = ["| Run | Faults | Recovered | Median time to recover | Mean |", "|---|---|---|---|---|"]
    for name, s in results.items():
        lines.append(f"| {name} | {s['faults']} | {s['recovered']} | {m(s['median_s'])} | {m(s['mean_s'])} |")
    if len(results) == 2:
        a, b = (s["median_s"] for s in results.values())
        if a is not None and b:
            lines += ["", f"Median change: {(a - b) / b:+.0%} (target: -50%, docs/design.md section 6)."]
    return "\n".join(lines) + "\n"
