"""Global providers that tests can read back, set before the code records anything."""

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SPANS = InMemorySpanExporter()
_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_tracer_provider)

READER = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[READER]))


@pytest.fixture
def spans():
    SPANS.clear()
    return SPANS


@pytest.fixture
def metric_reader():
    return READER
