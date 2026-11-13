import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.faults import Faults


def test_off_by_default():
    faults = Faults.from_env({})
    assert (faults.error_rate, faults.latency_ms, faults.leak_kb_per_request) == (0, 0, 0)


@pytest.mark.parametrize(
    "env",
    [{"FAULT_ERROR_RATE": "1.5"}, {"FAULT_ERROR_RATE": "high"}, {"FAULT_LATENCY_MS": "-1"}, {"FAULT_LEAK_KB_PER_REQUEST": "-2"}],
)
def test_rejects_bad_values(env):
    with pytest.raises(ValueError, match=next(iter(env))):
        Faults.from_env(env)


@pytest.fixture
def with_faults(monkeypatch):
    def install(**kwargs):
        faults = Faults(**kwargs)
        monkeypatch.setattr(main, "FAULTS", faults)
        return faults

    return install


def test_error_rate_fails_like_a_real_bug(client, with_faults):
    draws = iter([0.01, 0.9])
    with_faults(error_rate=0.05, random=lambda: next(draws))
    server = TestClient(main.app, raise_server_exceptions=False)

    failed = server.get("/search", params={"q": "laptop"})
    assert failed.status_code == 500
    assert "fault" not in failed.text.lower() and "inject" not in failed.text.lower()
    assert server.get("/search", params={"q": "laptop"}).status_code == 200


def test_latency_is_spent_on_searches_only(client, with_faults):
    slept = []
    with_faults(latency_ms=250, sleep=slept.append)

    assert client.get("/healthz").status_code == 200
    assert slept == []
    assert client.get("/search", params={"q": "laptop"}).status_code == 200
    assert slept == [0.25]


def test_leak_keeps_memory_after_every_search(client, with_faults):
    faults = with_faults(leak_kb_per_request=64)

    for _ in range(3):
        assert client.get("/search", params={"q": "laptop"}).status_code == 200
    assert len(faults.retained) == 3
    assert sum(map(len, faults.retained)) == 3 * 64 * 1024
