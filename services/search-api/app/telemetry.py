"""OpenTelemetry setup and JSON logging.

Traces and metrics go to the collector over OTLP. Logs stay on stdout, one JSON
object per line with the trace and span ids, and the collector's agent picks
them up from the node. That way a crash loop still leaves logs behind.

Configured through the standard environment variables:
  OTEL_EXPORTER_OTLP_ENDPOINT   collector, e.g. http://otel-agent.monitoring:4318
  OTEL_SERVICE_NAME             defaults to "search-api"
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
from datetime import UTC, datetime

from opentelemetry import metrics, trace

SERVICE_NAME = "search-api"

# 0.3 s must be a bucket boundary: the latency SLO counts requests under 300 ms.
# The semantic-convention defaults jump from 0.25 to 0.5.
DURATION_BUCKETS_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5)

PROBES = frozenset({"/healthz", "/readyz"})


def is_probe(scope) -> bool:
    return scope.get("path") in PROBES


def views():
    """FastAPI records http.server.request.duration itself (natively, since 0.16x),
    with the route and status. This only moves its buckets onto the SLO threshold."""
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    return [
        View(
            instrument_name="http.server.request.duration",
            aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS_SECONDS),
        )
    ]


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
    # uvicorn installs its own plain-text handlers; send its records through ours.
    # uvicorn.access is left alone: --no-access-log keeps probe traffic out.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def enabled() -> bool:
    return os.getenv("OTEL_SDK_DISABLED", "").lower() != "true" and bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def configure() -> None:
    """Set up the SDK and instrument the Redis client.

    FastAPI instruments itself once a global provider exists: one server span per
    request named after the route, and the request duration metric.
    """
    if not enabled():
        return

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.redis import RedisInstrumentor
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
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader], views=views()))

    # One client span per Redis command: the data-store hop of the search path.
    RedisInstrumentor().instrument(tracer_provider=tracer_provider)


def shutdown() -> None:
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        if hasattr(provider, "shutdown"):
            provider.shutdown()
