"""Record telemetry in fixed 5-minute chunks.

Every chunk covers [start, start + 5 min) on a wall-clock grid and holds one
section per signal: metrics, alerts, events, deploys, logs, traces. A section
that fails (Loki restarting, say) is stored as null with the error next to it,
so one bad backend never costs the other signals.

Live mode waits until a chunk is over plus a lag (OTLP batches and rate windows
need about a minute to settle), records it, and moves on. Started with a past
--from, it catches up first. Kubernetes keeps events for about an hour, so
those are only complete for chunks recorded live.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .kube import Kube, KubeError

QUERIES = Path(__file__).with_name("queries.yaml")
CHUNK_SECONDS = 300
LAG_SECONDS = 90
EVENT_TTL_SECONDS = 3300  # the API server keeps events for an hour; leave some margin
FORMAT_VERSION = 1

# Span attributes worth keeping in a condensed trace.
SPAN_ATTRIBUTES = (
    "http.request.method", "http.route", "http.response.status_code", "url.path",
    "db.system", "db.system.name", "db.operation.name", "db.query.text", "db.statement",
    "server.address", "peer.service", "error.type",
)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def chunk_name(t0: float) -> str:
    return datetime.fromtimestamp(t0, timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json.gz"


def load_queries(path: Path = QUERIES) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- sections -------------------------------------------------------------------
def _num(v: Any) -> float | None:
    f = float(v)
    return None if math.isnan(f) or math.isinf(f) else f


def series(result: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prometheus or Loki matrix -> [{labels, values: [[t, v], ...]}], NaN as null."""
    out = []
    for s in result:
        labels = {k: v for k, v in s.get("metric", {}).items() if k != "__name__"}
        out.append({"labels": labels, "values": [[float(t), _num(v)] for t, v in s.get("values", [])]})
    return out


def _metrics(kube: Kube, q: dict[str, Any], t0: float, t1: float, errors: dict[str, str]) -> dict[str, Any]:
    step, out = q["step_seconds"], {}
    for name, expr in q["metrics"].items():
        try:
            out[name] = series(kube.prom_range(expr, t0, t1 - step, step))
        except (KubeError, ValueError) as exc:
            out[name] = None
            errors[f"metrics.{name}"] = str(exc)[:300]
    return out


def _alerts(kube: Kube, q: dict[str, Any], t0: float, t1: float) -> list[dict[str, Any]]:
    step = q["step_seconds"]
    return series(kube.prom_range(q["alerts"], t0, t1 - step, step))


def _event_time(e: dict[str, Any]) -> str | None:
    return e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp")


def _events(kube: Kube, q: dict[str, Any], t0: float, t1: float) -> list[dict[str, Any]]:
    out = []
    for ns in q["events_namespaces"]:
        for e in kube.get("events", ns):
            when = _event_time(e)
            if not when or not t0 <= parse_iso(when) < t1:
                continue
            obj = e.get("involvedObject", {})
            out.append({
                "time": when, "namespace": ns, "type": e.get("type"), "reason": e.get("reason"),
                "object": f"{obj.get('kind')}/{obj.get('name')}", "message": (e.get("message") or "")[:500],
                "count": e.get("count") or 1,
                "source": e.get("reportingComponent") or e.get("source", {}).get("component"),
            })
    return sorted(out, key=lambda e: e["time"])


