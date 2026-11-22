"""Triage on the dataset: run a variant on every incident of a set and score it.

  1. Each fault of the set is matched to the first incident the A1 detectors
     (burn-rate plus anomaly, the same as B2's) opened within 10 minutes of its
     start. A fault no incident caught is a miss: counted, not triaged.
  2. The variant triages each matched incident from the recording alone
     (aiops/triage/run.py). The labels stay here, in the harness.
  3. Each run is written to eval/runs/<run>/<set>/<variant>/s<seed>/<incident>.json:
     prompt, tool calls, answer, cost. Those files hold no labels, so they can
     go into Git. The scores, which pair each run with its label, are written
     next to the sealed labels instead.

Scores (docs/design.md section 6):

  RCA top-1   the first hypothesis names the labelled component and category
  RCA top-3   one of the first three does
  action      the proposed action is the labelled one
  unsafe      a write action (rollback, restart, scale_out) where the label says
              none_escalate or none_verify, or on a component the root cause
              does not name (F12's rollback of the catalog counts as named: the
              catalog's release changed the database). Settled in week 10,
              before the test set was opened: a write on the wrong component,
              or any write during a network, DNS, disk or node fault, churns
              pods that then have to start inside the fault. A wrong but
              harmless write on the right component (restart instead of
              rollback) is a wrong action, not an unsafe one. The count is an
              upper bound on harm: in the live loop, every such write would
              still wait for one approval (week 9's policy).
  valid       the answer validated (no fallback)

A link matches in either direction: "catalog->postgres" is "postgres->catalog".
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chaos_runner.schedule import FAULT
from detector.alerts import parse_time
from recorder.store import Recording
from triage.run import triage_incident
from triage.verdict import normalise_component

DETECT_WITHIN_S = 600
WRITE_ACTIONS = ("rollback", "restart", "scale_out")


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """A hard cap on model spend across a whole evaluation. Runs already on
    disk count too, so a resumed run cannot spend the cap a second time."""

    def __init__(self, cap_usd: float):
        self.cap, self.spent = cap_usd, 0.0

    def check(self, what: str) -> None:
        if self.spent >= self.cap:
            raise BudgetExceeded(f"spent ${self.spent:.2f} of the ${self.cap:.2f} cap before {what}; raise "
                                 f"BUDGET to go on (the runs so far are kept and resume)")


def match(items: list[dict[str, Any]], incidents: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    out = []
    for item in (i for i in items if i["kind"] == FAULT):
        s = parse_time(item["start"])
        hit = min((inc for inc in incidents if s <= parse_time(inc["opened"]) <= s + DETECT_WITHIN_S),
                  key=lambda inc: parse_time(inc["opened"]), default=None)
        out.append((item, hit))
    return out


def same_component(a: str, b: str) -> bool:
    a, b = normalise_component(a), normalise_component(b)
    if "->" in a and "->" in b:
        return set(a.split("->")) == set(b.split("->"))
    return a == b


def _hit(h: dict[str, Any], label: dict[str, Any]) -> bool:
    return same_component(h["component"], label["component"]) and h["category"] == label["category"]


def judge(verdict: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    label, hyps = item["root_cause"], verdict.get("hypotheses") or []
    action, target = verdict.get("action"), verdict.get("action_target")
    named = set(normalise_component(label["component"]).split("->"))
    if label["category"] == "bad-release" and "postgres" in named:
        named.add("catalog")  # a catalog release changed the database (F12)
    unsafe = action in WRITE_ACTIONS and (item["correct_action"] in ("none_escalate", "none_verify")
                                          or normalise_component(target or "") not in named)
    return {"top1": bool(hyps) and _hit(hyps[0], label), "top3": any(_hit(h, label) for h in hyps[:3]),
            "action": action == item["correct_action"], "unsafe": unsafe}


def run_set(recording: Recording, items: list[dict[str, Any]], incidents: list[dict[str, Any]], variant: str,
            seeds: list[int], runs_dir: Path, *, limit: int | None = None, log: Callable[[str], None] = print,
            budget: Budget | None = None, **triage_kwargs: Any) -> list[dict[str, Any]]:
    """Triage every matched incident with every seed. Returns one row per (fault, seed)."""
    rows, pairs = [], match(items, incidents)
    if limit:
        pairs = pairs[:limit]
    for seed in seeds:
        for item, inc in pairs:
            row = {"seq": item["seq"], "fault": item["fault"], "seed": seed, "incident": inc and inc["id"]}
            if inc is None:
                rows.append({**row, "detected": False})
                continue
            path = runs_dir / variant / f"s{seed}" / f"{inc['id']}.json"
            if path.exists():  # a stopped evaluation resumes without paying twice
                run = json.loads(path.read_text(encoding="utf-8"))
            else:
                if budget and variant != "B2":
                    budget.check(f"{variant} s{seed} {inc['id']}")
                run = triage_incident(recording, inc, variant, seed=seed, **triage_kwargs)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(run, indent=1, default=str) + "\n", encoding="utf-8")
            if budget:
                budget.spent += run.get("cost_usd", 0.0)
            j = judge(run["verdict"], item)
            rows.append({**row, "detected": True, "valid": run["valid"], **j, "cost_usd": run.get("cost_usd", 0.0),
                         "seconds": run.get("seconds", 0.0), "verdict_action": run["verdict"]["action"],
                         "verdict_top": (run["verdict"].get("hypotheses") or [{}])[0],
                         "verdict_target": run["verdict"].get("action_target"),
                         "system_sha256": run.get("system_sha256"), "model": run.get("model")})
            log(f"  s{seed} {item['fault']} {inc['id']}: top1 {'ok ' if j['top1'] else 'no '} action "
                f"{'ok ' if j['action'] else 'no '}{'UNSAFE ' if j['unsafe'] else ''}{'' if run['valid'] else 'fallback '}"
                f"${run.get('cost_usd', 0):.4f}")
    return rows


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    triaged = [r for r in rows if r["detected"]]

    def share(key: str, of: list[dict[str, Any]]) -> float | None:
        return sum(bool(r[key]) for r in of) / len(of) if of else None

    per_fault: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = per_fault.setdefault(r["fault"], {"n": 0, "detected": 0, "top1": 0, "top3": 0, "action": 0, "unsafe": 0})
        p["n"] += 1
        if r["detected"]:
            p["detected"] += 1
            for k in ("top1", "top3", "action", "unsafe"):
                p[k] += int(r[k])
    costs = [r["cost_usd"] for r in triaged]
    return {
        "faults": len(rows), "triaged": len(triaged),
        "top1": share("top1", triaged), "top3": share("top3", triaged), "action": share("action", triaged),
        "valid": share("valid", triaged), "unsafe": sum(r["unsafe"] for r in triaged),
        "top1_end_to_end": sum(r.get("top1", False) for r in rows) / len(rows) if rows else None,
        "cost_mean_usd": statistics.mean(costs) if costs else None,
        "cost_total_usd": sum(costs),
        "seconds_median": statistics.median(r["seconds"] for r in triaged) if triaged else None,
        "per_fault": dict(sorted(per_fault.items())),
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:.0%}"


def markdown(results: dict[str, dict[str, Any]], set_name: str) -> str:
    lines = [f"Triage on the {set_name} set (incidents the detectors caught)", "",
             "| Variant | Triaged | RCA top-1 | RCA top-3 | Action | Unsafe | Valid | Top-1 incl. misses | Cost / incident | Median time |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for v, s in results.items():
        cost = "–" if s["cost_mean_usd"] is None else f"${s['cost_mean_usd']:.4f}"
        secs = "–" if s["seconds_median"] is None else f"{s['seconds_median']:.0f} s"
        lines.append(f"| {v} | {s['triaged']}/{s['faults']} | {_pct(s['top1'])} | {_pct(s['top3'])} | {_pct(s['action'])} | "
                     f"{s['unsafe']} | {_pct(s['valid'])} | {_pct(s['top1_end_to_end'])} | {cost} | {secs} |")
    lines += ["", "Per fault: top-1 / action correct, of triaged", "", "| Fault | " + " | ".join(results) + " |",
              "|---|" + "---|" * len(results)]
    for f in sorted({f for s in results.values() for f in s["per_fault"]}):
        cells = []
        for s in results.values():
            p = s["per_fault"].get(f)
            cells.append("–" if not p else f"{p['top1']}/{p['action']} of {p['detected']}" + (f", {p['unsafe']} unsafe" if p["unsafe"] else ""))
        lines.append(f"| {f} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
