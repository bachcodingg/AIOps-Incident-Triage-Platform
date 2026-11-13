"""Per-component signals the anomaly detectors score."""

from __future__ import annotations

import numpy as np

from .series import Frame

HTTP_SERVICES = ("gateway", "search-api", "catalog")
# Every component with its own metrics. node and cluster are judged by the
# static rules and by what their loss does to everything else.
ANOMALY_COMPONENTS = ("gateway", "search-api", "catalog", "indexer", "redis", "postgres")
# Below this request rate an error ratio means nothing (one 500 in two requests).
MIN_RPS = 0.05


def red(frame: Frame, component: str) -> dict[str, np.ndarray]:
    """Rate, errors and duration, as the service measures itself."""
    if component in HTTP_SERVICES:
        job = f"shopsearch/{component}"
        rps = frame.get("requests", job=job)
        errors = frame.get("errors", job=job)
        # No 5xx series at all just means no 5xx yet.
        errors = np.where(np.isnan(errors) & ~np.isnan(rps), 0.0, errors)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(rps > MIN_RPS, errors / rps, 0.0)
        ratio[np.isnan(rps)] = np.nan
        return {"requests": rps, "error_ratio": ratio, "latency_p95": frame.get("latency_p95", job=job)}
    if component == "indexer":
        return {"changes": frame.sum("indexer_changes")}
    return {}


def saturation(frame: Frame, component: str) -> dict[str, np.ndarray]:
    c = component
    with np.errstate(divide="ignore", invalid="ignore"):
        cpu = frame.get("cpu", container=c) / frame.get("cpu_limit", container=c)
        memory = frame.get("memory", container=c) / frame.get("memory_limit", container=c)
        ready = frame.get("ready", container=c) / frame.get("containers", container=c)
    restarts = np.clip(np.diff(frame.get("restarts", container=c), prepend=np.nan), 0, None)
    return {"cpu": cpu, "memory": memory, "ready": ready, "restarts": restarts}


def all_features(frame: Frame, component: str) -> dict[str, np.ndarray]:
    return {**red(frame, component), **saturation(frame, component)}


def presence(frame: Frame, component: str) -> np.ndarray:
    """True where the component reported its main metric."""
    if component in HTTP_SERVICES:
        ref = frame.get("requests", job=f"shopsearch/{component}")
    elif component == "indexer":
        ref = frame.get("indexer_ready")
    else:
        ref = frame.get("cpu", container=component)
    return ~np.isnan(ref)
