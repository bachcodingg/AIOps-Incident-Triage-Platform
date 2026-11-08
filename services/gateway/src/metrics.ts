import { Histogram, Registry } from "prom-client";

// Same metric and label names the OpenTelemetry semantic conventions produce,
// so canary analysis keeps working when week 4 swaps this for the OTel SDK.
export class Metrics {
  private readonly registry = new Registry();
  private readonly duration = new Histogram({
    name: "http_server_request_duration_seconds",
    help: "Duration of API requests served by the gateway.",
    labelNames: ["http_request_method", "http_route", "http_response_status_code"] as const,
    buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5],
    registers: [this.registry],
  });

  get contentType(): string {
    return this.registry.contentType;
  }

  observe(method: string, route: string, status: number, seconds: number): void {
    this.duration.observe(
      { http_request_method: method, http_route: route, http_response_status_code: String(status) },
      seconds,
    );
  }

  render(): Promise<string> {
    return this.registry.metrics();
  }
}
