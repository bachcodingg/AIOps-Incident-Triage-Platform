import numpy as np
import pytest

from detector.alerts import Alert, component_of, read_alerts, write_jsonl
from detector.anomaly import (
    absent_alerts, intervals, iforest_alerts, load_model, mad_alerts, rolling_scores, save_model, train_iforest,
)
from detector.cli import detect, load_config, main
from detector.series import Frame, load

from .synth import CHUNK, T0, make_chunks, write_recording

CONFIG = load_config()


def between(start, end):
    return lambda t: start <= t < end


def errors_on(component, start, end, ratio=0.10):
    def modify(metric, comp, t, v):
        if comp == component and metric == "errors" and start <= t < end:
            return 8.0 * ratio if comp == "gateway" else {"search-api": 6.0, "catalog": 2.0}[comp] * ratio
        return v
    return modify


def test_rolling_scores_flag_a_jump_and_ignore_noise():
    rng = np.random.default_rng(1)
    x = 1 + rng.normal(0, 0.02, 400)
    x[300:320] = 2.0
    z = rolling_scores(x, window=120, min_points=60, relative=0.0, absolute=0.0)
    assert np.isnan(z[:60]).all()            # not enough history yet
    assert np.nanmax(np.abs(z[60:300])) < 6  # noise stays under the threshold
    assert (z[300:320] > 6).all()


def test_intervals_need_a_streak_to_fire_and_to_resolve():
    e = np.array([0, 1, 0, 1, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 1], dtype=bool)
    # on after 3 in a row, off after 2 in a row; still firing at the end
    assert intervals(e, step=15, for_s=45, clear_s=30) == [(5, 9, 3), (14, None, 12)]


def test_mad_finds_a_bad_release_on_the_right_service_and_nothing_on_clean_traffic():
    clean, _ = load(make_chunks(3))
    assert mad_alerts(clean, CONFIG["mad"]) == []

    start = T0 + 2 * 3600
    frame, _ = load(make_chunks(3, modify=errors_on("catalog", start, start + 900)))
    found = mad_alerts(frame, CONFIG["mad"])
    assert {(a.component, a.labels["metric"]) for a in found} == {("catalog", "error_ratio")}
    alert = found[0]
    assert start < alert.start <= start + 120  # fires after `for` (60 s), well inside the plan's 2 minutes
    assert alert.end is not None and alert.score > CONFIG["mad"]["threshold"]


def test_traffic_drops_are_anomalies_too():
    start = T0 + 2 * 3600

    def drop(metric, comp, t, v):
        return v * 0.1 if comp == "gateway" and metric == "requests" and start <= t < start + 600 else v

    frame, _ = load(make_chunks(3, modify=drop))
    found = [a for a in mad_alerts(frame, CONFIG["mad"]) if a.labels["metric"] == "requests"]
    assert [(a.component, a.labels["direction"]) for a in found] == [("gateway", "down")]


def test_absent_fires_when_a_service_goes_silent_but_not_at_the_start_of_data():
    start = T0 + 2 * 3600

    def crash(metric, comp, t, v):
        return None if comp == "search-api" and metric in ("requests", "errors", "latency_p95") and t >= start else v

    frame, _ = load(make_chunks(3, modify=crash))
    found = absent_alerts(frame, CONFIG["absent"])
    assert [(a.component, a.end) for a in found] == [("search-api", None)]
    assert found[0].start == pytest.approx(start + CONFIG["absent"]["for_seconds"] - 15)


def test_isolation_forest_learns_normal_and_flags_a_saturated_pod(tmp_path):
    baseline, _ = load(make_chunks(4, seed=3))
    models = train_iforest(baseline, CONFIG["iforest"], seed=0)
    assert set(models) == {"gateway", "search-api", "catalog", "indexer", "redis", "postgres"}
    assert "error_ratio" in models["catalog"].features and "cpu" in models["redis"].features

    path = tmp_path / "model.pkl"
    save_model(path, models, {"hours": 4})
    models, meta = load_model(path)
    assert meta == {"hours": 4}

    start = T0 + 4 * 3600 + 3600

    def cpu_hog(metric, comp, t, v):
        return 0.98 if comp == "postgres" and metric == "cpu" and start <= t < start + 900 else v

    frame, _ = load(make_chunks(3, start=T0 + 4 * 3600, seed=4, modify=cpu_hog))
    found = iforest_alerts(frame, models, CONFIG["iforest"])
    hits = [a for a in found if a.component == "postgres" and a.start >= start]
    assert hits and hits[0].start <= start + 120
    assert hits[0].labels == {"feature": "cpu"}  # what was abnormal, for whoever reads the alert
    # Fault-free hours raise at most a stray alert or two (the threshold allows 0.1% of points).
    assert len([a for a in found if a.start < start]) <= 2


