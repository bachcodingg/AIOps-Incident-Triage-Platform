import gzip
import json

import pytest

from recorder.kube import KubeError
from recorder.record import (
    chunk_name, condense_trace, file_sha256, iso, load_queries, parse_iso, record, record_chunk, write_chunk,
)
from recorder.store import Recording

from .synth import CHUNK, T0, make_chunks, write_recording

Q = load_queries()


class FakeKube:
    """Answers like the real backends; `broken` names sections that fail."""

    def __init__(self, broken=()):
        self.broken = set(broken)
        self.calls = []

    def prom_range(self, query, start, end, step):
        self.calls.append(("prom", query, start, end, step))
        if "prom" in self.broken:
            raise KubeError("Prometheus is down")
        if query.startswith("ALERTS"):
            return [{"metric": {"__name__": "ALERTS", "alertname": "StaticHighErrorRate", "alertstate": "firing",
                                "detector": "static", "component": "catalog"}, "values": [[start, "1"]]}]
        return [{"metric": {"job": "shopsearch/gateway"}, "values": [[start, "1.5"], [start + step, "NaN"]]}]

    def get(self, kind, namespace):
        self.calls.append(("get", kind, namespace))
        if kind == "events":
            return [
                {"lastTimestamp": iso(T0 + 10), "type": "Warning", "reason": "BackOff", "count": 3,
                 "involvedObject": {"kind": "Pod", "name": "catalog-1"}, "message": "Back-off restarting"},
                {"lastTimestamp": iso(T0 - 600), "type": "Normal", "reason": "Old",
                 "involvedObject": {"kind": "Pod", "name": "x"}},
            ] if namespace == "shopsearch" else []
        if kind.startswith("applications"):
            return [{"metadata": {"name": "catalog"},
                     "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"},
                                "history": [{"id": 4, "deployedAt": iso(T0 + 60), "revision": "abc",
                                             "source": {"helm": {"parameters": [{"name": "image.tag", "value": "3f9c2a1"}]}}},
                                            {"id": 3, "deployedAt": iso(T0 - 3600), "revision": "old"}]}}]
        if kind.startswith("rollouts"):
            return [{"metadata": {"name": "catalog"},
                     "spec": {"template": {"spec": {"containers": [{"image": "incidentlens/catalog:3f9c2a1"}]}}},
                     "status": {"phase": "Healthy", "currentPodHash": "h2", "stableRS": "h2"}}]
        return []

    def loki_range(self, query, start, end, limit=None, step=None):
        if "loki" in self.broken:
            raise KubeError("Loki is down")
        if step:
            return {"result": [{"metric": {"service_name": "catalog", "detected_level": "error"}, "values": [[start, "4"]]}]}
        return {"result": [{"stream": {"service_name": "catalog", "detected_level": "error", "trace_id": "t1"},
                            "values": [[str(int((start + 5) * 1e9)), "connection pool exhausted"]]}]}

    def tempo_search(self, traceql, start, end, limit):
        if "error" in traceql:
            return [{"traceID": "t1", "rootServiceName": "gateway", "rootTraceName": "GET /api/products/:id",
                     "durationMs": 1200, "startTimeUnixNano": str(int(start * 1e9))}]
        return [{"traceID": "t2", "rootServiceName": "gateway", "durationMs": 400, "startTimeUnixNano": "0"}]

    def tempo_trace(self, trace_id):
        return {"batches": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "catalog"}}]},
                             "scopeSpans": [{"spans": [
                                 {"name": "SELECT", "spanId": "b", "parentSpanId": "a", "kind": 3,
                                  "startTimeUnixNano": "2000000", "endTimeUnixNano": "902000000",
                                  "attributes": [{"key": "db.system.name", "value": {"stringValue": "postgresql"}},
                                                 {"key": "irrelevant", "value": {"stringValue": "x"}}]},
                                 {"name": "GET /products/{id}", "spanId": "a", "kind": 2, "status": {"code": 2},
                                  "startTimeUnixNano": "1000000", "endTimeUnixNano": "1001000000"}]}]}]}


def test_a_chunk_has_every_section_on_the_5_minute_grid():
    kube = FakeKube()
    chunk = record_chunk(kube, Q, T0, T0 + CHUNK, now=T0 + 400)
    assert chunk["start"] == iso(T0) and chunk["end"] == iso(T0 + CHUNK) and chunk["errors"] == {}
    assert set(chunk["metrics"]) == set(Q["metrics"])
    assert chunk["metrics"]["requests"][0]["values"] == [[T0, 1.5], [T0 + 15, None]]  # NaN stored as null
    # Prometheus' range end is inclusive: the chunk stops one step before the next one starts.
    assert all(c[3] == T0 + CHUNK - 15 for c in kube.calls if c[0] == "prom")
    assert chunk["alerts"][0]["labels"]["detector"] == "static"
    assert [e["reason"] for e in chunk["events"]] == ["BackOff"]  # the hour-old event belongs to another chunk
    assert [h["id"] for h in chunk["deploys"]["history"]] == [4]
    assert chunk["deploys"]["history"][0]["parameters"] == {"image.tag": "3f9c2a1"}
    assert chunk["logs"]["lines"][0]["line"] == "connection pool exhausted"
    assert chunk["traces"]["full"][0]["trace_id"] == "t1"  # the failed trace before the slow one


