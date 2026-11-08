"""OpenTelemetry setup, worker metrics and JSON logging.

Traces and metrics go to the collector over OTLP. Logs stay on stdout, one JSON
object per line with the trace and span ids, and the collector's agent picks
them up from the node.

Configured through the standard environment variables:
  OTEL_EXPORTER_OTLP_ENDPOINT   collector, e.g. http://otel-agent.monitoring:4318
  OTEL_SERVICE_NAME             defaults to "indexer"
  OTEL_RESOURCE_ATTRIBUTES      pod name, rollout hash, ... (set by the chart)
  OTEL_METRIC_EXPORT_INTERVAL   milliseconds, default 15000
  OTEL_SDK_DISABLED=true        turns everything off
Without an endpoint only logging is set up, so tests run offline.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation

SERVICE_NAME = "indexer"

_meter = metrics.get_meter("indexer")

# indexer_changes_total{outcome} in Prometheus. Canary analysis fails a release
# that dead-letters events.
CHANGES = _meter.create_counter("indexer.changes", unit="{change}", description="Catalog change events handled.")


def observe_readiness(is_ready: Callable[[], bool]) -> None:
    """indexer_ready in Prometheus: 1 once the first full reindex is done.

    A worker has no request rate, so canary analysis uses this to tell that a
    new release is up and exporting."""

    def callback(_options: CallbackOptions):
        yield Observation(1 if is_ready() else 0)

    _meter.create_observable_gauge("indexer.ready", callbacks=[callback], description="1 when ready to apply changes.")


def trace_fields(span: trace.Span | None = None) -> dict[str, str]:
    ctx = (span or trace.get_current_span()).get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **trace_fields(),
        }
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line)


def configure_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.getenv("LOG_LEVEL", "INFO"))


def enabled() -> bool:
    return os.getenv("OTEL_SDK_DISABLED", "").lower() != "true" and bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def configure() -> None:
    """Set up the SDK and instrument Redis and the catalog HTTP client."""
    if not enabled():
        return

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.urllib import URLLibInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Resource.create also reads OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES.
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", SERVICE_NAME)})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(), export_interval_millis=int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "15000"))
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    RedisInstrumentor().instrument(tracer_provider=tracer_provider)
    # The full reindex pages through catalog; this propagates the trace to it.
    URLLibInstrumentor().instrument(tracer_provider=tracer_provider)


def shutdown() -> None:
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        if hasattr(provider, "shutdown"):
            provider.shutdown()
