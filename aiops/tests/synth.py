"""Synthetic recordings: steady traffic with noise, plus whatever a test breaks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

from recorder.record import QUERIES, iso, write_chunk, write_meta

T0 = 1_800_000_000.0  # 2027-01-15T08:00:00Z, on the 5-minute grid
STEP = 15.0
CHUNK = 300.0

# Baseline per service: requests/s, error ratio, p95 seconds, cpu share, memory share.
BASE = {
    "gateway": (8.0, 0.001, 0.040, 0.20, 0.50),
    "search-api": (6.0, 0.001, 0.025, 0.25, 0.55),
    "catalog": (2.0, 0.001, 0.015, 0.15, 0.60),
}
OTHERS = {"indexer": (0.10, 0.40), "redis": (0.05, 0.20), "postgres": (0.20, 0.45)}

# A modifier changes a value: modifier(metric, component, t, value) -> value (None drops the sample).
Modifier = Callable[[str, str, float, float], float | None]


def _noise(rng: np.random.Generator, value: float, rel: float = 0.05) -> float:
    return max(0.0, value * (1 + rng.normal(0, rel)))


def make_chunks(hours: float, *, start: float = T0, seed: int = 0, modify: Modifier | None = None,
                alerts: list[tuple[dict[str, str], float, float]] = ()) -> list[dict]:
    rng = np.random.default_rng(seed)
    chunks = []
    for c in range(int(hours * 3600 / CHUNK)):
        t0 = start + c * CHUNK
        times = [t0 + i * STEP for i in range(int(CHUNK / STEP))]
        metrics: dict[str, list] = {k: [] for k in (
            "requests", "errors", "latency_p95", "cpu", "cpu_limit", "memory", "memory_limit",
            "ready", "containers", "restarts", "indexer_changes", "indexer_ready", "graph_requests")}

        def add(metric: str, component: str, labels: dict[str, str], value_at: Callable[[float], float]) -> None:
            values = []
            for t in times:
                v = value_at(t)
                if modify is not None:
                    v = modify(metric, component, t, v)
                if v is not None:
                    values.append([t, v])
            if values:
                metrics[metric].append({"labels": labels, "values": values})

        for svc, (rps, err, p95, cpu, mem) in BASE.items():
            job = {"job": f"shopsearch/{svc}"}
            add("requests", svc, job, lambda t, r=rps: _noise(rng, r))
            add("errors", svc, job, lambda t, r=rps, e=err: _noise(rng, r * e, 0.3))
            add("latency_p95", svc, job, lambda t, p=p95: _noise(rng, p, 0.08))
            for m, v in (("cpu", cpu), ("memory", mem)):
                add(m, svc, {"container": svc}, lambda t, v=v: _noise(rng, v, 0.05))
        for comp, (cpu, mem) in OTHERS.items():
            add("cpu", comp, {"container": comp}, lambda t, v=cpu: _noise(rng, v, 0.05))
            add("memory", comp, {"container": comp}, lambda t, v=mem: _noise(rng, v, 0.05))
        for comp in (*BASE, *OTHERS):
            add("cpu_limit", comp, {"container": comp}, lambda t: 1.0)
            add("memory_limit", comp, {"container": comp}, lambda t: 1.0)
            add("ready", comp, {"container": comp}, lambda t: 1.0)
            add("containers", comp, {"container": comp}, lambda t: 1.0)
            add("restarts", comp, {"container": comp}, lambda t: 0.0)
        add("indexer_changes", "indexer", {"outcome": "indexed"}, lambda t: _noise(rng, 0.5, 0.1))
        add("indexer_ready", "indexer", {}, lambda t: 1.0)
        add("graph_requests", "gateway", {"client": "gateway", "server": "search-api"}, lambda t: 6.0)
        add("graph_requests", "search-api", {"client": "search-api", "server": "redis"}, lambda t: 6.0)
        add("graph_requests", "catalog", {"client": "catalog", "server": "postgresql"}, lambda t: 2.0)

        alert_series = []
        for labels, a_start, a_end in alerts:
            values = [[t, 1.0] for t in times if a_start <= t < a_end]
            if values:
                alert_series.append({"labels": {**labels, "alertstate": "firing"}, "values": values})
        chunks.append({"version": 1, "start": iso(t0), "end": iso(t0 + CHUNK), "step": STEP,
                       "events_complete": True, "metrics": metrics, "alerts": alert_series, "events": [],
                       "deploys": None, "logs": None, "traces": None, "errors": {}})
    return chunks


def write_recording(directory: Path, chunks: list[dict]) -> Path:
    write_meta(directory, name=Path(directory).name, context="test", identity=None, chunk_s=int(CHUNK),
               queries_path=QUERIES)
    for chunk in chunks:
        write_chunk(directory, chunk)
    return Path(directory)
