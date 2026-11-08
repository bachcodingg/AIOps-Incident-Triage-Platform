import json
import logging

import fakeredis
import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, StatusCode, TraceFlags

from indexer import core, telemetry

PRODUCER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PRODUCER_SPAN = "00f067aa0ba902b7"
PHONE = {"id": 2, "name": "Smartphone", "category": "phones", "price": 899}


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


def test_event_is_applied_in_the_trace_of_the_request_that_produced_it(r, spans):
    event = {"op": "upsert", "id": 2, "product": PHONE, "traceparent": f"00-{PRODUCER_TRACE}-{PRODUCER_SPAN}-01"}
    r.lpush(core.QUEUE_KEY, json.dumps(event))

    assert core.consume_once(r, timeout_seconds=1) is True

    [span] = [s for s in spans.get_finished_spans() if s.name == "process catalog:changes"]
    assert span.kind == SpanKind.CONSUMER
    assert format(span.context.trace_id, "032x") == PRODUCER_TRACE
    assert format(span.parent.span_id, "016x") == PRODUCER_SPAN
    assert span.attributes["messaging.destination.name"] == "catalog:changes"


def test_event_without_trace_context_starts_a_new_trace(r, spans):
    r.lpush(core.QUEUE_KEY, json.dumps({"op": "delete", "id": 2}))
    core.consume_once(r, timeout_seconds=1)
    [span] = [s for s in spans.get_finished_spans() if s.name == "process catalog:changes"]
    assert span.parent is None


def test_dead_lettered_event_marks_the_span_as_error(r, spans):
    r.lpush(core.QUEUE_KEY, "not json")
    assert core.consume_once(r, timeout_seconds=1) is False
    [span] = [s for s in spans.get_finished_spans() if s.name == "process catalog:changes"]
    assert span.status.status_code == StatusCode.ERROR


def test_idle_poll_makes_no_spans(r, spans):
    assert core.consume_once(r, timeout_seconds=1) is None
    assert spans.get_finished_spans() == ()


def test_full_reindex_is_one_span(r, spans):
    core.full_reindex(r, [PHONE])
    assert [s.name for s in spans.get_finished_spans()] == ["full reindex"]


def points(reader, name):
    data = reader.get_metrics_data()
    return [
        p
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == name
        for p in m.data.data_points
    ]


def test_change_counter_and_readiness_gauge(metric_reader):
    telemetry.CHANGES.add(1, {"outcome": "dead-lettered"})
    ready = [False]
    telemetry.observe_readiness(lambda: ready[0])

    assert [p.value for p in points(metric_reader, "indexer.ready")] == [0]
    ready[0] = True
    assert [p.value for p in points(metric_reader, "indexer.ready")] == [1]
    dead = [p.value for p in points(metric_reader, "indexer.changes") if p.attributes["outcome"] == "dead-lettered"]
    assert dead and dead[0] >= 1


def test_json_log_lines_carry_trace_ids():
    span = NonRecordingSpan(SpanContext(int(PRODUCER_TRACE, 16), int(PRODUCER_SPAN, 16), False, TraceFlags(1)))
    record = logging.LogRecord("indexer", logging.INFO, __file__, 1, "change %s", ("applied",), None)
    with trace.use_span(span):
        line = json.loads(telemetry.JsonFormatter().format(record))
    assert line == {
        "time": line["time"],
        "level": "info",
        "logger": "indexer",
        "msg": "change applied",
        "trace_id": PRODUCER_TRACE,
        "span_id": PRODUCER_SPAN,
    }
