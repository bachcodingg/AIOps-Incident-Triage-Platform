// OpenTelemetry SDK setup. server.ts imports this before anything else, so the
// http module is patched before Fastify loads it.
//
// Configured through the standard environment variables:
//   OTEL_EXPORTER_OTLP_ENDPOINT   collector, e.g. http://otel-agent.monitoring:4318
//   OTEL_SERVICE_NAME             defaults to "gateway"
//   OTEL_RESOURCE_ATTRIBUTES      pod name, rollout hash, ... (set by the chart)
//   OTEL_METRIC_EXPORT_INTERVAL   milliseconds, default 15000
//   OTEL_SDK_DISABLED=true        turns everything off
// Without an endpoint nothing is set up, so tests and `npm start` work offline.
import { context, metrics, propagation, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { CompositePropagator, W3CBaggagePropagator, W3CTraceContextPropagator } from "@opentelemetry/core";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-proto";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { HttpInstrumentation } from "@opentelemetry/instrumentation-http";
import { UndiciInstrumentation } from "@opentelemetry/instrumentation-undici";
import { defaultResource, detectResources, envDetector, resourceFromAttributes } from "@opentelemetry/resources";
import { AggregationType, MeterProvider, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import { BatchSpanProcessor, TracerProvider } from "@opentelemetry/sdk-trace";
import { ATTR_SERVICE_NAME, METRIC_HTTP_SERVER_REQUEST_DURATION } from "@opentelemetry/semantic-conventions";

const PROBES = new Set(["/healthz", "/readyz"]);

let shutdown: () => Promise<void> = async () => {};

const env = process.env;
if (env.OTEL_SDK_DISABLED !== "true" && env.OTEL_EXPORTER_OTLP_ENDPOINT) {
  const resource = defaultResource()
    .merge(resourceFromAttributes({ [ATTR_SERVICE_NAME]: "gateway" }))
    .merge(detectResources({ detectors: [envDetector] }));

  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  propagation.setGlobalPropagator(
    new CompositePropagator({ propagators: [new W3CTraceContextPropagator(), new W3CBaggagePropagator()] }),
  );

  const tracerProvider = new TracerProvider({
    resource,
    spanProcessors: [new BatchSpanProcessor({ exporter: new OTLPTraceExporter() })],
  });
  trace.setGlobalTracerProvider(tracerProvider);

  const meterProvider = new MeterProvider({
    resource,
    readers: [
      new PeriodicExportingMetricReader({
        exporter: new OTLPMetricExporter(),
        exportIntervalMillis: Number(env.OTEL_METRIC_EXPORT_INTERVAL ?? 15000),
      }),
    ],
    views: [
      // The http instrumentation would record the same metric without a route
      // and with probes included. app.ts records the one the SLOs use.
      {
        instrumentName: METRIC_HTTP_SERVER_REQUEST_DURATION,
        meterName: "@opentelemetry/instrumentation-http",
        aggregation: { type: AggregationType.DROP },
      },
    ],
  });
  metrics.setGlobalMeterProvider(meterProvider);

  registerInstrumentations({
    tracerProvider,
    meterProvider,
    instrumentations: [
      new HttpInstrumentation({
        ignoreIncomingRequestHook: (req) => PROBES.has((req.url ?? "").split("?")[0]),
        // Upstream calls use fetch (undici, below). The only http client is the
        // OTLP exporter itself, whose requests must not become spans.
        disableOutgoingRequestInstrumentation: true,
      }),
      new UndiciInstrumentation(),
    ],
  });

  shutdown = async () => {
    await Promise.allSettled([tracerProvider.shutdown(), meterProvider.shutdown()]);
  };
}

// Flushes spans and metrics that are still buffered. Safe to call when disabled.
export function shutdownTelemetry(): Promise<void> {
  return shutdown();
}