def test_static_and_burn_rate_alerts_come_back_from_the_recorded_alerts_series():
    alerts = [
        ({"alertname": "StaticHighErrorRate", "detector": "static", "severity": "baseline", "component": "catalog",
          "job": "shopsearch/catalog"}, T0 + 600, T0 + 1200),
        ({"alertname": "ProductsAvailabilityBudgetBurn", "detector": "burn-rate", "severity": "page",
          "sloth_slo": "products-availability"}, T0 + 630, T0 + 3600),  # still firing when the data ends
    ]
    _, found = load(make_chunks(1, alerts=alerts))
    assert [(a.detector, a.name, a.component, a.start, a.end) for a in found] == [
        ("static", "StaticHighErrorRate", "catalog", T0 + 600, T0 + 1200),
        ("burn-rate", "ProductsAvailabilityBudgetBurn", "gateway", T0 + 630, None),
    ]
    assert found[0].labels == {"job": "shopsearch/catalog"}


def test_an_alert_that_stops_and_starts_again_is_two_alerts():
    labels = {"alertname": "StaticPodNotReady", "detector": "static", "component": "redis"}
    _, found = load(make_chunks(1, alerts=[(labels, T0, T0 + 300), (labels, T0 + 900, T0 + 1200)]))
    assert [(a.start, a.end) for a in found] == [(T0, T0 + 300), (T0 + 900, T0 + 1200)]


@pytest.mark.parametrize("labels, component", [
    ({"component": "redis"}, "redis"),
    ({"sloth_slo": "search-latency"}, "gateway"),
    ({"job": "shopsearch/catalog"}, "catalog"),
    ({"container": "postgres"}, "postgres"),
    ({"node": "incidentlens-worker"}, "node"),
    ({}, "unknown"),
])
def test_component_of(labels, component):
    assert component_of(labels) == component


def test_frame_sum_keeps_missing_as_missing():
    f = Frame(0, 60, 15)
    f.add("x", {"a": "1"}, [(0, 1.0), (15, 2.0)])
    f.add("x", {"a": "2"}, [(15, 3.0)])
    assert np.array_equal(f.sum("x"), np.array([1.0, 5.0, np.nan, np.nan]), equal_nan=True)


def test_alerts_round_trip_through_jsonl(tmp_path):
    a = Alert("anomaly", "AnomalyMAD", "catalog", T0, None, "anomaly", {"metric": "error_ratio"}, 9.5)
    write_jsonl(tmp_path / "a.jsonl", [a])
    assert read_alerts(tmp_path / "a.jsonl") == [a]


def test_detect_end_to_end_from_a_recording(tmp_path):
    start = T0 + 2 * 3600
    alerts = [({"alertname": "StaticHighErrorRate", "detector": "static", "component": "catalog"}, start + 120, start + 900)]
    rec = write_recording(tmp_path / "rec", make_chunks(3, modify=errors_on("catalog", start, start + 900), alerts=alerts))
    assert main(["run", "--recording", str(rec), "--out", str(tmp_path / "alerts.jsonl")]) == 0
    found = read_alerts(tmp_path / "alerts.jsonl")
    assert {(a.detector, a.component) for a in found} == {("static", "catalog"), ("anomaly", "catalog")}
    # Training refuses less than a day of traffic: a model that never saw a night would flag every night.
    assert main(["train", "--recording", str(rec), "--out", str(tmp_path / "m.pkl")]) == 2
    assert detect(iter(make_chunks(1)), CONFIG) == []
    assert CHUNK == 300
