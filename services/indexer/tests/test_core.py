import json

import fakeredis
import pytest

from indexer import core

LAPTOP = {"id": 1, "sku": "LT-1", "name": "Gaming Laptop", "description": "fast", "category": "laptops", "price": "1299.00", "stock": 4}
PHONE = {"id": 2, "sku": "PH-2", "name": "Smartphone", "description": "small", "category": "phones", "price": 899}


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


def docs(r):
    return {int(k): json.loads(v) for k, v in r.hgetall(core.DOCS_KEY).items()}


def test_to_doc_keeps_only_indexed_fields_and_normalises_price():
    doc = core.to_doc(LAPTOP)
    assert doc == {"id": 1, "sku": "LT-1", "name": "Gaming Laptop", "description": "fast", "category": "laptops", "price": 1299.0}


def test_to_doc_rejects_product_without_name():
    with pytest.raises(core.InvalidChange):
        core.to_doc({"id": 3})


def test_full_reindex_replaces_old_documents(r):
    r.hset(core.DOCS_KEY, "99", json.dumps({"id": 99, "name": "stale"}))
    assert core.full_reindex(r, [LAPTOP, PHONE]) == 2
    assert set(docs(r)) == {1, 2}
    assert int(r.get(core.VERSION_KEY)) == 1


def test_full_reindex_with_empty_catalog_clears_index(r):
    core.full_reindex(r, [LAPTOP])
    core.full_reindex(r, [])
    assert docs(r) == {}
    assert int(r.get(core.VERSION_KEY)) == 2


def test_upsert_then_delete(r):
    assert core.apply_change(r, json.dumps({"op": "upsert", "id": 2, "product": PHONE}))
    assert docs(r)[2]["name"] == "Smartphone"
    assert core.apply_change(r, json.dumps({"op": "delete", "id": 2}))
    assert docs(r) == {}
    assert int(r.get(core.VERSION_KEY)) == 2


def test_upsert_uses_event_id_over_product_id(r):
    core.apply_change(r, json.dumps({"op": "upsert", "id": 5, "product": {**PHONE, "id": 2}}))
    assert set(docs(r)) == {5}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"op": "rename", "id": 1}),
        json.dumps({"op": "upsert", "id": 1}),
        json.dumps({"op": "upsert", "id": 1, "product": {"description": "no name"}}),
        json.dumps({"op": "delete", "id": "abc"}),
        json.dumps([1, 2]),
    ],
)
def test_bad_events_are_dead_lettered(r, raw):
    assert core.apply_change(r, raw) is False
    assert r.lrange(core.DEAD_LETTER_KEY, 0, -1) == [raw]
    assert r.get(core.VERSION_KEY) is None


def test_consume_once_processes_in_fifo_order(r):
    r.lpush(core.QUEUE_KEY, json.dumps({"op": "upsert", "id": 1, "product": LAPTOP}))
    r.lpush(core.QUEUE_KEY, json.dumps({"op": "upsert", "id": 1, "product": {**LAPTOP, "name": "Renamed Laptop"}}))
    assert core.consume_once(r, timeout_seconds=1) is True
    assert core.consume_once(r, timeout_seconds=1) is True
    assert docs(r)[1]["name"] == "Renamed Laptop"


def test_consume_once_times_out_on_empty_queue(r):
    assert core.consume_once(r, timeout_seconds=1) is None
