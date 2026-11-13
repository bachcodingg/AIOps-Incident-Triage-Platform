"""Recorded chunks -> aligned numpy series, and the static and burn-rate alerts.

Everything lives on the recording's 15 s grid. A missing sample is NaN, which
matters: a service that crashed has no metrics at all, and the detectors treat
"no data" differently from "zero".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import numpy as np

from .alerts import Alert, component_of

# Labels that describe an alert's state rather than which alert it is.
_ALERT_META = {"alertname", "alertstate", "detector", "severity", "component"}


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def label_key(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={labels[k]}" for k in sorted(labels))


class Frame:
    """Metric series on a regular grid: frame.get("requests", job="shopsearch/gateway")."""

    def __init__(self, start: float, end: float, step: float):
        self.start, self.end, self.step = start, end, step
        self.times = np.arange(start, end, step, dtype=float)
        self.data: dict[str, dict[str, np.ndarray]] = {}
        self._labels: dict[str, dict[str, dict[str, str]]] = {}

    def __len__(self) -> int:
        return len(self.times)

    def index(self, t: float) -> int:
        return int(round((t - self.start) / self.step))

    def add(self, metric: str, labels: dict[str, str], values: Iterable[tuple[float, float | None]]) -> None:
        key = label_key(labels)
        arr = self.data.setdefault(metric, {}).get(key)
        if arr is None:
            arr = self.data[metric][key] = np.full(len(self.times), np.nan)
            self._labels.setdefault(metric, {})[key] = dict(labels)
        for t, v in values:
            i = self.index(t)
            if v is not None and 0 <= i < len(arr):
                arr[i] = v

    def get(self, metric: str, **labels: str) -> np.ndarray:
        arr = self.data.get(metric, {}).get(label_key(labels))
        return np.full(len(self.times), np.nan) if arr is None else arr.copy()

    def series(self, metric: str) -> list[tuple[dict[str, str], np.ndarray]]:
        return [(self._labels[metric][k], v.copy()) for k, v in self.data.get(metric, {}).items()]

    def sum(self, metric: str) -> np.ndarray:
        """Sum over all label sets; NaN only where every one of them is missing."""
        parts = [v for _, v in self.series(metric)]
        if not parts:
            return np.full(len(self.times), np.nan)
        stacked = np.vstack(parts)
        out = np.nansum(stacked, axis=0)
        out[np.all(np.isnan(stacked), axis=0)] = np.nan
        return out


def alert_intervals(samples: dict[str, tuple[dict[str, str], list[float]]], step: float, data_end: float) -> list[Alert]:
    """ALERTS samples per label set -> one Alert per uninterrupted firing run."""
    alerts = []
    for labels, times in samples.values():
        times = sorted(set(times))
        runs, run = [], [times[0]]
        for t in times[1:]:
            if t - run[-1] > 2 * step:
                runs.append(run)
                run = [t]
            else:
                run.append(t)
        runs.append(run)
        for r in runs:
            still_firing = r[-1] >= data_end - 2 * step
            alerts.append(Alert(
                detector=labels.get("detector", "unknown"), name=labels.get("alertname", "unknown"),
                component=component_of(labels), start=r[0], end=None if still_firing else r[-1] + step,
                severity=labels.get("severity", ""),
                labels={k: v for k, v in labels.items() if k not in _ALERT_META},
            ))
    return sorted(alerts, key=lambda a: (a.start, a.name))


def load(chunks: Iterable[dict[str, Any]]) -> tuple[Frame, list[Alert]]:
    """Read metrics and alerts from chunks (oldest first) in one pass."""
    metrics: list[tuple[str, dict[str, str], list]] = []
    alert_samples: dict[str, tuple[dict[str, str], list[float]]] = {}
    start = end = step = None
    for chunk in chunks:
        t0, t1 = parse_iso(chunk["start"]), parse_iso(chunk["end"])
        start = t0 if start is None else min(start, t0)
        end = t1 if end is None else max(end, t1)
        step = chunk["step"]
        for name, result in (chunk.get("metrics") or {}).items():
            for s in result or []:
                metrics.append((name, s["labels"], s["values"]))
        for s in chunk.get("alerts") or []:
            key = label_key({k: v for k, v in s["labels"].items() if k != "alertstate"})
            entry = alert_samples.setdefault(key, (s["labels"], []))
            entry[1].extend(t for t, v in s["values"] if v is not None)
    if start is None:
        raise ValueError("no chunks to read")
    frame = Frame(start, end, step)
    for name, labels, values in metrics:
        frame.add(name, labels, values)
    alert_samples = {k: v for k, v in alert_samples.items() if v[1]}
    return frame, alert_intervals(alert_samples, step, end)
