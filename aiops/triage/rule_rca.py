"""Rule-based RCA, the baseline the agent has to beat (variant B2).

The plan's rule: blame the most upstream service whose error rate rose first.
"Upstream" here is in the direction a failure travels, from callee to caller:
when PostgreSQL fails, the catalog's errors start before the gateway's, so the
deepest component that changed first is blamed. Latency counts as well as
errors (half the fault catalog is slowness), and so do the signals a component
without RED metrics gives: readiness, restarts, saturation, a growing database.

  1. Baseline: each signal's median and MAD over the hour before the incident.
  2. A signal rose at the first point after (opened - 10 min) where it is out of
     its normal range for two samples in a row.
  3. A lost node or DNS outage explains everything and wins. Otherwise the
     earliest rise wins, ties within 30 s going to the deeper component.
  4. A fixed table maps the winner's signals to a category and an action,
     with a release of that service in the last 30 minutes meaning bad-release.

No learning, no tuning to the labels: thresholds are round numbers. Output is
the same Verdict the agent gives, so the same code scores both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np

from detector.alerts import DATA_STORES, SERVICES, Alert
from recorder.record import parse_iso

from .replay import View
from .tools import fmt
from .verdict import Verdict, parse

BASELINE_S = 3600
LEAD_S = 600           # look for rises from this long before the incident opened
RELEASE_S = 1800       # a release this recent counts as the cause
TIE_S = 30
CONTAINERS = SERVICES + DATA_STORES
# Gateway routes and the service behind each.
ROUTES = {"/api/search": "search-api", "/api/products/:id": "catalog"}
NEXT_HOP = {"search-api": "redis", "catalog": "postgres", "indexer": "redis"}


@dataclass(frozen=True)
class Rise:
    t: float
    component: str
    signal: str      # errors | latency | load | unready | restarts | cpu | memory | disk | node | dns | route:<route>
    value: float
    normal: float


def _first_rise(x: np.ndarray, lo: int, hi: int, base: slice, rule: str) -> tuple[int, float] | None:
    b = x[base]
    b = b[~np.isnan(b)]
    if len(b) < 10:
        return None
    med = float(np.median(b))
    mad = 1.4826 * float(np.median(np.abs(b - med)))
    limit = {
        "errors": med + max(4 * mad, 0.01),        # error ratio: at least 1 point above normal
        "latency": max(1.5 * med, med + 4 * mad, med + 0.05),
        "load": 2 * med,
        "drop": med - 0.5,                          # a count of ready things fell by one
        "count": 0.5,                               # a counter went up at all
        "ratio": 0.8,                               # share of a limit
        "memory": 0.9,
        "slope": 50_000.0,                          # bytes per second
    }[rule]
    over = (x > limit) if rule != "drop" else (x < limit)
    over = np.nan_to_num(over.astype(float), nan=0.0).astype(bool)
    for i in range(lo, hi - 1):
        if over[i] and over[i + 1]:
            return i, med
    return None


def rises(view: View, opened: float) -> list[Rise]:
    f = view.frame
    hi = min(len(f), f.index(view.now) + 1)
    lo = max(0, f.index(opened - LEAD_S))
    base = slice(max(0, f.index(opened - LEAD_S - BASELINE_S)), lo)
    out: list[Rise] = []

    def check(component: str, signal: str, x: np.ndarray, rule: str) -> None:
        if (r := _first_rise(x, lo, hi, base, rule)) is not None:
            i, med = r
            out.append(Rise(float(f.times[i]), component, signal, float(x[i]), med))

    with np.errstate(divide="ignore", invalid="ignore"):
        for s in SERVICES:
            job = {"job": f"shopsearch/{s}"}
            req = f.get("requests", **job)
            check(s, "errors", f.get("errors", **job) / req, "errors")
            check(s, "latency", f.get("latency_p95", **job), "latency")
        check("gateway", "load", f.get("requests", job="shopsearch/gateway"), "load")
        for route in ROUTES:
            check("gateway", f"route:{route}", f.get("route_latency_p95", http_route=route), "latency")
        for c in CONTAINERS:
            cont = {"container": c}
            check(c, "unready", f.get("ready", **cont), "drop")
            restarts = f.get("restarts", **cont)
            before = restarts[base][~np.isnan(restarts[base])]
            if len(before):
                check(c, "restarts", restarts - before.min(), "count")
            check(c, "cpu", f.get("cpu", **cont) / f.get("cpu_limit", **cont), "ratio")
            check(c, "memory", f.get("memory", **cont) / f.get("memory_limit", **cont), "memory")
        size = f.sum("db_size")
        check("postgres", "disk", np.gradient(size, f.step) if len(size) > 1 else size, "slope")
        check("node", "node", f.sum("nodes_ready"), "drop")
        check("cluster", "dns", f.sum("coredns_available"), "drop")
    return sorted(out, key=lambda r: r.t)


def recent_releases(view: View, opened: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for _, d in view.section("deploys"):
        for h in d.get("history", []):
            t = parse_iso(h["deployed_at"])
            if opened - RELEASE_S <= t <= view.now:
                out[h["app"]] = max(out.get(h["app"], 0), t)
    return out


def classify(component: str, signals: set[str], releases: dict[str, float]) -> tuple[str, str, str, str | None]:
    """(root-cause component, category, action, action target): the rule table."""
    if component == "node":
        return "node", "infrastructure", "none_verify", None
    if component == "cluster":
        return "cluster", "dns", "none_escalate", None
    if component in SERVICES and component in releases:
        return component, "bad-release", "rollback", component
    if component in DATA_STORES and signals & {"unready", "restarts"}:
        return component, "dependency", "restart", component
    if component == "postgres" and "disk" in signals:
        return "postgres", "resource", "none_escalate", None
    if "memory" in signals:
        return component, "resource", "restart", component
    if "cpu" in signals:
        return (component, "resource", "scale_out", component) if component in SERVICES else (component, "resource", "none_escalate", None)
    if component == "gateway" and "load" in signals:
        return "gateway", "load", "scale_out", "gateway"
    slow = {s for s in signals if s == "latency" or s.startswith("route:")}
    if slow and not signals & {"errors", "unready", "restarts"}:
        if component == "gateway":
            routes = [s.split(":", 1)[1] for s in sorted(slow) if s.startswith("route:")]
            callee = ROUTES.get(routes[0]) if routes else "search-api"
        else:
            callee = NEXT_HOP.get(component)
        if callee:
            return f"{component}->{callee}", "network", "none_escalate", None
    if component in SERVICES and signals & {"restarts", "unready"}:
        return component, "resource", "restart", component
    return component, ("bad-release" if releases else "resource"), "none_escalate", None


def rule_rca(view: View, incident: list[Alert]) -> Verdict:
    opened = min(a.start for a in incident)
    found = rises(view, opened)
    releases = recent_releases(view, opened)
    infra = [r for r in found if r.component in ("node", "cluster")]
    ordered: list[str] = []
    if infra:
        ordered.append(infra[0].component)
    graph = view.graph
    pool = [r for r in found if r.component not in ("node", "cluster")]
    pool.sort(key=lambda r: (round(r.t / TIE_S), -graph.depth(r.component)))
    for r in pool:
        if r.component not in ordered:
            ordered.append(r.component)
    if not ordered:  # nothing out of range: fall back to the incident's first alert
        ordered = [a.component for a in sorted(incident, key=lambda a: a.start) if a.component not in ("unknown",)][:1] or ["gateway"]
    signals = {c: {r.signal for r in found if r.component == c} for c in ordered}
    hyps, seen = [], set()
    for c, conf in zip(ordered, (0.6, 0.25, 0.15)):
        comp, cat, action, target = classify(c, signals[c], releases)
        if (comp, cat) in seen:
            continue
        seen.add((comp, cat))
        first = next((r for r in found if r.component == c), None)
        why = (f"{c} {first.signal} out of its normal range {int(view.now - first.t)}s before triage: {fmt(first.value)} (normal {fmt(first.normal)})"
               if first else f"{c} raised the incident's first alert")
        hyps.append({"component": comp, "category": cat, "confidence": conf, "reason": why,
                     "_action": action, "_target": target})
    top = hyps[0]
    evidence = [{"tool": "rule", "query": f"first rise of {r.component} {r.signal}",
                 "finding": f"at {int(view.now - r.t)}s before triage: {fmt(r.value)} against a normal {fmt(r.normal)}"}
                for r in found[:6]]
    if releases:
        evidence.append({"tool": "rule", "query": "ArgoCD releases in the last 30 min",
                         "finding": ", ".join(f"{a} {int(view.now - t)}s before triage" for a, t in sorted(releases.items()))})
    if not evidence:
        evidence.append({"tool": "rule", "query": "rises", "finding": "no signal out of its normal range"})
    verdict, problems = parse(json.dumps({
        "hypotheses": [{k: v for k, v in h.items() if not k.startswith("_")} for h in hyps],
        "evidence": evidence[:10], "action": top["_action"], "action_target": top["_target"],
        "confidence": top["confidence"],
        "summary": f"Rule baseline: {top['component']} ({top['category']}) changed first; {top['_action']}.",
    }))
    if verdict is None:
        raise AssertionError(f"rule baseline built an invalid verdict: {problems}")
    return verdict
