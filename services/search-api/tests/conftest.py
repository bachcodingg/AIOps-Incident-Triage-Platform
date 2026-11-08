"""A global meter provider that tests can read back, set before the app records anything."""

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.telemetry import views

READER = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[READER], views=views()))


@pytest.fixture
def metric_reader():
    return READER