def _deploys(kube: Kube, q: dict[str, Any], t0: float, t1: float) -> dict[str, Any]:
    history, apps = [], []
    for app in kube.get("applications.argoproj.io", q["argocd_namespace"]):
        name, status = app["metadata"]["name"], app.get("status", {})
        apps.append({"app": name, "sync": status.get("sync", {}).get("status"),
                     "health": status.get("health", {}).get("status")})
        for h in status.get("history") or []:
            if h.get("deployedAt") and t0 <= parse_iso(h["deployedAt"]) < t1:
                params = (h.get("source") or {}).get("helm", {}).get("parameters") or []
                history.append({"app": name, "deployed_at": h["deployedAt"], "id": h.get("id"),
                                "revision": h.get("revision"),
                                "parameters": {p["name"]: p.get("value") for p in params}})
    rollouts = []
    for r in kube.get("rollouts.argoproj.io", q["rollouts_namespace"]):
        st, containers = r.get("status", {}), r["spec"].get("template", {}).get("spec", {}).get("containers", [])
        rollouts.append({
            "name": r["metadata"]["name"], "phase": st.get("phase"), "current_hash": st.get("currentPodHash"),
            "stable_hash": st.get("stableRS"), "images": [c.get("image") for c in containers],
            "replicas": st.get("replicas"), "ready": st.get("readyReplicas"),
        })
    return {"history": sorted(history, key=lambda h: h["deployed_at"]), "applications": apps,
            "rollouts": rollouts, "observed_at": iso(time.time())}


def _logs(kube: Kube, q: dict[str, Any], t0: float, t1: float) -> dict[str, Any]:
    cfg = q["logs"]
    volume = kube.loki_range(cfg["volume"], t0, t1, step=60)
    data = kube.loki_range(cfg["lines"], t0, t1, limit=cfg["max_lines"])
    lines = []
    for stream in data.get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            lines.append({"time": iso(int(ts) / 1e9), "service": labels.get("service_name"),
                          "level": labels.get("detected_level"), "trace_id": labels.get("trace_id"),
                          "line": line[:2000]})
    lines.sort(key=lambda entry: entry["time"])
    return {"volume": series(volume.get("result", [])), "lines": lines, "truncated": len(lines) >= cfg["max_lines"]}


def _attr_value(v: dict[str, Any]) -> Any:
    return next(iter(v.values()), None) if v else None