def test_a_failing_backend_costs_only_its_own_sections():
    chunk = record_chunk(FakeKube(broken={"loki"}), Q, T0, T0 + CHUNK, now=T0 + 400)
    assert chunk["logs"] is None and "Loki is down" in chunk["errors"]["logs"]
    assert chunk["traces"] is not None and chunk["metrics"]["requests"]

    chunk = record_chunk(FakeKube(broken={"prom"}), Q, T0, T0 + CHUNK, now=T0 + 400)
    assert chunk["metrics"]["requests"] is None and "metrics.requests" in chunk["errors"]
    assert chunk["events"]


def test_events_are_marked_incomplete_when_recorded_too_late():
    assert record_chunk(FakeKube(), Q, T0, T0 + CHUNK, now=T0 + 400)["events_complete"]
    assert not record_chunk(FakeKube(), Q, T0, T0 + CHUNK, now=T0 + 7200)["events_complete"]


def test_condensed_traces_keep_the_span_tree_and_the_useful_attributes():
    t = condense_trace(FakeKube().tempo_trace("t1"), "t1")
    assert [s["name"] for s in t["spans"]] == ["GET /products/{id}", "SELECT"]
    root, query = t["spans"]
    assert root["offset_ms"] == 0 and query["offset_ms"] == 1.0 and query["duration_ms"] == 900.0
    assert query["parent_id"] == "a" and query["attributes"] == {"db.system.name": "postgresql"}


def test_chunks_are_byte_identical_when_their_content_is(tmp_path):
    chunk = make_chunks(5 / 60)[0]
    a = write_chunk(tmp_path / "a", chunk)
    b = write_chunk(tmp_path / "b", json.loads(json.dumps(chunk)))
    assert file_sha256(a) == file_sha256(b)
    assert json.loads(gzip.decompress(a.read_bytes())) == chunk


def test_live_recording_waits_for_each_chunk_to_settle_and_resumes(tmp_path):
    now = [T0 + 10]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    kube = FakeKube()
    write_recording(tmp_path, [])
    n = record(kube, tmp_path, Q, start=T0 + 10, end=T0 + 2 * CHUNK, lag_s=90, clock=lambda: now[0],
               sleep=sleep, log=lambda _: None)
    assert n == 2
    assert now[0] >= T0 + 2 * CHUNK + 90  # the second chunk was recorded only after it settled
    assert [p.name for p in Recording(tmp_path).chunk_paths()] == [chunk_name(T0), chunk_name(T0 + CHUNK)]
    # Run again: nothing new to record, nothing waited for.
    slept.clear()
    assert record(kube, tmp_path, Q, start=T0, end=T0 + 2 * CHUNK, clock=lambda: now[0], sleep=sleep,
                  log=lambda _: None) == 0
    assert slept == []


def test_recording_knows_its_gaps_and_its_hash(tmp_path):
    chunks = make_chunks(0.5)
    rec = Recording(write_recording(tmp_path, chunks[:2] + chunks[3:]))
    assert rec.span() == (T0, T0 + 6 * CHUNK)
    assert rec.gaps() == [(T0 + 2 * CHUNK, T0 + 3 * CHUNK)]
    assert rec.covered(T0, T0 + 2 * CHUNK)
    assert not rec.covered(T0 + CHUNK, T0 + 4 * CHUNK)
    before = rec.sha256()
    (tmp_path / "chunks" / chunk_name(T0)).write_bytes(b"changed")
    assert rec.sha256() != before


def test_a_recording_refuses_other_queries(tmp_path):
    from recorder.record import write_meta

    write_recording(tmp_path, [])
    other = tmp_path / "other.yaml"
    other.write_text("step_seconds: 30\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        write_meta(tmp_path, name="x", context="c", identity=None, chunk_s=300, queries_path=other)


def test_queries_never_reach_into_the_chaos_namespace():
    text = json.dumps(Q)
    assert "chaos" not in text
    for name, expr in Q["metrics"].items():
        assert "namespace=\"shopsearch\"" in expr or "shopsearch/" in expr or name in {
            "nodes_ready", "coredns_available", "db_size", "db_backends", "graph_requests", "graph_failed",
        }, name


def test_parse_iso_round_trips():
    assert parse_iso(iso(T0)) == T0
