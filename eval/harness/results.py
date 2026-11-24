"""`make results`: eval/results.md, eval/results.json and the charts in
eval/figures/, from the scores `harness final` left next to the sealed labels.

One script produces every number, so the write-up cannot drift from the runs.
What goes into Git is aggregates only: shares, counts per fault type, CIs.
The rows that pair each run with its label stay next to the labels.

  detection   B0, B1, B2: recall within 10 minutes and MTTD with 95% CIs
              (bootstrap over incidents), false alarms per 24 h of fault-free
              traffic, alerts per incident before and after correlation
  triage      B2 and A1 to A4: RCA top-1 and top-3, action accuracy and
              validity over the incidents the detectors caught, top-1 over
              every fault (misses count as wrong), unsafe actions, cost and
              time per incident; 3 seeds each, CIs over incidents
  A1 vs B2    McNemar's exact test on paired outcomes (an incident is right
              for a variant when most of its seeds are), also per seed
  ablations   A2 (no traces), A3 (no runbooks), A4 (open-weight model) minus
              A1, paired bootstrap CIs
  confusion   by fault type: the category the first hypothesis named, and the
              action proposed against the labelled one
  targets     every metric against the target fixed before running (design
              doc section 6), met or missed, and whether the CI spans it
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from chaos_runner.schedule import FAULT
from triage.verdict import ACTIONS, CATEGORIES

from . import stats

DETECTION = ("B0", "B1", "B2")
TRIAGE = ("B2", "A1", "A2", "A3", "A4")
ABLATIONS = {"A2": "A2 without traces", "A3": "A3 without runbooks", "A4": "A4 open-weight model"}
MISSED, NO_ANSWER = "missed", "no answer"
README_START, README_END = "<!-- results:start -->", "<!-- results:end -->"
WRONG_START, WRONG_END = "<!-- wrong:start -->", "<!-- wrong:end -->"


class ResultsError(ValueError):
    pass


# --- inputs --------------------------------------------------------------------

def load_scores(labels_dir: Path, set_name: str) -> tuple[dict | None, dict[str, list[dict]]]:
    det_path = labels_dir / f"detect-{set_name}.json"
    detection = json.loads(det_path.read_text(encoding="utf-8")) if det_path.exists() else None
    triage = {}
    for v in TRIAGE:
        path = labels_dir / f"triage-{set_name}-{v}.json"
        if path.exists():
            triage[v] = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if detection is None and not triage:
        raise ResultsError(f"no scores for the {set_name} set in {labels_dir}: run `make eval-final` "
                           f"(test) or `make detect-score` / `make triage` (tuning) first")
    return detection, triage


def _by_seq(rows: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["seq"], []).append(r)
    return dict(sorted(out.items()))


# --- detection -----------------------------------------------------------------

def detection_report(scores: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for v in DETECTION:
        s = scores.get(v)
        if not s:
            continue
        rows = s.get("rows", [])
        recall = stats.ratio([int(r["detected"]) for r in rows], [1] * len(rows))
        mttd = stats.statistic([[r["ttd_s"] / 60] if r["detected"] else [] for r in rows])
        out[v] = {"detectors": s["detectors"], "faults": s["incidents"], "recall": recall.as_dict(),
                  "mttd_min": mttd.as_dict(), "false_alarms_per_day": s["false_alarms_per_day"],
                  "benign_release_alarms_per_day": s["benign_release_alarms_per_day"],
                  "quiet_hours": s.get("quiet_hours"), "alerts_per_incident": s["alerts_per_incident"],
                  "incidents_per_incident": s["incidents_per_incident"], "noise_reduction": s["noise_reduction"],
                  "per_fault": s["per_fault"]}
    return out


# --- triage --------------------------------------------------------------------

def _shares(groups: dict[int, list[dict]], key: str, *, of_all: bool = False) -> stats.Estimate:
    num, den = [], []
    for runs in groups.values():
        counted = runs if of_all else [r for r in runs if r["detected"]]
        num.append(sum(bool(r.get(key)) for r in counted))
        den.append(len(counted))
    return stats.ratio(num, den)


def triage_report(rows: list[dict]) -> dict[str, Any]:
    groups = _by_seq(rows)
    triaged = [r for r in rows if r["detected"]]
    seeds = sorted({r["seed"] for r in rows})
    per_seed = {}
    for s in seeds:
        mine = [r for r in rows if r["seed"] == s]
        per_seed[str(s)] = sum(bool(r.get("top1")) for r in mine) / len(mine) if mine else None
    # do the seeds agree? same first hypothesis and same action on every seed
    agree, comparable = 0, 0
    for runs in groups.values():
        answers = {(json.dumps(r.get("verdict_top") or {}, sort_keys=True), r.get("verdict_action"))
                   for r in runs if r["detected"]}
        if len([r for r in runs if r["detected"]]) > 1:
            comparable += 1
            agree += len(answers) == 1
    paid = [r for r in triaged if r.get("cost_usd")]
    return {
        "incidents": len(groups), "seeds": seeds, "runs": len(rows), "triaged_runs": len(triaged),
        "top1": _shares(groups, "top1").as_dict(), "top3": _shares(groups, "top3").as_dict(),
        "action": _shares(groups, "action").as_dict(), "valid": _shares(groups, "valid").as_dict(),
        "top1_end_to_end": _shares(groups, "top1", of_all=True).as_dict(),
        "unsafe": sum(bool(r.get("unsafe")) for r in triaged),
        "unsafe_rate": _shares(groups, "unsafe").as_dict(),
        "cost_mean_usd": stats.statistic([[r["cost_usd"] for r in g if r["detected"]] for g in groups.values()],
                                         fn=lambda a: float(a.mean())).as_dict(),
        "cost_total_usd": sum(r.get("cost_usd", 0.0) for r in triaged),
        "paid_runs": len(paid),
        "seconds_median": stats.statistic([[r["seconds"] for r in g if r["detected"]]
                                           for g in groups.values()]).as_dict(),
        "per_seed_top1_end_to_end": per_seed,
        "seed_agreement": agree / comparable if comparable else None,
    }


def paired_outcomes(a: list[dict], b: list[dict], key: str, seed: int | None = None) -> tuple[list[bool], list[bool]]:
    ga, gb = _by_seq([r for r in a if seed is None or r["seed"] == seed]), \
        _by_seq([r for r in b if seed is None or r["seed"] == seed])
    seqs = sorted(set(ga) & set(gb))
    return ([stats.majority([bool(r.get(key)) for r in ga[s]]) for s in seqs],
            [stats.majority([bool(r.get(key)) for r in gb[s]]) for s in seqs])


def mcnemar_report(a1: list[dict], b2: list[dict]) -> dict[str, Any]:
    out = {}
    for key in ("top1", "action"):
        x, y = paired_outcomes(a1, b2, key)
        out[key] = stats.mcnemar(x, y).as_dict()
    seeds = sorted({r["seed"] for r in a1} & {r["seed"] for r in b2})
    out["per_seed_top1"] = {}
    if len(seeds) > 1:  # with one seed the pooled test is already the per-seed one
        for s in seeds:
            x, y = paired_outcomes(a1, b2, "top1", seed=s)
            out["per_seed_top1"][str(s)] = stats.mcnemar(x, y).as_dict()
    return out


def _cluster(rows: list[dict], key: str) -> tuple[dict[int, int], dict[int, int]]:
    num, den = {}, {}
    for seq, runs in _by_seq(rows).items():
        num[seq] = sum(bool(r.get(key)) for r in runs)
        den[seq] = len(runs)
    return num, den


def ablation_report(triage: dict[str, list[dict]]) -> dict[str, Any]:
    if "A1" not in triage:
        return {}
    out = {}
    for v, name in ABLATIONS.items():
        if v not in triage:
            continue
        entry = {}
        for key in ("top1", "action"):
            na, da = _cluster(triage[v], key)
            nb, db = _cluster(triage["A1"], key)
            seqs = sorted(set(da) & set(db))
            entry["top1_end_to_end" if key == "top1" else key] = stats.paired_difference(
                [na[s] for s in seqs], [da[s] for s in seqs], [nb[s] for s in seqs], [db[s] for s in seqs]).as_dict()
        out[name] = entry
    return out


def confusion(rows: list[dict], items: dict[int, dict]) -> dict[str, Any]:
    cols = list(CATEGORIES) + [NO_ANSWER, MISSED]
    counts: dict[str, Counter] = {}
    expected = {}
    for r in rows:
        item = items.get(r["seq"])
        if item is None:
            continue
        fault = item["fault"]
        expected[fault] = item["root_cause"]["category"]
        if not r["detected"]:
            col = MISSED
        else:
            col = (r.get("verdict_top") or {}).get("category") or NO_ANSWER
        counts.setdefault(fault, Counter())[col] += 1
    faults = sorted(counts)
    return {"rows": faults, "cols": cols, "counts": {f: dict(c) for f, c in counts.items()}, "expected": expected}


def action_matrix(rows: list[dict], items: dict[int, dict]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter] = {}
    for r in rows:
        item = items.get(r["seq"])
        if item is None:
            continue
        col = r.get("verdict_action") if r["detected"] else MISSED
        out.setdefault(item["correct_action"], Counter())[col or NO_ANSWER] += 1
    return {k: dict(v) for k, v in sorted(out.items())}


def per_fault(triage: dict[str, list[dict]], items: dict[int, dict]) -> dict[str, dict[str, dict[str, int]]]:
    out: dict[str, dict[str, dict[str, int]]] = {}
    for v, rows in triage.items():
        for r in rows:
            fault = items.get(r["seq"], {}).get("fault", r.get("fault"))
            p = out.setdefault(fault, {}).setdefault(v, {"runs": 0, "detected": 0, "top1": 0, "action": 0, "unsafe": 0})
            p["runs"] += 1
            if r["detected"]:
                p["detected"] += 1
                for k in ("top1", "action", "unsafe"):
                    p[k] += int(bool(r.get(k)))
    return dict(sorted(out.items()))


# --- targets -------------------------------------------------------------------

# (metric, target text, where the number is, "ge" or "le", threshold)
TARGETS = (
    ("Detection recall (10 min)", "≥ 90%", ("detection", "recall"), "ge", 0.90),
    ("MTTD", "< 2 min", ("detection", "mttd_min"), "le", 2.0),
    ("False alarms / 24 h fault-free", "< 1", ("detection", "false_alarms_per_day"), "le", 1.0),
    ("Alert noise after correlation", "−50%", ("detection", "noise_reduction"), "ge", 0.50),
    ("RCA top-1", "≥ 70%", ("triage", "top1"), "ge", 0.70),
    ("RCA top-3", "≥ 90%", ("triage", "top3"), "ge", 0.90),
    ("Action accuracy", "≥ 80%", ("triage", "action"), "ge", 0.80),
    ("Unsafe actions", "0", ("triage", "unsafe"), "le", 0),
    ("Cost per incident", "< $0.05", ("triage", "cost_mean_usd"), "le", 0.05),
    ("Triage latency (median)", "< 60 s", ("triage", "seconds_median"), "le", 60.0),
)


def _value(x: Any) -> tuple[float | None, float | None, float | None]:
    if isinstance(x, dict):
        return x.get("value"), x.get("low"), x.get("high")
    return x, None, None


def status(x: Any, op: str, threshold: float) -> str:
    v, low, high = _value(x)
    if v is None:
        return "–"
    ok = v >= threshold if op == "ge" else v <= threshold
    if op == "le" and threshold == 0:
        return "met" if ok else "missed"
    spans = low is not None and high is not None and low < threshold < high
    return ("met" if ok else "missed") + (" (CI spans it)" if spans else "")


def targets(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, text, (part, key), op, thr in TARGETS:
        cells = {}
        if part == "detection":
            for v in DETECTION:
                if v in report["detection"]:
                    cells[v] = report["detection"][v][key]
            judged = cells.get("B2")  # A1 detects with B2's detectors
        else:
            for v in ("B2", "A1"):
                if v in report["triage"]:
                    cells[v] = report["triage"][v][key]
            judged = cells.get("A1", cells.get("B2"))
        out.append({"metric": name, "target": text, "values": cells, "status": status(judged, op, thr)})
    mttr = report.get("mttr")
    if mttr and mttr.get("change") is not None:
        out.append({"metric": "MTTR (live test)", "target": "−50%", "values": {"A1": mttr["change"]},
                    "status": status(mttr["change"], "le", -0.5)})
    return out


# --- the report ------------------------------------------------------------------

def mttr_report(path: Path | None) -> dict[str, Any] | None:
    if not path or not Path(path).exists():
        return None
    runs = json.loads(Path(path).read_text(encoding="utf-8"))
    medians = [s.get("median_s") for s in runs.values()]
    change = None
    if len(medians) == 2 and medians[0] is not None and medians[1]:
        change = (medians[0] - medians[1]) / medians[1]
    return {"runs": runs, "change": change}


def build(labels_dir: Path, set_name: str, *, run: str, lock: dict[str, Any] | None = None,
          mttr: Path | None = None) -> dict[str, Any]:
    split = json.loads((labels_dir / "split.json").read_text(encoding="utf-8"))
    items = {i["seq"]: i for i in split[set_name] if i["kind"] == FAULT}
    det_scores, triage = load_scores(labels_dir, set_name)
    report: dict[str, Any] = {
        "run": run, "set": set_name,
        "faults": len(items),
        "gate": None if lock is None else {k: lock.get(k) for k in ("opened_at", "completed_at", "spent_usd",
                                                                    "dataset_lock_sha256", "seeds", "variants")},
        "prompts": None if lock is None else lock.get("prompts"),
        "detection": detection_report(det_scores) if det_scores else {},
        "triage": {v: triage_report(rows) for v, rows in triage.items()},
        "mcnemar": mcnemar_report(triage["A1"], triage["B2"]) if "A1" in triage and "B2" in triage else None,
        "ablations": ablation_report(triage),
        "confusion": {v: confusion(rows, items) for v, rows in triage.items() if v in ("A1", "B2")},
        "actions": {v: action_matrix(rows, items) for v, rows in triage.items() if v in ("A1", "B2")},
        "per_fault": per_fault(triage, items),
        "mttr": mttr_report(mttr),
    }
    report["targets"] = targets(report)
    report["wrong"] = what_went_wrong(report)
    return report


def what_went_wrong(report: dict[str, Any]) -> list[str]:
    """The fault types the main variant gets wrong most: the starting points for the write-up."""
    v = next((x for x in ("A1", "B2") if x in report["confusion"]), None)
    out = []
    if v is None:
        return out
    m = report["confusion"][v]
    for fault in m["rows"]:
        c = Counter(m["counts"][fault])
        total, right = sum(c.values()), c.get(m["expected"].get(fault), 0)
        if total and right / total < 0.5:
            wrong = [(k, n) for k, n in c.most_common() if k != m["expected"].get(fault)]
            named = ", ".join(f"{k} {n}" for k, n in wrong[:2])
            out.append(f"{fault} (labelled {m['expected'][fault]}): {v} right in {right} of {total} runs; "
                       f"instead: {named}")
    return out


# --- markdown --------------------------------------------------------------------

def _pct(e: Any, signed: bool = False) -> str:
    v, low, high = _value(e)
    if v is None:
        return "–"
    f = "{:+.0%}" if signed else "{:.0%}"
    text = f.format(v)
    if low is not None and high is not None:
        text += f" ({f.format(low)} to {f.format(high)})"
    return text


def _num(e: Any, unit: str = "", digits: int = 1) -> str:
    v, low, high = _value(e)
    if v is None:
        return "–"
    text = f"{v:.{digits}f}{unit}"
    if low is not None and high is not None:
        text += f" ({low:.{digits}f} to {high:.{digits}f})"
    return text


def _usd(e: Any) -> str:
    v, low, high = _value(e)
    if v is None:
        return "–"
    if v == 0 and not high:
        return "$0 (no model)"
    text = f"${v:.4f}"
    return text + (f" (${low:.4f} to ${high:.4f})" if low is not None and high is not None and high > 0 else "")


def _p(p: float) -> str:
    return "< 0.001" if p < 0.001 else f"{p:.3f}"


def _cell(v: Any, key: str) -> str:
    if key in ("mttd_min",):
        return _num(v, " min")
    if key == "false_alarms_per_day":
        return _num(v, "", 2)
    if key == "cost_mean_usd":
        return _usd(v)
    if key == "seconds_median":
        return _num(v, " s", 0)
    if key == "unsafe":
        return "–" if v is None else str(v)
    return _pct(v)


def markdown(report: dict[str, Any], figures: str | None = "figures") -> str:
    set_name = report["set"]
    L: list[str] = []
    add = L.append
    add(f"# Results: {'locked test set' if set_name == 'test' else 'tuning set (not a result)'}")
    add("")
    add("Written by `make results` from the scores of `harness final`; do not edit by hand. "
        "Method and targets: [docs/design.md](../docs/design.md#6-evaluation-plan). "
        "Brackets are 95% bootstrap confidence intervals over incidents.")
    add("")
    g = report.get("gate")
    add(f"- dataset run `{report['run']}`, {report['faults']} faults in the {set_name} set")
    if g:
        add(f"- gate G3: test set opened {g['opened_at']}, runs completed {g['completed_at'] or '(not yet)'}, "
            f"seeds {', '.join(map(str, g.get('seeds') or []))}, model spend ${g.get('spent_usd') or 0:.2f}")
        add(f"- frozen with the G2 lock `{(g['dataset_lock_sha256'] or '')[:12]}`; the G3 lock "
            f"(`eval/dataset/{report['run']}.g3.json`) holds the hash of every prompt, model, detector and scorer")
    if set_name != "test":
        add("- **tuning set**: these numbers guided the tuning and are not the evaluation")
    add("")

    add("## Against the targets")
    add("")
    add("Targets were fixed before anything ran. Detection is judged on B2's detectors (A1 uses the same); "
        "triage on A1. \"CI spans it\": the interval includes the target, so the data cannot tell.")
    add("")
    cols = ["B0", "B1", "B2", "A1"]
    add("| Metric | Target | " + " | ".join(cols) + " | Verdict |")
    add("|---|---|" + "---|" * len(cols) + "---|")
    keys = {name: key for name, _, (_, key), _, _ in TARGETS}
    for t in report["targets"]:
        key = keys.get(t["metric"], "noise_reduction")
        cells = []
        for c in cols:
            if c in t["values"]:
                cells.append(_pct(t["values"][c], signed=True) if t["metric"].startswith("MTTR") else _cell(t["values"][c], key))
            elif c == "A1" and keys.get(t["metric"]) in ("recall", "mttd_min", "false_alarms_per_day", "noise_reduction"):
                cells.append("as B2")
            else:
                cells.append("")
        add(f"| {t['metric']} | {t['target']} | " + " | ".join(cells) + f" | {t['status']} |")
    add("")

    det = report["detection"]
    if det:
        add("## Detection")
        add("")
        if figures:
            add(f"![Detection recall]({figures}/detection-recall.svg) ![MTTD]({figures}/detection-mttd.svg)")
            add("")
        add("| Variant | Detectors | Recall (10 min) | MTTD | False alarms / 24 h | Benign-release alarms / 24 h | "
            "Alerts per incident | After correlation | Noise reduction |")
        add("|---|---|---|---|---|---|---|---|---|")
        for v, d in det.items():
            add(f"| {v} | {' + '.join(d['detectors'])} | {_pct(d['recall'])} | {_num(d['mttd_min'], ' min')} | "
                f"{_num(d['false_alarms_per_day'], '', 2)} | {_num(d['benign_release_alarms_per_day'], '', 2)} | "
                f"{_num(d['alerts_per_incident'])} | {_num(d['incidents_per_incident'])} | {_pct(d['noise_reduction'])} |")
        hours = next(iter(det.values())).get("quiet_hours") or {}
        if hours:
            add("")
            add(f"False alarms are counted over {hours.get('fault-free', 0):.0f} h of fault-free traffic and "
                f"{hours.get('benign-release', 0):.1f} h of benign releases; they have no CI (too few windows).")
        add("")
        faults = sorted({f for d in det.values() for f in d["per_fault"]})
        if faults:
            add("Detected per fault type (median minutes to detect)")
            add("")
            add("| Fault | " + " | ".join(det) + " |")
            add("|---|" + "---|" * len(det))
        for f in faults:
            cells = []
            for d in det.values():
                p = d["per_fault"].get(f)
                cells.append("–" if not p else f"{p['detected']}/{p['of']}" +
                             (f" ({p['mttd_s'] / 60:.1f})" if p.get("mttd_s") is not None else ""))
            add(f"| {f} | " + " | ".join(cells) + " |")
        add("")

    tri = report["triage"]
    if tri:
        add("## Triage")
        add("")
        add("Each fault is triaged as the first incident the detectors (burn-rate + anomaly) opened within 10 "
            "minutes of its start. The first five columns are over those incidents; top-1 end to end counts the "
            "detector's misses as wrong.")
        add("")
        if figures:
            add(f"![Triage scores]({figures}/triage.svg)")
            add("")
        add("| Variant | Runs triaged | RCA top-1 | RCA top-3 | Action | Valid | Top-1 end to end | Unsafe | "
            "Cost / incident | Median time |")
        add("|---|---|---|---|---|---|---|---|---|---|")
        for v, t in tri.items():
            add(f"| {v} | {t['triaged_runs']}/{t['runs']} | {_pct(t['top1'])} | {_pct(t['top3'])} | {_pct(t['action'])} | "
                f"{_pct(t['valid'])} | {_pct(t['top1_end_to_end'])} | {t['unsafe']} | {_usd(t['cost_mean_usd'])} | "
                f"{_num(t['seconds_median'], ' s', 0)} |")
        add("")
        add("Seeds: top-1 end to end per seed, and the share of incidents where every seed gave the same first "
            "hypothesis and action")
        add("")
        add("| Variant | Per seed | Seeds agree | Model spend |")
        add("|---|---|---|---|")
        for v, t in tri.items():
            seeds = ", ".join(f"s{s} {x:.0%}" for s, x in t["per_seed_top1_end_to_end"].items() if x is not None)
            agree = "–" if t["seed_agreement"] is None else f"{t['seed_agreement']:.0%}"
            add(f"| {v} | {seeds} | {agree} | ${t['cost_total_usd']:.2f} |")
        add("")

    mc = report.get("mcnemar")
    if mc:
        add("## A1 against B2")
        add("")
        add("McNemar's exact test, two-sided, over the same incidents. An incident is right for a variant when "
            "most of its seeds got it right. Only the incidents where the two disagree carry information.")
        add("")
        add("| Score | Only A1 right | Only B2 right | Both | Neither | p |")
        add("|---|---|---|---|---|---|")
        for key, name in (("top1", "RCA top-1 (end to end)"), ("action", "Action")):
            m = mc[key]
            add(f"| {name} | {m['a_only']} | {m['b_only']} | {m['both']} | {m['neither']} | {_p(m['p_value'])} |")
        for s, m in mc.get("per_seed_top1", {}).items():
            add(f"| top-1, seed {s} only | {m['a_only']} | {m['b_only']} | {m['both']} | {m['neither']} | {_p(m['p_value'])} |")
        add("")

    abl = report.get("ablations")
    if abl:
        add("## Ablations")
        add("")
        add("Each variant minus A1 on the same incidents (paired bootstrap). Negative: the part removed was worth "
            "something.")
        add("")
        if figures:
            add(f"![Ablations]({figures}/ablations.svg)")
            add("")
        add("| Variant | RCA top-1 end to end | Action |")
        add("|---|---|---|")
        for name, a in abl.items():
            add(f"| {name} | {_pct(a['top1_end_to_end'], signed=True)} | {_pct(a['action'], signed=True)} |")
        add("")

    conf = report.get("confusion") or {}
    if conf:
        add("## By fault type")
        add("")
        add("The category of the first hypothesis, per labelled fault, counted over runs (seeds included). "
            "**Bold**: the labelled category.")
        add("")
        for v, m in conf.items():
            if figures:
                add(f"![{v} confusion]({figures}/confusion-{v}.svg)")
                add("")
            add(f"{v}")
            add("")
            add("| Fault | " + " | ".join(m["cols"]) + " |")
            add("|---|" + "---|" * len(m["cols"]))
            for f in m["rows"]:
                cells = []
                for c in m["cols"]:
                    n = m["counts"][f].get(c, 0)
                    cells.append(f"**{n}**" if m["expected"].get(f) == c else (str(n) if n else ""))
                add(f"| {f} | " + " | ".join(cells) + " |")
            add("")
        for v, a in (report.get("actions") or {}).items():
            cols = list(ACTIONS) + [MISSED]
            add(f"{v}: proposed action (columns) against the labelled one (rows)")
            add("")
            add("| Labelled | " + " | ".join(cols) + " |")
            add("|---|" + "---|" * len(cols))
            for lab, c in a.items():
                cells = [(f"**{c.get(x, 0)}**" if x == lab else (str(c.get(x, 0)) if c.get(x) else "")) for x in cols]
                add(f"| {lab} | " + " | ".join(cells) + " |")
            add("")
        pf = report.get("per_fault") or {}
        if pf:
            variants = list(report["triage"])
            add("Top-1 / action right, of the runs the detectors caught (unsafe actions noted)")
            add("")
            add("| Fault | " + " | ".join(variants) + " |")
            add("|---|" + "---|" * len(variants))
            for f, by in pf.items():
                cells = []
                for v in variants:
                    p = by.get(v)
                    cells.append("–" if not p else f"{p['top1']}/{p['action']} of {p['detected']}" +
                                 (f", {p['unsafe']} unsafe" if p["unsafe"] else ""))
                add(f"| {f} | " + " | ".join(cells) + " |")
            add("")

    if report.get("wrong"):
        add("## What did not work")
        add("")
        add("Fault types the main variant got right in fewer than half its runs. The why belongs in the "
            "write-up (week 11), from the runs in `eval/runs/`.")
        add("")
        for w in report["wrong"]:
            add(f"- {w}")
        add("")

    mt = report.get("mttr")
    if mt:
        add("## Time to recover (live test)")
        add("")
        add("A separate live test of about ten incidents on kind, not the replayed test set (week 9).")
        add("")
        add("| Run | Faults | Recovered | Median | Mean |")
        add("|---|---|---|---|---|")
        for name, s in mt["runs"].items():
            m = lambda v: "–" if v is None else f"{v / 60:.1f} min"  # noqa: E731
            add(f"| {name} | {s['faults']} | {s['recovered']} | {m(s['median_s'])} | {m(s['mean_s'])} |")
        if mt["change"] is not None:
            add("")
            add(f"Median change: {mt['change']:+.0%} (target −50%).")
        add("")

    if report.get("prompts"):
        add("## What was frozen")
        add("")
        add("| Variant | Model | Traces | Runbooks | System prompt |")
        add("|---|---|---|---|---|")
        for v, p in report["prompts"].items():
            add(f"| {v} | {p['model']} | {'yes' if p.get('traces', v != 'B2') else 'no'} | "
                f"{'yes' if p.get('runbooks', v != 'B2') else 'no'} | `{p['system_sha256'][:12]}` |")
        add("")
    return "\n".join(L).rstrip() + "\n"


def readme_table(report: dict[str, Any]) -> str:
    det, tri = report["detection"], report["triage"]
    names = {"B0": "B0 static thresholds", "B1": "B1 SLO burn-rate alerts", "B2": "B2 burn-rate + anomaly + rule RCA",
             "A1": "A1 burn-rate + anomaly + LLM agent", "A2": "A2 = A1 without traces",
             "A3": "A3 = A1 without runbooks", "A4": "A4 = A1 on an open-weight model"}
    lines = ["| Variant | Detection recall | MTTD | RCA top-1 | Action accuracy | Cost / incident |",
             "|---|---|---|---|---|---|"]
    for v in ("B0", "B1", "B2", "A1", "A2", "A3", "A4"):
        if v not in det and v not in tri:
            continue
        d = det.get("B2") if v.startswith("A") else det.get(v)  # A variants detect with B2's detectors
        t = tri.get(v)

        def short(e: Any, unit: str = "") -> str:
            val = _value(e)[0]
            if val is None:
                return "–"
            return f"{val:.0%}" if unit == "" else f"{val:.1f} {unit}"
        cost = "–" if not t else ("$0" if not t["cost_total_usd"] else f"${t['cost_mean_usd']['value']:.3f}")
        lines.append(f"| {names[v]} | {short(d['recall']) if d else '–'} | {short(d['mttd_min'], 'min') if d else '–'} | "
                     f"{short(t['top1']) if t else '–'} | {short(t['action']) if t else '–'} | {cost} |")
    return "\n".join(lines)


def update_readme(path: Path, report: dict[str, Any]) -> bool:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(README_START) + r".*?" + re.escape(README_END), re.S)
    if not pattern.search(text):
        return False
    block = (f"{README_START}\nMeasured on the locked test set ({report['faults']} incidents, 3 seeds); "
             f"95% intervals, significance tests and the confusion matrix in [eval/results.md](eval/results.md).\n\n"
             f"{readme_table(report)}\n{README_END}")
    text = pattern.sub(lambda _: block, text)
    wrong = re.compile(re.escape(WRONG_START) + r".*?" + re.escape(WRONG_END), re.S)
    if wrong.search(text):
        v = next((x for x in ("A1", "B2") if x in report["confusion"]), "A1")
        items = "\n".join(f"- {w}" for w in report["wrong"]) or f"- {v} got every fault type right in most runs."
        body = (f"{WRONG_START}\nFault types {v} got wrong in at least half its runs on the test set "
                f"([by fault type](eval/results.md#by-fault-type)):\n\n{items}\n{WRONG_END}")
        text = wrong.sub(lambda _: body, text)
    path.write_text(text, encoding="utf-8")
    return True
