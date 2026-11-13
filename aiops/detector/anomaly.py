"""Detector (c): rolling median/MAD per RED metric, an Isolation Forest per
component, and missing telemetry. All three turn a score into alerts the same
way Prometheus does: above the threshold for a while before firing, below it for
a while before resolving.
"""

from __future__ import annotations

import pickle
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from sklearn.ensemble import IsolationForest

from .alerts import ANOMALY, Alert
from .features import ANOMALY_COMPONENTS, all_features, presence, red
from .series import Frame

MODEL_FORMAT = 2
# Features where a missing value means "none": no requests, no restarts.
ZERO_FILL = {"requests", "changes", "error_ratio", "restarts", "ready"}


# --- scores -> alerts --------------------------------------------------------------
def intervals(exceed: np.ndarray, step: float, for_s: float, clear_s: float) -> list[tuple[int, int | None, int]]:
    """(index it fired, index it resolved or None, index the run began) for each alert."""
    need_on, need_off = max(1, round(for_s / step)), max(1, round(clear_s / step))
    out: list[tuple[int, int | None, int]] = []
    active, on, off, fired, began = False, 0, 0, 0, 0
    for i, e in enumerate(exceed):
        if not active:
            on = on + 1 if e else 0
            if on >= need_on:
                active, fired, began, off = True, i, i - need_on + 1, 0
        else:
            off = off + 1 if not e else 0
            if off >= need_off:
                out.append((fired, i - need_off + 1, began))
                active, on = False, 0
    if active:
        out.append((fired, None, began))
    return out


def _alerts(frame: Frame, score: np.ndarray, threshold: float, for_s: float, clear_s: float,
            name: str, component: str, labels: dict[str, str],
            labels_at: Callable[[int], dict[str, str]] | None = None) -> list[Alert]:
    exceed = np.nan_to_num(score, nan=-np.inf) > threshold
    out = []
    for fired, resolved, began in intervals(exceed, frame.step, for_s, clear_s):
        stop = len(score) if resolved is None else resolved
        peak_at = began + int(np.nanargmax(score[began:stop]))
        out.append(Alert(
            detector=ANOMALY, name=name, component=component, start=float(frame.times[fired]),
            end=None if resolved is None else float(frame.times[resolved]), severity="anomaly",
            labels={**labels, **(labels_at(peak_at) if labels_at else {})}, score=round(float(score[peak_at]), 3),
        ))
    return out


# --- rolling median and MAD ---------------------------------------------------------
def rolling_scores(x: np.ndarray, window: int, min_points: int, relative: float, absolute: float) -> np.ndarray:
    """Robust z-score of each point against the `window` points before it."""
    n = len(x)
    if n == 0:
        return x.copy()
    past = sliding_window_view(np.concatenate([np.full(window, np.nan), x]), window)[:n]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN windows at the start
        median = np.nanmedian(past, axis=1)
        mad = 1.4826 * np.nanmedian(np.abs(past - median[:, None]), axis=1)
    scale = np.maximum(mad, relative * np.abs(median) + absolute)
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = (x - median) / scale
    scores[(np.sum(~np.isnan(past), axis=1) < min_points) | np.isnan(x)] = np.nan
    return scores


def mad_alerts(frame: Frame, cfg: dict[str, Any]) -> list[Alert]:
    window = round(cfg["window_minutes"] * 60 / frame.step)
    out = []
    for component in ANOMALY_COMPONENTS:
        for metric, x in red(frame, component).items():
            m = cfg["metrics"][metric]
            z = rolling_scores(x, window, cfg["min_points"], m["relative"], m["absolute"])
            for direction in (("up", "down") if m["direction"] == "both" else (m["direction"],)):
                score = z if direction == "up" else -z
                out += _alerts(frame, score, cfg["threshold"], cfg["for_seconds"], cfg["clear_seconds"],
                               "AnomalyMAD", component, {"metric": metric, "direction": direction})
    return out


# --- Isolation Forest -----------------------------------------------------------------
@dataclass
class ComponentModel:
    features: list[str]
    center: np.ndarray
    scale: np.ndarray
    fill: np.ndarray
    forest: IsolationForest
    threshold: float      # forest score above which a point is anomalous
    low: np.ndarray       # the envelope of training values, per feature, margin included
    high: np.ndarray
    trained_points: int


def _matrix(feats: dict[str, np.ndarray], names: list[str]) -> np.ndarray:
    return np.column_stack([feats[n] for n in names])


def _prepare(model: ComponentModel, x: np.ndarray) -> np.ndarray:
    x = np.where(np.isnan(x), model.fill, x)
    return (x - model.center) / model.scale


