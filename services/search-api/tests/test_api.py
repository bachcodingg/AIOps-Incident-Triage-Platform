import json

import redis
from fastapi.testclient import TestClient

from app.main import app, get_store
from app.store import DOCS_KEY, VERSION_KEY, RedisStore


def duration_points(reader):
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == "http.server.request.duration"
        for point in metric.data.data_points
    ]


def count(reader, status):
    return sum(p.count for p in duration_points(reader) if p.attributes["http.response.status_code"] == status)


def test_metrics_count_search_requests_by_status(client, metric_reader):
    before_ok, before_bad = count(metric_reader, 200), count(metric_reader, 400)
    client.get("/search", params={"q": "mouse"})
    client.get("/search", params={"q": "the"})
    assert count(metric_reader, 200) == before_ok + 1
    assert count(metric_reader, 400) == before_bad + 1


def test_metrics_use_semconv_names_and_slo_bucket(client, metric_reader):
    client.get("/search", params={"q": "mouse"})
    point = duration_points(metric_reader)[0]
    assert {"http.request.method", "http.route", "http.response.status_code"} <= set(point.attributes)
    assert point.attributes["http.route"] == "/search"
    assert 0.3 in point.explicit_bounds  # the latency SLO threshold


def test_metrics_ignore_probes(client, metric_reader):
    client.get("/healthz")
    client.get("/readyz")
    assert all(p.attributes.get("http.route") not in {"/healthz", "/readyz"} for p in duration_points(metric_reader))


def test_no_prometheus_endpoint(client):
    assert client.get("/metrics").status_code == 404


def test_json_log_lines_carry_trace_ids():
    import json
    import logging

    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    from app.telemetry import JsonFormatter

    span = NonRecordingSpan(SpanContext(0x0AF7651916CD43DD8448EB211C80319C, 0xB7AD6B7169203331, False, TraceFlags(1)))
    record = logging.LogRecord("search-api", logging.WARNING, __file__, 1, "redis %s", ("down",), None)
    with trace.use_span(span):
        line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "redis down"
    assert line["level"] == "warning"
    assert line["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert line["span_id"] == "b7ad6b7169203331"

    assert "trace_id" not in json.loads(JsonFormatter().format(record))


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_ok(client):
    assert client.get("/readyz").status_code == 200


def test_readyz_fails_without_redis():
    class DownStore:
        def ping(self):
            return False

    app.dependency_overrides[get_store] = lambda: DownStore()
    try:
        assert TestClient(app).get("/readyz").status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_search_returns_ranked_results(client):
    body = client.get("/search", params={"q": "gaming lapto"}).json()
    assert body["corrected_query"] == "gaming laptop"
    assert body["results"][0]["id"] == 1
    assert body["cached"] is False


def test_second_identical_search_is_cached(client):
    client.get("/search", params={"q": "mouse"})
    assert client.get("/search", params={"q": "Mouse"}).json()["cached"] is True


def test_index_change_invalidates_cache(client, fake_redis):
    client.get("/search", params={"q": "keyboard"})
    fake_redis.hset(DOCS_KEY, "3", json.dumps({"name": "Mechanical Keyboard", "category": "accessories"}))
    fake_redis.incr(VERSION_KEY)
    body = client.get("/search", params={"q": "keyboard"}).json()
    assert body["cached"] is False
    assert body["results"][0]["id"] == 3


def test_query_validation(client):
    assert client.get("/search").status_code == 422
    assert client.get("/search", params={"q": "the"}).status_code == 400
    assert client.get("/search", params={"q": "mouse", "limit": 0}).status_code == 422


def test_redis_failure_returns_503():
    broken = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.1)
    app.dependency_overrides[get_store] = lambda: RedisStore(broken)
    try:
        assert TestClient(app).get("/search", params={"q": "mouse"}).status_code == 503
    finally:
        app.dependency_overrides.clear()
