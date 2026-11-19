"""The agent's tools: read-only, typed, with a timeout and a cap on what they return.

Each tool is a Pydantic model of its arguments plus a function over the replay
View. There is no shell, no kubectl and no free-form query language:

  query_metric     a recorded metric (the PromQL in recorder/queries.yaml), per series, over the last minutes
  list_alerts      static, burn-rate and anomaly alerts started or firing in a window
  search_logs      the warning and error lines the services logged, and log volume per level
  get_traces       failed and slow requests from Tempo, and the spans of a single trace
  k8s_events       Kubernetes events in shopsearch and kube-system
  deploy_history   ArgoCD releases and the rollout status of every service
  service_graph    who calls whom, and how each edge's traffic and failures changed
  read_runbook     one runbook from docs/runbooks

query_metric only knows the recorded queries, which are scoped to the workload.
That keeps the promise the recorder makes (no series about the chaos namespace,
its k6 pods or Chaos Mesh reach the agent) without a filter on free-form PromQL,
the gap section 11 of the design doc left open for week 8.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from recorder.record import QUERIES, iso, load_queries, parse_iso

from .replay import View

RUNBOOKS = Path(__file__).resolve().parents[2] / "docs" / "runbooks"
MAX_CHARS = 6000
TIMEOUT_S = 10.0

# Metrics that come from traces (Tempo's service graph): hidden with the traces in ablation A2.
TRACE_METRICS = ("graph_requests", "graph_failed")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryMetric(_Args):
    metric: str = Field(description="a metric name from the list in this tool's description")
    match: dict[str, str] = Field(default_factory=dict, description='label filters, e.g. {"job": "shopsearch/catalog"} or {"container": "postgres"}; values match exactly')
    minutes: int = Field(default=30, ge=5, le=120, description="how far back from now")
    step_seconds: int = Field(default=60, ge=15, le=600, description="one value per step: the mean over it")


class ListAlerts(_Args):
    minutes: int = Field(default=60, ge=5, le=120)
    component: str | None = Field(default=None, description="only alerts about this component")


class SearchLogs(_Args):
    service: str | None = Field(default=None, description="gateway, search-api, catalog or indexer")
    level: str | None = Field(default=None, description="warn or error")
    contains: str | None = Field(default=None, description="case-insensitive substring of the line")
    minutes: int = Field(default=15, ge=1, le=120)
    limit: int = Field(default=20, ge=1, le=50)


class GetTraces(_Args):
    kind: Literal["errors", "slow", "any"] = "any"
    service: str | None = Field(default=None, description="root service of the trace")
    trace_id: str | None = Field(default=None, description="show the spans of this trace (if its spans were recorded)")
    minutes: int = Field(default=15, ge=1, le=120)
    limit: int = Field(default=10, ge=1, le=30)


class K8sEvents(_Args):
    namespace: Literal["shopsearch", "kube-system"] | None = None
    object: str | None = Field(default=None, description="substring of the object, e.g. 'Pod/catalog' or 'Rollout/'")
    type: Literal["Normal", "Warning"] | None = None
    minutes: int = Field(default=30, ge=1, le=120)
    limit: int = Field(default=30, ge=1, le=60)


class DeployHistory(_Args):
    app: str | None = Field(default=None, description="an ArgoCD application: gateway, search-api, catalog, indexer, redis, postgres")
    minutes: int = Field(default=120, ge=5, le=120)


class ServiceGraphArgs(_Args):
    minutes: int = Field(default=5, ge=1, le=30, description="recent window, compared with the hour before it")


class ReadRunbook(_Args):
    name: str = Field(description="a runbook file name from the index in the system prompt, e.g. 'F12-slow-query'")


# --- formatting -----------------------------------------------------------------
def fmt(v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.3g}G"
    if a >= 1e6:
        return f"{v / 1e6:.3g}M"
    if a >= 1e4:
        return f"{v / 1e3:.3g}k"
    return f"{v:.3g}"


def ago(view: View, t: float) -> str:
    s = view.now - t
    return f"-{s / 60:.0f}m" if s >= 90 else f"-{s:.0f}s"


def labels_str(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items())) or "(no labels)"


def _mean(x: np.ndarray) -> float | None:
    x = x[~np.isnan(x)]
    return float(np.mean(x)) if len(x) else None


# --- tools ----------------------------------------------------------------------
def query_metric(view: View, a: QueryMetric, metrics: dict[str, str]) -> str:
    if a.metric not in metrics:
        raise ValueError(f"unknown metric {a.metric!r}; known: {', '.join(metrics)}")
    f, step = view.frame, view.frame.step
    hi = f.index(view.now) + 1
    lo = max(0, hi - int(a.minutes * 60 / step))
    base_lo = max(0, lo - int(3600 / step))
    per = max(1, int(a.step_seconds / step))
    rows = []
    for labels, values in f.series(a.metric):
        if any(labels.get(k) != v for k, v in a.match.items()):
            continue
        window = values[lo:hi]
        if np.all(np.isnan(window)):
            continue
        base = values[base_lo:lo]
        base = base[~np.isnan(base)]
        points = [fmt(_mean(window[i:i + per])) for i in range(0, len(window), per)]
        rows.append(f"{labels_str(labels)}: hour before median {fmt(float(np.median(base)) if len(base) else None)}"
                    f" | {' '.join(points)} (now)")
    if not rows:
        return f"no data for {a.metric} {labels_str(a.match)} in the last {a.minutes} min"
    head = (f"{a.metric}, last {a.minutes} min, one value per {per * step:.0f}s, oldest first "
            f"(from {iso(f.start + lo * step)} to {iso(view.now)})")
    return "\n".join([head, *rows])


def list_alerts(view: View, a: ListAlerts) -> str:
    since = view.now - a.minutes * 60
    rows = []
    for al in view.alerts:
        if al.end is not None and al.end < since:
            continue
        if a.component and al.component != a.component:
            continue
        state = "firing" if al.end is None else f"resolved {ago(view, al.end)}"
        extra = labels_str({k: v for k, v in al.labels.items() if k not in ("job",)})
        score = f" score {al.score}" if al.score is not None else ""
        rows.append(f"{ago(view, al.start)}  {al.detector:<9} {al.name:<28} {al.component:<10} {al.severity:<8} {state}{score}  {extra}")
    return "\n".join(rows) if rows else f"no alerts in the last {a.minutes} min"


def search_logs(view: View, a: SearchLogs) -> str:
    since = view.now - a.minutes * 60
    lines, truncated, counts = [], False, {}
    for _, logs in view.section("logs"):
        truncated |= bool(logs.get("truncated"))
        for s in logs.get("volume") or []:
            key = (s["labels"].get("service_name"), s["labels"].get("detected_level"))
            counts[key] = counts.get(key, 0) + sum(v or 0 for t, v in s["values"] if t >= since)
        for e in logs.get("lines", []):
            if parse_iso(e["time"]) < since:
                continue
            if a.service and e.get("service") != a.service:
                continue
            if a.level and not (e.get("level") or "").lower().startswith(a.level.lower()[:4]):
                continue
            if a.contains and a.contains.lower() not in e["line"].lower():
                continue
            lines.append(e)
    vol = ", ".join(f"{svc}/{lvl} {int(n)}" for (svc, lvl), n in sorted(counts.items(), key=lambda kv: str(kv[0])) if n)
    out = [f"log lines per service/level, last {a.minutes} min: {vol or 'none'}"]
    shown = lines[-a.limit:]
    out.append(f"{len(lines)} matching warning/error lines" + (f", the last {len(shown)}:" if lines else ""))
    for e in shown:
        out.append(f"{ago(view, parse_iso(e['time']))} {e.get('service')} {e.get('level')} "
                   f"trace={e.get('trace_id') or '-'}: {e['line'][:400]}")
    if truncated:
        out.append("(the recorder kept at most 300 lines per 5 minutes; some were dropped)")
    return "\n".join(out)


def get_traces(view: View, a: GetTraces) -> str:
    if a.trace_id:
        for _, tr in view.section("traces"):
            for t in tr.get("full", []):
                if t["trace_id"] == a.trace_id:
                    out = [f"trace {a.trace_id}: {len(t['spans'])} spans (offset ms, duration ms)"]
                    for s in t["spans"][:60]:
                        attrs = labels_str({k: str(v) for k, v in s.get("attributes", {}).items()})
                        err = " ERROR" if s.get("status") in (2, "STATUS_CODE_ERROR") else ""
                        out.append(f"  +{s.get('offset_ms', 0):.1f} {s['duration_ms']:.1f}ms {s['service']} "
                                   f"{s['kind']} {s['name']}{err} {attrs}")
                    return "\n".join(out)
        return f"trace {a.trace_id}: its spans were not recorded (only 3 per 5 minutes are); use the summaries"
    since = view.now - a.minutes * 60
    seen, rows, full_ids = set(), [], set()
    for _, tr in view.section("traces"):
        full_ids |= {t["trace_id"] for t in tr.get("full", [])}
        for s in tr.get("summaries", []):
            if parse_iso(s["start"]) < since or s["trace_id"] in seen:
                continue
            if a.kind != "any" and s["search"] != a.kind:
                continue
            if a.service and s.get("root_service") != a.service:
                continue
            seen.add(s["trace_id"])
            rows.append(s)
    rows.sort(key=lambda s: -(s.get("duration_ms") or 0))
    out = [f"{len(rows)} {a.kind} traces in the last {a.minutes} min, slowest first"]
    for s in rows[:a.limit]:
        mark = " [spans recorded]" if s["trace_id"] in full_ids else ""
        out.append(f"{ago(view, parse_iso(s['start']))} {s['search']:<6} {s.get('root_service')} {s.get('root_name')} "
                   f"{s.get('duration_ms')}ms trace={s['trace_id']}{mark}")
    return "\n".join(out)


def k8s_events(view: View, a: K8sEvents) -> str:
    since = view.now - a.minutes * 60
    rows, complete = [], True
    for c, events in view.section("events"):
        complete &= bool(c.get("events_complete", True))
        for e in events:
            if parse_iso(e["time"]) < since:
                continue
            if (a.namespace and e["namespace"] != a.namespace) or (a.type and e["type"] != a.type):
                continue
            if a.object and a.object.lower() not in e["object"].lower():
                continue
            rows.append(e)
    out = [f"{len(rows)} events in the last {a.minutes} min" + (f", the last {a.limit}:" if len(rows) > a.limit else "")]
    for e in rows[-a.limit:]:
        out.append(f"{ago(view, parse_iso(e['time']))} {e['namespace']} {e['type']} {e['reason']} {e['object']} "
                   f"x{e['count']}: {e['message'][:300]}")
    if not complete:
        out.append("(some chunks were recorded late, after the API server had dropped their events)")
    return "\n".join(out)


def deploy_history(view: View, a: DeployHistory) -> str:
    since = view.now - a.minutes * 60
    history, status = [], None
    for _, d in view.section("deploys"):
        history += [h for h in d.get("history", []) if parse_iso(h["deployed_at"]) >= since and (not a.app or h["app"] == a.app)]
        if d.get("observed_at"):
            status = d
    out = [f"{len(history)} ArgoCD releases in the last {a.minutes} min"]
    for h in sorted(history, key=lambda h: h["deployed_at"]):
        params = labels_str(h.get("parameters") or {}) if h.get("parameters") else "no parameter overrides"
        out.append(f"{ago(view, parse_iso(h['deployed_at']))} {h['app']} history id {h.get('id')} "
                   f"revision {str(h.get('revision'))[:12]}: {params}")
    if status:
        out.append(f"rollout status observed {ago(view, parse_iso(status['observed_at']))}:")
        for r in status.get("rollouts") or []:
            if a.app and r["name"] != a.app:
                continue
            canary = "" if r.get("current_hash") == r.get("stable_hash") else f" (current {r.get('current_hash')} is not the stable {r.get('stable_hash')})"
            out.append(f"  {r['name']}: {r.get('phase')}, {r.get('ready')}/{r.get('replicas')} ready, "
                       f"image {', '.join(i.rsplit('/', 1)[-1] for i in r.get('images') or [])}{canary}")
        apps = [f"{x['app']} {x.get('sync')}/{x.get('health')}" for x in status.get("applications") or []
                if not a.app or x["app"] == a.app]
        if apps:
            out.append("  applications: " + "; ".join(apps))
    return "\n".join(out)


def service_graph(view: View, a: ServiceGraphArgs, with_traces: bool) -> str:
    g = view.graph
    out = ["edges, two components that talk to each other ('static only': traces showed no traffic on it):"]
    out += [f"  {e}" for e in g.describe()] if with_traces else [f"  {' -- '.join(sorted(e))}" for e in sorted(g.edges, key=sorted)]
    if not with_traces:
        return "\n".join(out)
    f, step = view.frame, view.frame.step
    hi = f.index(view.now) + 1
    lo = max(0, hi - int(a.minutes * 60 / step))
    base_lo = max(0, lo - int(3600 / step))
    failed = {labels_str(l): v for l, v in f.series("graph_failed")}
    out.append(f"traffic per edge from traces, req/s and failed/s: last {a.minutes} min vs the hour before")
    for labels, req in f.series("graph_requests"):
        fail = failed.get(labels_str(labels), np.full(len(req), np.nan))
        out.append(f"  {labels.get('client')} -> {labels.get('server')}: {fmt(_mean(req[lo:hi]))} req/s "
                   f"(was {fmt(_mean(req[base_lo:lo]))}), failed {fmt(_mean(fail[lo:hi]))}/s (was {fmt(_mean(fail[base_lo:lo]))})")
    return "\n".join(out)


def runbook_names(root: Path = RUNBOOKS) -> list[str]:
    return sorted(p.stem for p in root.glob("F*.md"))


def read_runbook(a: ReadRunbook, root: Path = RUNBOOKS) -> str:
    name = Path(a.name).stem
    if name not in runbook_names(root):
        raise ValueError(f"no runbook {a.name!r}; there are: {', '.join(runbook_names(root))}")
    text = (root / f"{name}.md").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.startswith("!["))  # images mean nothing here


# --- the toolbox ------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[_Args]
    run: Callable[[View, Any], str]

    def input_schema(self) -> dict[str, Any]:
        s = self.args.model_json_schema()
        s.pop("title", None)
        return s


def metric_catalog(queries_path: Path = QUERIES, with_traces: bool = True) -> dict[str, str]:
    q = load_queries(queries_path)
    return {k: " ".join(v.split()) for k, v in q["metrics"].items() if with_traces or k not in TRACE_METRICS}


def toolbox(*, traces: bool = True, runbooks: bool = True, queries_path: Path = QUERIES) -> list[Tool]:
    """The tools of a variant: A1 all, A2 without traces, A3 without runbooks."""
    metrics = metric_catalog(queries_path, traces)
    listing = "\n".join(f"- {k}: {v}" for k, v in metrics.items())
    tools = [
        Tool("query_metric",
             "Values of one recorded metric over the last minutes, per series, with the median of the hour before "
             "for comparison. Each metric is a fixed PromQL query; services carry job=shopsearch/<service>, "
             "containers carry container=<name>. The metrics:\n" + listing,
             QueryMetric, lambda v, a: query_metric(v, a, metrics)),
        Tool("list_alerts", "Alerts from the three detectors (static thresholds, SLO burn rate, anomaly) that started "
             "or were still firing in the window, oldest first, with how long ago they started.", ListAlerts, list_alerts),
        Tool("search_logs", "Warning and error log lines of the ShopSearch services (each carries its trace id), "
             "newest last, and the number of log lines per service and level.", SearchLogs, search_logs),
        Tool("k8s_events", "Kubernetes events (pod starts, kills, probe failures, scheduling, rollout steps) in the "
             "shopsearch and kube-system namespaces.", K8sEvents, k8s_events),
        Tool("deploy_history", "Releases ArgoCD made (with the Helm parameters each set) and the current status of "
             "every Argo Rollout and ArgoCD application.", DeployHistory, deploy_history),
        Tool("service_graph", "Who calls whom in ShopSearch" + (", and per edge the request and failure rates traces "
             "measured, now against the hour before." if traces else "."), ServiceGraphArgs,
             lambda v, a: service_graph(v, a, traces)),
    ]
    if traces:
        tools.append(Tool("get_traces", "Failed and slow (over 300 ms) requests from Tempo, slowest first; with "
                          "trace_id, the spans of that request with their durations.", GetTraces, get_traces))
    if runbooks:
        tools.append(Tool("read_runbook", "The full text of a runbook: symptoms, checks, fix, prevention.",
                          ReadRunbook, lambda v, a: read_runbook(a)))
    return tools


def run_tool(tools: dict[str, Tool], view: View, name: str, raw_args: dict[str, Any],
             timeout_s: float = TIMEOUT_S, max_chars: int = MAX_CHARS) -> tuple[str, bool, float]:
    """(output, is_error, seconds). Bad arguments, a timeout or an exception
    come back as an error result the model can read and correct, never as a crash."""
    started = time.monotonic()
    tool = tools.get(name)
    if tool is None:
        return f"no tool {name!r}; available: {', '.join(tools)}", True, 0.0
    try:
        args = tool.args.model_validate(raw_args or {})
    except ValidationError as exc:
        return f"invalid arguments: {json.dumps(exc.errors(include_url=False), default=str)[:1500]}", True, 0.0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        out = pool.submit(tool.run, view, args).result(timeout=timeout_s)
        error = False
    except concurrent.futures.TimeoutError:
        out, error = f"{name} timed out after {timeout_s:.0f}s", True
    except (ValueError, KeyError, TypeError) as exc:
        out, error = f"{name} failed: {exc}", True
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n... cut at {max_chars} characters; narrow the query"
    return out, error, round(time.monotonic() - started, 3)
