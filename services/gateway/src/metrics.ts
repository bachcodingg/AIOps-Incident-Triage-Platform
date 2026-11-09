import { isSpanContextValid, metrics, trace, type Histogram, type Meter, type Span } from "@opentelemetry/api";
import {
  ATTR_HTTP_REQUEST_METHOD,
  ATTR_HTTP_RESPONSE_STATUS_CODE,
  ATTR_HTTP_ROUTE,
  METRIC_HTTP_SERVER_REQUEST_DURATION,
} from "@opentelemetry/semantic-conventions";

// 0.3 s must be a bucket boundary: the latency SLO counts requests under 300 ms.
// The semantic-convention defaults jump from 0.25 to 0.5.
export const DURATION_BUCKETS_SECONDS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5];

// Request duration as the OpenTelemetry HTTP semantic conventions define it. In
// Prometheus it becomes http_server_request_duration_seconds_{bucket,count,sum},
// the series the SLOs and the canary analysis query.
export class Metrics {
  private readonly duration: Histogram;

  constructor(meter: Meter = metrics.getMeter("gateway")) {
    this.duration = meter.createHistogram(METRIC_HTTP_SERVER_REQUEST_DURATION, {
      description: "Duration of API requests served by the gateway.",
      unit: "s",
      advice: { explicitBucketBoundaries: DURATION_BUCKETS_SECONDS },
    });
  }

  observe(method: string, route: string, status: number, seconds: number): void {
    this.duration.record(seconds, {
      [ATTR_HTTP_REQUEST_METHOD]: method,
      [ATTR_HTTP_ROUTE]: route,
      [ATTR_HTTP_RESPONSE_STATUS_CODE]: status,
    });
  }
}

// Fields every log line carries, so Grafana can jump from a log line to its trace.
export function traceFields(span: Span | undefined = trace.getActiveSpan()): Record<string, string> {
  const ctx = span?.spanContext();
  if (!ctx || !isSpanContextValid(ctx)) return {};
  return { trace_id: ctx.traceId, span_id: ctx.spanId };
}
