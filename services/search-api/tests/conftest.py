"""A global meter provider that tests can read back, set before the app records anything,
and a search-api backed by an in-memory Redis."""

import json

import fakeredis
import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.telemetry import views

READER = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[READER], views=views()))


@pytest.fixture
def metric_reader():
    return READER


# app.main is imported inside the fixtures, so the meter provider above is in
# place before the app creates its instruments.
@pytest.fixture
def fake_redis():
    from app.store import DOCS_KEY, VERSION_KEY

    client = fakeredis.FakeRedis(decode_responses=True)
    client.hset(DOCS_KEY, mapping={
        "1": json.dumps({"name": "Gaming Laptop", "category": "laptops", "description": "fast", "price": 1299.0}),
        "2": json.dumps({"name": "Wireless Mouse", "category": "accessories", "description": "quiet", "price": 29.0}),
    })
    client.set(VERSION_KEY, 1)
    return client


@pytest.fixture
def client(fake_redis):
    from app.main import app, get_store
    from app.store import RedisStore

    app.dependency_overrides[get_store] = lambda: RedisStore(fake_redis)
    yield TestClient(app)
    app.dependency_overrides.clear()