def condense_trace(body: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """An OTLP trace from Tempo, cut down to what a human or an agent reads."""
    spans = []
    for rs in body.get("batches") or body.get("resourceSpans") or []:
        res = {a["key"]: _attr_value(a.get("value", {})) for a in rs.get("resource", {}).get("attributes", [])}
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans", []):
                attrs = {a["key"]: _attr_value(a.get("value", {})) for a in sp.get("attributes", [])}
                start, end = int(sp["startTimeUnixNano"]), int(sp["endTimeUnixNano"])
                kept = {k: (str(attrs[k])[:300] if isinstance(attrs[k], str) else attrs[k])
                        for k in SPAN_ATTRIBUTES if k in attrs}
                spans.append({"service": res.get("service.name"), "name": sp.get("name"),
                              "span_id": sp.get("spanId"), "parent_id": sp.get("parentSpanId") or None,
                              "kind": sp.get("kind"), "start_ns": start, "duration_ms": round((end - start) / 1e6, 3),
                              "status": (sp.get("status") or {}).get("code"), "attributes": kept})
    if spans:
        first = min(s["start_ns"] for s in spans)
        for s in spans:
            s["offset_ms"] = round((s.pop("start_ns") - first) / 1e6, 3)
    spans.sort(key=lambda s: s.get("offset_ms", 0))
    return {"trace_id": trace_id, "spans": spans}


def _traces(kube: Kube, q: dict[str, Any], t0: float, t1: float) -> dict[str, Any]:
    cfg, summaries = q["traces"], []
    for name, traceql in cfg["searches"].items():
        for t in kube.tempo_search(traceql, t0, t1, cfg["limit"]):
            summaries.append({"search": name, "trace_id": t.get("traceID"), "root_service": t.get("rootServiceName"),
                              "root_name": t.get("rootTraceName"), "duration_ms": t.get("durationMs"),
                              "start": iso(int(t.get("startTimeUnixNano", 0)) / 1e9)})
    # Failed ones first, then the slowest; each trace once.
    ranked = sorted(summaries, key=lambda s: (s["search"] != "errors", -(s["duration_ms"] or 0)))
    picked: list[str] = []
    for s in ranked:
        if s["trace_id"] and s["trace_id"] not in picked:
            picked.append(s["trace_id"])
        if len(picked) >= cfg["full_traces"]:
            break
    return {"summaries": summaries, "full": [condense_trace(kube.tempo_trace(tid), tid) for tid in picked]}


def record_chunk(kube: Kube, q: dict[str, Any], t0: float, t1: float, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    errors: dict[str, str] = {}
    chunk: dict[str, Any] = {"version": FORMAT_VERSION, "start": iso(t0), "end": iso(t1),
                             "step": q["step_seconds"], "events_complete": now - t0 < EVENT_TTL_SECONDS}
    chunk["metrics"] = _metrics(kube, q, t0, t1, errors)
    sections: dict[str, Callable[[], Any]] = {
        "alerts": lambda: _alerts(kube, q, t0, t1),
        "events": lambda: _events(kube, q, t0, t1),
        "deploys": lambda: _deploys(kube, q, t0, t1),
        "logs": lambda: _logs(kube, q, t0, t1),
        "traces": lambda: _traces(kube, q, t0, t1),
    }
    for name, fn in sections.items():
        try:
            chunk[name] = fn()
        except (KubeError, ValueError, KeyError) as exc:
            chunk[name] = None
            errors[name] = str(exc)[:300]
    chunk["errors"] = errors
    return chunk


# --- files ------------------------------------------------------------------------
def write_chunk(directory: Path, chunk: dict[str, Any]) -> Path:
    """Gzipped JSON with a zero mtime, so the same chunk always has the same hash."""
    chunks = Path(directory) / "chunks"
    chunks.mkdir(parents=True, exist_ok=True)
    path = chunks / chunk_name(parse_iso(chunk["start"]))
    tmp = path.with_suffix(".tmp")
    data = json.dumps(chunk, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        gz.write(data)
    os.replace(tmp, path)
    return path


def write_meta(directory: Path, *, name: str, context: str, identity: str | None, chunk_s: int,
               queries_path: Path) -> None:
    path = Path(directory) / "recording.json"
    if path.exists():
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta["queries_sha256"] != file_sha256(queries_path) or meta["chunk_seconds"] != chunk_s:
            raise SystemExit(f"{directory} was recorded with other queries or chunk size; use a new directory")
        return
    Path(directory).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "name": name, "format": FORMAT_VERSION, "created": iso(time.time()), "context": context,
        "identity": identity, "chunk_seconds": chunk_s, "queries_sha256": file_sha256(queries_path),
        "queries": load_queries(queries_path),
    }, indent=1) + "\n", encoding="utf-8")


def record(
    kube: Kube,
    directory: Path,
    q: dict[str, Any],
    *,
    start: float,
    end: float | None,
    chunk_s: int = CHUNK_SECONDS,
    lag_s: int = LAG_SECONDS,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> int:
    """Record every chunk from `start` (rounded down to the grid) until `end`,
    or forever. Chunks already on disk are skipped, so a stopped recording can
    be resumed with the same command."""
    t0, written = math.floor(start / chunk_s) * chunk_s, 0
    while end is None or t0 < end:
        t1 = t0 + chunk_s
        path = Path(directory) / "chunks" / chunk_name(t0)
        if not path.exists():
            while (wait := t1 + lag_s - clock()) > 0:
                sleep(min(wait, 30))
            chunk = record_chunk(kube, q, t0, t1, now=clock())
            write_chunk(directory, chunk)
            written += 1
            bad = f" (failed: {', '.join(chunk['errors'])})" if chunk["errors"] else ""
            log(f"{iso(t0)}  {len(chunk.get('alerts') or [])} alert series, "
                f"{len((chunk.get('logs') or {}).get('lines', []))} log lines{bad}")
        t0 = t1
    return written
