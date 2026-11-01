"""Redis access for the search index written by the indexer.

Contract with the indexer (see services/indexer/indexer/core.py):
  search:docs     hash, product id -> product JSON
  search:version  integer, bumped on every change to search:docs
"""

from __future__ import annotations

import json
import threading
from typing import Protocol

import redis

from .search import Index

DOCS_KEY = "search:docs"
VERSION_KEY = "search:version"
CACHE_PREFIX = "search:cache"


class Store(Protocol):
    def index(self) -> tuple[int, Index]: ...
    def cached(self, key: str) -> str | None: ...
    def cache(self, key: str, value: str, ttl_seconds: int) -> None: ...
    def ping(self) -> bool: ...


class RedisStore:
    def __init__(self, client: redis.Redis):
        self._client = client
        self._lock = threading.Lock()
        self._version: int | None = None
        self._index = Index.build({})

    def index(self) -> tuple[int, Index]:
        # Rebuild the in-memory index only when the indexer bumped the version.
        version = int(self._client.get(VERSION_KEY) or 0)
        with self._lock:
            if version != self._version:
                raw = self._client.hgetall(DOCS_KEY)
                docs = {int(k): json.loads(v) for k, v in raw.items()}
                self._index = Index.build(docs)
                self._version = version
            return version, self._index

    def cached(self, key: str) -> str | None:
        return self._client.get(f"{CACHE_PREFIX}:{key}")

    def cache(self, key: str, value: str, ttl_seconds: int) -> None:
        self._client.set(f"{CACHE_PREFIX}:{key}", value, ex=ttl_seconds)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.RedisError:
            return False
