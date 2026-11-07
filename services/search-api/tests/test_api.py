import json

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from app.main import app, get_store
from app.store import DOCS_KEY, VERSION_KEY, RedisStore


@pytest.fixture
def fake_redis():
    client = fakeredis.FakeRedis(decode_responses=True)
    client.hset(DOCS_KEY, mapping={
        "1": json.dumps({"name": "Gaming Laptop", "category": "laptops", "description": "fast", "price": 1299.0}),
        "2": json.dumps({"name": "Wireless Mouse", "category": "accessories", "description": "quiet", "price": 29.0}),
    })
    client.set(VERSION_KEY, 1)
    return client


@pytest.fixture
def client(fake_redis):
    app.dependency_overrides[get_store] = lambda: RedisStore(fake_redis)
    yield TestClient(app)
    app.dependency_overrides.clear()


def count(client, status):
    line = (
        'http_server_request_duration_seconds_count{http_request_method="GET",'
        f'http_response_status_code="{status}",http_route="/search"}}'
    )
    for row in client.get("/metrics").text.splitlines():
        if row.startswith(line):
            return float(row.rsplit(" ", 1)[1])
    return 0.0


def test_metrics_count_search_requests_by_status(client):
    before_ok, before_bad = count(client, 200), count(client, 400)
    client.get("/search", params={"q": "mouse"})
    client.get("/search", params={"q": "the"})
    assert count(client, 200) == before_ok + 1
    assert count(client, 400) == before_bad + 1


def test_metrics_ignore_probes(client):
    client.get("/healthz")
    assert 'http_route="/healthz"' not in client.get("/metrics").text


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
