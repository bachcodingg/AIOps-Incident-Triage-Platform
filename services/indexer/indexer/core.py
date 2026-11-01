"""Index maintenance: turn catalog products and change events into the
Redis structures that search-api reads.

Redis contract:
  catalog:changes        list, change events LPUSHed by catalog, BRPOPed here
  catalog:changes:dead   list, events that could not be applied
  search:docs            hash, product id -> product JSON
  search:version         integer, bumped after every write to search:docs

A change event looks like:
  {"op": "upsert", "id": 7, "product": {...}}   or   {"op": "delete", "id": 7}
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

import redis

QUEUE_KEY = "catalog:changes"
DEAD_LETTER_KEY = "catalog:changes:dead"
DOCS_KEY = "search:docs"
VERSION_KEY = "search:version"
INDEXED_FIELDS = ("id", "sku", "name", "description", "category", "price")

log = logging.getLogger("indexer")


class InvalidChange(ValueError):
    pass


def to_doc(product: dict) -> dict:
    if "id" not in product or "name" not in product:
        raise InvalidChange(f"product is missing id or name: {product!r}")
    doc = {f: product.get(f) for f in INDEXED_FIELDS}
    doc["id"] = int(doc["id"])
    if doc["price"] is not None:
        doc["price"] = float(doc["price"])
    return doc


def full_reindex(client: redis.Redis, products: Iterable[dict]) -> int:
    """Replace the whole index atomically: readers see the old or the new index, never a mix."""
    docs = {str(d["id"]): json.dumps(d) for d in map(to_doc, products)}
    tmp_key = f"{DOCS_KEY}:rebuild"
    pipe = client.pipeline(transaction=True)
    pipe.delete(tmp_key)
    if docs:
        pipe.hset(tmp_key, mapping=docs)
        pipe.rename(tmp_key, DOCS_KEY)
    else:
        pipe.delete(DOCS_KEY)
    pipe.incr(VERSION_KEY)
    pipe.execute()
    return len(docs)


def parse_change(raw: str) -> dict:
    try:
        change = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidChange(f"not JSON: {raw[:200]!r}") from exc
    if not isinstance(change, dict) or change.get("op") not in ("upsert", "delete") or "id" not in change:
        raise InvalidChange(f"unknown change shape: {raw[:200]!r}")
    if change["op"] == "upsert" and not isinstance(change.get("product"), dict):
        raise InvalidChange(f"upsert without product: {raw[:200]!r}")
    return change


def apply_change(client: redis.Redis, raw: str) -> bool:
    """Apply one change event. Bad events go to the dead-letter list instead of crashing the worker."""
    try:
        change = parse_change(raw)
        pipe = client.pipeline(transaction=True)
        if change["op"] == "upsert":
            doc = to_doc({**change["product"], "id": change["id"]})
            pipe.hset(DOCS_KEY, str(doc["id"]), json.dumps(doc))
        else:
            pipe.hdel(DOCS_KEY, str(int(change["id"])))
        pipe.incr(VERSION_KEY)
        pipe.execute()
        return True
    except (InvalidChange, TypeError, ValueError) as exc:
        log.warning("dead-lettering change: %s", exc)
        client.lpush(DEAD_LETTER_KEY, raw)
        return False


def consume_once(client: redis.Redis, timeout_seconds: int = 5) -> bool | None:
    """Block for at most `timeout_seconds` waiting for one event. Returns None on timeout."""
    item = client.brpop([QUEUE_KEY], timeout=timeout_seconds)
    if item is None:
        return None
    _, raw = item
    return apply_change(client, raw)
