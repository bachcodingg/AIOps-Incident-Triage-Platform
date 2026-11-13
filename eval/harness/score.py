"""Detection metrics for B0, B1 and B2 (docs/design.md section 6).

  detection recall   share of incidents with an alert within 10 minutes of the fault's start
  MTTD               median time from the fault's start to its first alert, over detected incidents
  false alarms       incidents opened during fault-free windows, per 24 h of them
  alert noise        raw alerts per incident, before and after correlation

An alert counts for a fault if it starts between the fault's start and 10
minutes later; alerts already firing when a fault starts do not detect it.
Noise counts the alerts and incidents that start between the fault's start and
5 minutes after it ended.

Scores the tuning set. The test set is scored once, in week 10, after which
nothing is tuned again (gate G3); --final is how week 10 asks for it.
"""

from __future__ import annotations

import statistics
from typing import Any

from chaos_runner.schedule import BENIGN, FAULT, FAULT_FREE
from correlator.correlate import VARIANTS, correlate, select
from correlator.graph import ServiceGraph
from detector.alerts import Alert, parse_time

DETECT_WITHIN_S = 600
NOISE_AFTER_S = 300
DETECTION_VARIANTS = ("B0", "B1", "B2")  # A1 detects exactly as B2 does


def _window(item: dict[str, Any]) -> tuple[float, float]:
    return parse_time(item["start"]), parse_time(item["end"])


def score_variant(alerts: list[Alert], items: list[dict[str, Any]], graph: ServiceGraph,
                  detectors: tuple[str, ...]) -> dict[str, Any]:
    chosen = select(alerts, detectors)
    incidents = correlate(chosen, graph)
    per_fault: dict[str, dict[str, list]] = {}
    ttd, raw, after, n_faults = [], [], [], 0
    for item in (i for i in items if i["kind"] == FAULT):
        n_faults += 1
        s, e = _window(item)
        first = min((a.start for a in chosen if s <= a.start <= s + DETECT_WITHIN_S), default=None)
        entry = per_fault.setdefault(item["fault"], {"detected": [], "ttd": []})
        entry["detected"].append(first is not None)
        if first is not None:
            ttd.append(first - s)
            entry["ttd"].append(first - s)
            raw.append(sum(1 for a in chosen if s <= a.start <= e + NOISE_AFTER_S))
            after.append(sum(1 for inc in incidents if s <= inc.opened <= e + NOISE_AFTER_S))
    quiet = {kind: [_window(i) for i in items if i["kind"] == kind] for kind in (FAULT_FREE, BENIGN)}

    def per_day(windows: list[tuple[float, float]]) -> float | None:
        hours = sum(e - s for s, e in windows) / 3600
        opened = sum(1 for inc in incidents for s, e in windows if s <= inc.opened < e)
        return None if hours == 0 else opened / hours * 24

    noise_before = statistics.mean(raw) if raw else None
    noise_after = statistics.mean(after) if after else None
    return {
        "detectors": list(detectors),
        "incidents": n_faults,
        "recall": len(ttd) / n_faults if n_faults else None,
        "mttd_s": statistics.median(ttd) if ttd else None,
        "false_alarms_per_day": per_day(quiet[FAULT_FREE]),
        "benign_release_alarms_per_day": per_day(quiet[BENIGN]),
        "alerts_per_incident": noise_before,
        "incidents_per_incident": noise_after,
        "noise_reduction": (1 - noise_after / noise_before) if noise_before else None,
        "per_fault": {f: {"detected": sum(v["detected"]), "of": len(v["detected"]),
                          "mttd_s": statistics.median(v["ttd"]) if v["ttd"] else None}
                      for f, v in sorted(per_fault.items())},
    }


def score(alerts: list[Alert], items: list[dict[str, Any]], graph: ServiceGraph) -> dict[str, Any]:
    return {v: score_variant(alerts, items, graph, VARIANTS[v]) for v in DETECTION_VARIANTS}


def _fmt(v: float | None, kind: str) -> str:
    if v is None:
        return "–"
    if kind == "pct":
        return f"{v:.0%}"
    if kind == "s":
        return f"{v / 60:.1f} min" if v >= 120 else f"{v:.0f} s"
    return f"{v:.2f}"


def markdown(results: dict[str, Any], set_name: str) -> str:
    lines = [f"Detection on the {set_name} set", "",
             "| Variant | Detectors | Recall (10 min) | MTTD | False alarms / 24 h | Alerts per incident | After correlation | Noise reduction |",
             "|---|---|---|---|---|---|---|---|"]
    for v, r in results.items():
        lines.append(f"| {v} | {' + '.join(r['detectors'])} | {_fmt(r['recall'], 'pct')} | {_fmt(r['mttd_s'], 's')} | "
                     f"{_fmt(r['false_alarms_per_day'], 'n')} | {_fmt(r['alerts_per_incident'], 'n')} | "
                     f"{_fmt(r['incidents_per_incident'], 'n')} | {_fmt(r['noise_reduction'], 'pct')} |")
    faults = sorted({f for r in results.values() for f in r["per_fault"]})
    lines += ["", "Detected per fault (MTTD in brackets)", "", "| Fault | " + " | ".join(results) + " |",
              "|---|" + "---|" * len(results)]
    for f in faults:
        cells = []
        for r in results.values():
            p = r["per_fault"].get(f)
            cells.append("–" if p is None else f"{p['detected']}/{p['of']}" + (f" ({_fmt(p['mttd_s'], 's')})" if p["mttd_s"] is not None else ""))
        lines.append(f"| {f} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
