"""`make postmortem`: draft a blameless postmortem from what the live test recorded.

Week 11. The plan asks for three postmortems in the Google SRE format, for the
most interesting incidents. Most of a postmortem is facts that already exist:
when the fault started and what it was (the run's sealed labels), which alerts
fired and when, what the agent concluded and from which evidence, what was
proposed, who approved it, what ran and when it recovered (the live store and
its audit log). This writes those parts and leaves the parts only a person can
write (summary, impact on users, the analysis, lessons, action items) as gaps
marked "_To write_".

  harness postmortem --run LIVE --list           every fault of a live-test run, and why each is worth a look
  harness postmortem --run LIVE --seq 3          docs/postmortems/<date>-<fault>-<target>.md

Only live-test runs (`make live-test` with `make console` running): their
labels are not part of the frozen dataset, and only they have remediation.
Approvers' names are in the audit log; the draft says "the operator" unless
--keep-names, because a blameless postmortem is about the system.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_runner.catalog import REPO_ROOT, Catalog
from chaos_runner.labels import read
from chaos_runner.schedule import FAULT
from detector.alerts import parse_time
from loop.api import grafana_links
from remediator.store import Store

from .triage import DETECT_WITHIN_S, judge

OUT_DIR = REPO_ROOT / "docs" / "postmortems"
TODO = "_To write_"
SYSTEM_ACTORS = {"loop", "agent", "rule-baseline", "policy", "remediator"}


class PostmortemError(ValueError):
    pass


# --- gathering -----------------------------------------------------------------------

def cases(labels_path: Path, store: Store) -> list[dict[str, Any]]:
    """One case per injected fault: its label and the incidents the loop opened for it.

    An incident belongs to the latest fault that started before it opened and
    had not yet recovered (or started at most 10 minutes before). An incident no
    fault explains is a false alarm and gets a case of its own."""
    faults = [r for r in read(labels_path) if r.get("kind") == FAULT and not r.get("aborted")]
    incidents = sorted(store.incidents(limit=10_000), key=lambda i: i["opened"])
    out = [{"label": f, "incidents": []} for f in faults]
    for inc in incidents:
        owner = None
        for case in out:
            f = case["label"]
            if f is None:
                continue
            start = parse_time(f["start"])
            until = max(start + DETECT_WITHIN_S, parse_time(f.get("recovered") or f.get("end")) or start)
            if start <= inc["opened"] <= until and (owner is None or start > parse_time(owner["label"]["start"])):
                owner = case
        if owner is not None:
            owner["incidents"].append(inc)
        else:
            out.append({"label": None, "incidents": [inc]})
    for case in out:
        ids = [i["id"] for i in case["incidents"]]
        case["audit"] = [e for i in ids for e in store.audit_log(i, limit=10_000)]
        case["audit"].sort(key=lambda e: e["seq"])
        case["proposals"] = [p for i in ids for p in store.proposals(i)]
        case["judged"] = _judge(case)
    return out


def _judge(case: dict[str, Any]) -> dict[str, Any] | None:
    first = next((i for i in case["incidents"] if i.get("verdict")), None)
    if case["label"] is None or first is None:
        return None
    return judge(first["verdict"], case["label"])


def recover_s(case: dict[str, Any]) -> float | None:
    f = case["label"]
    if f is None or not case["incidents"]:
        return None
    closed = [i["closed"] for i in case["incidents"] if i.get("closed")]
    if len(closed) < len(case["incidents"]):
        return None
    return max(closed) - parse_time(f["start"])


def reasons(case: dict[str, Any], all_cases: list[dict[str, Any]]) -> list[str]:
    """Why a case might make a good postmortem: something went wrong, or nearly did."""
    f, incs, j = case["label"], case["incidents"], case["judged"]
    out = []
    if f is None:
        return ["false alarm: no fault explains it"]
    if not incs:
        return ["missed: no incident opened"]
    if len(incs) > 1:
        out.append(f"split into {len(incs)} incidents")
    first = incs[0]
    if first["opened"] - parse_time(f["start"]) > 300:
        out.append(f"slow detection: {(first['opened'] - parse_time(f['start'])) / 60:.1f} min")
    if j is not None:
        if not j["top1"]:
            h = (first["verdict"].get("hypotheses") or [{}])[0]
            out.append(f"wrong root cause: {h.get('component')} / {h.get('category')}")
        if not j["action"]:
            out.append(f"wrong action: {first['verdict'].get('action')} (labelled {f['correct_action']})")
        if j["unsafe"]:
            out.append("unsafe proposal")
    for p in case["proposals"]:
        if p["status"] in ("rejected", "failed", "not-run", "refused"):
            out.append(f"{p['action']} {p['status']}")
    if any(e["event"] == "escalated" for e in case["audit"]):
        out.append("escalated to a human")
    times = [t for c in all_cases if (t := recover_s(c)) is not None]
    mine = recover_s(case)
    if mine is None:
        out.append("did not recover while the loop watched")
    elif len(times) >= 3 and mine > 1.5 * statistics.median(times):
        out.append(f"slow recovery: {mine / 60:.1f} min (median {statistics.median(times) / 60:.1f})")
    return out


def listing(all_cases: list[dict[str, Any]]) -> str:
    lines = ["| seq | Fault | Target | Incidents | Root cause | Action | Recovered after | Worth a look |",
             "|---|---|---|---|---|---|---|---|"]
    for case in all_cases:
        f, j = case["label"], case["judged"]
        t = recover_s(case)
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            f["seq"] if f else "–", f["fault"] if f else "none", (f or {}).get("target", "–"),
            ", ".join(i["id"] for i in case["incidents"]) or "–",
            "–" if j is None else ("right" if j["top1"] else "wrong"),
            "–" if j is None else ("right" if j["action"] else "wrong"),
            "–" if t is None else f"{t / 60:.1f} min",
            "; ".join(reasons(case, all_cases)) or "–"))
    return "\n".join(lines) + "\n"


# --- the draft -----------------------------------------------------------------------

def _hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


def _offset(ts: float, t0: float) -> str:
    s = round(ts - t0)
    sign = "−" if s < 0 else "+"
    return f"{sign}{abs(s) // 60}:{abs(s) % 60:02d}"


def _actor(name: str, keep_names: bool) -> str:
    return name if keep_names or name in SYSTEM_ACTORS else "the operator"


def _event_text(e: dict[str, Any], keep_names: bool) -> str:
    d, ev = e["data"], e["event"]
    who = _actor(e["actor"], keep_names)
    if ev == "incident-opened":
        return f"incident {e['incident']} opened on {', '.join(d.get('components') or []) or '?'}"
    if ev == "triaged":
        return (f"triage ({d.get('variant', '?')}): root cause {d.get('root_cause', '?')}, "
                f"proposes {d.get('action', '?')} {d.get('target') or ''}".rstrip())
    if ev == "proposed":
        need = "no approval (a canary in progress)" if d.get("mode") == "auto" else "one approval"
        return f"proposal {d.get('proposal')}, needs {need}"
    if ev in ("approved", "rejected"):
        extra = f" ({d['reason']})" if d.get("reason") else ""
        return f"{who} {ev} proposal {d.get('proposal')}{extra}"
    if ev in ("executed", "failed", "not-run", "unplannable", "refused", "escalated"):
        extra = d.get("reason") or d.get("action") or ""
        return ev + (f" ({extra})" if extra else "")
    if ev == "resolved":
        return f"incident {e['incident']} resolved: every alert cleared"
    return ev


def timeline(case: dict[str, Any], keep_names: bool = False) -> list[tuple[float, str, str]]:
    f = case["label"]
    rows: list[tuple[float, str, str]] = []
    if f is not None:
        what = f"{f['fault']} {f['fault_name']} injected on {f['target']}"
        if f.get("intensity"):
            what += f" at {f['intensity']}"
        rows.append((parse_time(f["start"]), "chaos-runner", what))
        if f.get("symptom_seen"):
            rows.append((parse_time(f["symptom_seen"]), "chaos-runner", "the fault's symptom query is true"))
        if f.get("end"):
            rows.append((parse_time(f["end"]), "chaos-runner", "fault removed (its planned duration ended)"))
        if f.get("recovered"):
            rows.append((parse_time(f["recovered"]), "chaos-runner", "health checks pass and the symptom is gone"))
    seen = set()
    for inc in case["incidents"]:
        for a in inc.get("alerts") or []:
            key = (a.get("detector"), a.get("name"), a.get("component"), a.get("start"))
            if key in seen or not a.get("start"):
                continue
            seen.add(key)
            rows.append((parse_time(a["start"]), a.get("detector") or "detector",
                         f"alert {a.get('name')} on {a.get('component')}"))
    for e in case["audit"]:
        rows.append((e["at"], _actor(e["actor"], keep_names), _event_text(e, keep_names)))
    rows.sort(key=lambda r: r[0])
    return rows


def _md(text: Any) -> str:
    return str(text if text is not None else "–").replace("|", "\\|").replace("\n", " ")


def draft(case: dict[str, Any], *, run: str, catalog: Catalog, grafana: str = "http://localhost:3000",
          keep_names: bool = False, chain: str | None = None) -> str:
    f, incs, j = case["label"], case["incidents"], case["judged"]
    if f is None:
        raise PostmortemError("a false alarm has no fault to write up; pick a case with a seq")
    fault = catalog.fault(f["fault"])
    t0 = parse_time(f["start"])
    day = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d")
    rec = recover_s(case)
    first_alert = min((parse_time(a["start"]) for i in incs for a in (i.get("alerts") or []) if a.get("start")),
                      default=None)
    triaged = next((e for e in case["audit"] if e["event"] == "triaged"), None)
    verdict = next((i["verdict"] for i in incs if i.get("verdict")), None)
    rc = f["root_cause"]
    L: list[str] = []
    add = L.append

    add(f"# Postmortem: {fault.name} in {f['target']}")
    add("")
    add(f"> Draft written by `make postmortem RUN={run} SEQ={f['seq']}` from the live store and the run's sealed "
        "labels. The sections marked _To write_ need a person. Everything else is recorded data: fix it at the "
        "source, not here. Format: [Google SRE, postmortem culture](https://sre.google/sre-book/example-postmortem/).")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Date | {day} |")
    add(f"| Authors | {TODO} |")
    add("| Status | Draft |")
    add(f"| Incident | {', '.join(i['id'] for i in incs) or 'none opened'} (live test `{run}`, fault {f['seq']}) |")
    intensity = f", intensity {f['intensity']}" if f.get("intensity") else ""
    add(f"| Fault | {fault.id} {fault.name}, on {f['target']}{intensity}; injected on purpose |")
    add(f"| Detected | {'not detected' if first_alert is None else _offset(first_alert, t0) + ' after the fault started'} |")
    add(f"| Recovered | {'not while the loop watched' if rec is None else f'{rec / 60:.1f} min after the fault started (its incidents closed)'} |")
    add("")

    add("## Summary")
    add("")
    add(f"{TODO}: two or three sentences. What broke, how it was noticed, how it was fixed.")
    add("")

    add("## Impact")
    add("")
    slos = sorted({a["labels"].get("sloth_slo") for i in incs for a in (i.get("alerts") or [])
                   if a.get("detector") == "burn-rate" and (a.get("labels") or {}).get("sloth_slo")})
    add(f"- SLOs whose error budget burned fast enough to alert: {', '.join(slos) if slos else 'none'}.")
    if rec is not None:
        add(f"- From the fault's start to recovery: {rec / 60:.1f} min.")
    add(f"- {TODO}: what users saw, from the SLO dashboard below (failed or slow requests, budget used).")
    add("")

    add("## Root causes")
    add("")
    add(f"Ground truth (sealed label): **{rc['component']} / {rc['category']}**; the right action is "
        f"**{f['correct_action']}**.")
    add("")
    if verdict:
        variant = f" ({triaged['data'].get('variant')})" if triaged else ""
        matches = "matching" if j and j["top1"] else "not matching"
        add(f"What the triage concluded{variant}, {matches} the label at rank 1:")
        add("")
        add("| Rank | Component / category | Confidence | Reason |")
        add("|---|---|---|---|")
        for n, h in enumerate(verdict.get("hypotheses") or [], start=1):
            add(f"| {n} | {_md(h.get('component'))} / {_md(h.get('category'))} | {_md(h.get('confidence'))} | "
                f"{_md(h.get('reason'))} |")
        add("")
        if verdict.get("summary"):
            add(f"Its summary: \"{verdict['summary']}\"")
            add("")
    else:
        add("No triage verdict was recorded.")
        add("")
    add(f"{TODO}: the mechanism. Why this change caused this symptom, and why nothing stopped it earlier "
        f"(canary analysis, tests, limits).")
    add("")

    add("## Trigger")
    add("")
    details = f.get("details") or {}
    if details.get("release") == "image":
        add(f"A release of {f['target']} with image tag `{details.get('tag')}`, promoted past the canary as a "
            "hotfix would be.")
    elif details.get("release") == "config":
        params = ", ".join(f"`{k}={v}`" for k, v in (details.get("params") or {}).items())
        add(f"A configuration release of {f['target']}: {params}.")
    elif fault.experiment:
        add(f"A Chaos Mesh experiment ([chaos/experiments/{fault.experiment}](../../chaos/experiments/{fault.experiment}))"
            f" on {f['target']}.")
    else:
        add(f"Injected with `{fault.injector}` on {f['target']}.")
    add("")

    add("## Resolution")
    add("")
    if case["proposals"]:
        for p in case["proposals"]:
            who = _actor(p["decided_by"], keep_names) if p.get("decided_by") else None
            line = f"- {p['action']} {p.get('target') or ''}: {p['mode']}, {p['status']}".replace("  ", " ")
            if who:
                line += f" (decided by {who})"
            add(line)
            for cmd in (p.get("plan") or {}).get("commands") or []:
                add(f"  - `{cmd}`")
    else:
        add("Nothing was proposed; the fault ended when its planned duration did.")
    add("")

    add("## Detection")
    add("")
    if first_alert is None:
        add("No detector fired.")
    else:
        detectors = sorted({a.get("detector") for i in incs for a in (i.get("alerts") or [])})
        add(f"First alert {_offset(first_alert, t0)} after the fault started; detectors that fired: "
            f"{', '.join(d for d in detectors if d)}. Incident opened {_offset(incs[0]['opened'], t0)}"
            + (f", triaged {_offset(triaged['at'], t0)}." if triaged else "."))
    add("")

    add("## Action items")
    add("")
    add("| Action item | Type | Owner | Issue |")
    add("|---|---|---|---|")
    add(f"| {TODO} | mitigate / prevent / detect | | |")
    add("")

    add("## Lessons learned")
    add("")
    for h in ("What went well", "What went wrong", "Where we got lucky"):
        add(f"### {h}")
        add("")
        add(f"{TODO}.")
        add("")
    hints = reasons(case, [case])
    if hints:
        add("Starting points from the data: " + "; ".join(hints) + ".")
        add("")

    add("## Timeline")
    add("")
    add("All times UTC; offsets from the fault's start.")
    add("")
    add("| Time | Offset | Who | Event |")
    add("|---|---|---|---|")
    for ts, who, what in timeline(case, keep_names):
        add(f"| {_hms(ts)} | {_offset(ts, t0)} | {_md(who)} | {_md(what)} |")
    add("")

    add("## Supporting information")
    add("")
    add(f"- Runbook: [docs/runbooks/{fault.runbook}](../runbooks/{fault.runbook})")
    end = max([i["closed"] for i in incs if i.get("closed")] or [parse_time(f.get("recovered") or f.get("end")) or t0])
    for link in grafana_links(grafana, sorted({c for i in incs for c in (i.get("components") or [])}) or [f["target"]],
                              t0, end):
        add(f"- Grafana, {link['title']} (the live cluster's, for this window): <{link['url']}>")
    if verdict and verdict.get("evidence"):
        add("- Evidence the triage collected:")
        add("")
        add("  | Tool | Query | Finding |")
        add("  |---|---|---|")
        for e in verdict["evidence"]:
            add(f"  | {_md(e.get('tool'))} | `{_md(e.get('query'))}` | {_md(e.get('finding'))} |")
        add("")
    if case["audit"]:
        add(f"- Audit log entries {case['audit'][0]['seq']} to {case['audit'][-1]['seq']}"
            + (f"; {chain}" if chain else "") + ".")
    return "\n".join(L).rstrip() + "\n"


def path_for(case: dict[str, Any], out_dir: Path = OUT_DIR) -> Path:
    f = case["label"]
    day = datetime.fromtimestamp(parse_time(f["start"]), timezone.utc).strftime("%Y-%m-%d")
    return out_dir / f"{day}-{f['fault'].lower()}-{f['target'].replace('->', '-')}.md"


def pick(all_cases: list[dict[str, Any]], seq: int) -> dict[str, Any]:
    for case in all_cases:
        if case["label"] is not None and case["label"]["seq"] == seq:
            return case
    raise PostmortemError(f"no fault with seq {seq} in this run; `--list` shows them")
