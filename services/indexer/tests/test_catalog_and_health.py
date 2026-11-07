import json
import urllib.request

from indexer.catalog_client import fetch_all_products
from indexer.health import CHANGES, HealthState, serve


def test_fetch_all_products_pages_until_short_page():
    calls = []
    pages = {0: [{"id": 1}, {"id": 2}], 1: [{"id": 3}, {"id": 4}], 2: [{"id": 5}]}

    def fake_fetch(url):
        calls.append(url)
        page = int(url.split("page=")[1].split("&")[0])
        return pages[page]

    products = list(fetch_all_products("http://catalog/", fetch=fake_fetch, page_size=2))
    assert [p["id"] for p in products] == [1, 2, 3, 4, 5]
    assert calls[0] == "http://catalog/products?page=0&size=2"
    assert len(calls) == 3


def test_fetch_all_products_stops_on_empty_first_page():
    assert list(fetch_all_products("http://catalog", fetch=lambda url: [], page_size=2)) == []


def test_health_state_goes_stale_without_heartbeat():
    now = [0.0]
    state = HealthState(stale_after_seconds=30, clock=lambda: now[0])
    assert state.alive()
    now[0] = 31.0
    assert not state.alive()
    state.beat()
    assert state.alive()


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as err:
        return err.code, json.load(err)


def test_health_server_exposes_metrics():
    state = HealthState()
    server = serve(state, port=0)
    port = server.server_address[1]
    try:
        CHANGES.labels("applied").inc()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as res:
            body = res.read().decode()
        assert res.status == 200
        assert 'indexer_changes_total{outcome="applied"}' in body
    finally:
        server.shutdown()


def test_health_server_reports_readiness():
    state = HealthState()
    server = serve(state, port=0)
    port = server.server_address[1]
    try:
        assert get(port, "/healthz") == (200, {"status": "ok"})
        assert get(port, "/readyz") == (503, {"status": "indexing"})
        state.ready = True
        assert get(port, "/readyz") == (200, {"status": "ready"})
        assert get(port, "/nope")[0] == 404
    finally:
        server.shutdown()
