from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from functools import lru_cache
from typing import Annotated

import redis
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .metrics import track_request
from .search import search, tokenize
from .store import RedisStore, Store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("search-api")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))

app = FastAPI(title="search-api", version="0.1.0")
app.middleware("http")(track_request)


@lru_cache
def get_store() -> Store:
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0)
    return RedisStore(client)


StoreDep = Annotated[Store, Depends(get_store)]


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz(store: StoreDep):
    if not store.ping():
        return JSONResponse({"status": "redis_unavailable"}, status_code=503)
    return {"status": "ready"}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/search")
def search_endpoint(
    store: StoreDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict:
    if not tokenize(q):
        raise HTTPException(status_code=400, detail="query has no searchable terms")
    try:
        version, index = store.index()
        # The index version is part of the cache key, so a catalog change
        # invalidates every cached result without an explicit purge.
        key = f"{version}:{limit}:{' '.join(tokenize(q))}"
        hit = store.cached(key)
        if hit is not None:
            return {**json.loads(hit), "query": q, "cached": True}
        result = asdict(search(index, q, limit))
        store.cache(key, json.dumps(result), CACHE_TTL_SECONDS)
    except redis.RedisError as exc:
        log.warning("redis error during search: %s", exc)
        raise HTTPException(status_code=503, detail="search index unavailable") from exc
    return {**result, "cached": False}