def train_iforest(frame: Frame, cfg: dict[str, Any], seed: int = 0) -> dict[str, ComponentModel]:
    models = {}
    for component in ANOMALY_COMPONENTS:
        feats = all_features(frame, component)
        names = sorted(k for k, v in feats.items() if np.mean(~np.isnan(v)) >= 0.5)
        if not names:
            continue
        x = _matrix(feats, names)
        x = x[~np.all(np.isnan(x), axis=1)]
        if len(x) < 200:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            center = np.nanmedian(x, axis=0)
            iqr = np.nanpercentile(x, 75, axis=0) - np.nanpercentile(x, 25, axis=0)
        center = np.nan_to_num(center)
        scale = np.maximum(np.nan_to_num(iqr), 0.01 * np.abs(center) + 1e-6)
        fill = np.array([0.0 if n in ZERO_FILL else c for n, c in zip(names, center)])
        filled = np.where(np.isnan(x), fill, x)
        margin = cfg["range_margin_iqr"] * scale
        model = ComponentModel(names, center, scale, fill, IsolationForest(
            n_estimators=cfg["trees"], max_samples=min(cfg["max_samples"], len(x)), random_state=seed,
        ), 0.0, np.percentile(filled, 0.1, axis=0) - margin, np.percentile(filled, 99.9, axis=0) + margin, len(x))
        z = _prepare(model, x)
        model.forest.fit(z)
        model.threshold = float(np.quantile(-model.forest.score_samples(z), cfg["quantile"]))
        models[component] = model
    return models


def iforest_scores(model: ComponentModel, frame: Frame, component: str) -> tuple[np.ndarray, np.ndarray]:
    """Per point: the anomaly score (above 1 is anomalous) and the index of the
    feature furthest from normal.

    The score is the forest's, relative to its threshold, or, for a point outside
    the envelope of training values, 1 plus how far outside it is (in IQRs). The
    envelope is there because a forest can only isolate values inside the range
    it was trained on: a CPU at 98% when training never saw more than 30% falls
    into the same leaf as the busiest training point and scores no higher.
    """
    x = _matrix(all_features(frame, component), model.features)
    missing = np.all(np.isnan(x), axis=1)
    filled = np.where(np.isnan(x), model.fill, x)
    forest = -model.forest.score_samples((filled - model.center) / model.scale) / model.threshold
    outside = np.maximum(model.low - filled, filled - model.high).clip(min=0) / model.scale
    scores = np.where(outside.max(axis=1) > 0, np.maximum(forest, 1 + outside.max(axis=1)), forest)
    scores[missing] = np.nan
    worst = np.argmax(np.abs(filled - model.center) / model.scale, axis=1)
    return scores, worst


def iforest_alerts(frame: Frame, models: dict[str, ComponentModel], cfg: dict[str, Any]) -> list[Alert]:
    out = []
    for component, model in models.items():
        scores, worst = iforest_scores(model, frame, component)
        out += _alerts(frame, scores, 1.0, cfg["for_seconds"], cfg["clear_seconds"], "AnomalyIForest", component,
                       {}, labels_at=lambda i, m=model, w=worst: {"feature": m.features[w[i]]})
    return out


def save_model(path: Path, models: dict[str, ComponentModel], meta: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump({"format": MODEL_FORMAT, "meta": meta, "components": models}, f)


def load_model(path: Path) -> tuple[dict[str, ComponentModel], dict[str, Any]]:
    """Only load model files you trained yourself: pickle runs code on load."""
    with open(path, "rb") as f:
        data = pickle.load(f)  # noqa: S301 - a local file written by save_model
    if data.get("format") != MODEL_FORMAT:
        raise ValueError(f"{path}: model format {data.get('format')}, expected {MODEL_FORMAT}; retrain")
    return data["components"], data["meta"]


# --- missing telemetry ----------------------------------------------------------------
def absent_alerts(frame: Frame, cfg: dict[str, Any]) -> list[Alert]:
    need = max(1, round(cfg["for_seconds"] / frame.step))
    history = max(1, round(cfg["history_minutes"] * 60 / frame.step))
    out = []
    for component in ANOMALY_COMPONENTS:
        present = presence(frame, component)
        i, n = 0, len(present)
        while i < n:
            if present[i]:
                i += 1
                continue
            j = i
            while j < n and not present[j]:
                j += 1
            before = present[max(0, i - history):i]
            if j - i >= need and len(before) == history and before.mean() >= cfg["min_presence"]:
                out.append(Alert(
                    detector=ANOMALY, name="AnomalyAbsent", component=component,
                    start=float(frame.times[i + need - 1]), end=None if j >= n else float(frame.times[j]),
                    severity="anomaly", labels={},
                ))
            i = j
    return out
