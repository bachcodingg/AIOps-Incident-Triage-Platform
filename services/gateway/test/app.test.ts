import { afterEach, describe, expect, it, vi } from "vitest";
import type { FastifyInstance } from "fastify";
import { TraceFlags, trace } from "@opentelemetry/api";
import { MeterProvider, MetricReader } from "@opentelemetry/sdk-metrics";
import { buildApp, type FetchFn } from "../src/app.js";
import { loadConfig } from "../src/config.js";
import { DURATION_BUCKETS_SECONDS, traceFields } from "../src/metrics.js";

const config = loadConfig({
  SEARCH_API_URL: "http://search-api.test",
  CATALOG_URL: "http://catalog.test/",
  RATE_LIMIT_PER_MINUTE: "3",
  UPSTREAM_TIMEOUT_MS: "50",
});

let app: FastifyInstance | undefined;

async function makeApp(fetchFn: FetchFn) {
  app = await buildApp({ config, fetchFn, logger: false });
  return app;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

afterEach(async () => {
  await app?.close();
  app = undefined;
});

describe("config", () => {
  it("strips trailing slashes and applies defaults", () => {
    expect(config.catalogUrl).toBe("http://catalog.test");
    expect(config.port).toBe(8080);
  });

  it("rejects invalid numbers", () => {
    expect(() => loadConfig({ PORT: "abc" })).toThrow(/PORT/);
  });
});

describe("health", () => {
  it("answers liveness and readiness without calling upstreams", async () => {
    const fetchFn = vi.fn<FetchFn>();
    const a = await makeApp(fetchFn);
    expect((await a.inject("/healthz")).statusCode).toBe(200);
    expect((await a.inject("/readyz")).statusCode).toBe(200);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("is exempt from rate limiting", async () => {
    const a = await makeApp(vi.fn<FetchFn>());
    for (let i = 0; i < 10; i++) {
      expect((await a.inject("/healthz")).statusCode).toBe(200);
    }
  });
});

// Collects whatever the app recorded, on demand, instead of exporting it.
class TestReader extends MetricReader {
  protected async onForceFlush(): Promise<void> {}
  protected async onShutdown(): Promise<void> {}
}

async function makeMeteredApp(fetchFn: FetchFn) {
  const reader = new TestReader();
  const provider = new MeterProvider({ readers: [reader] });
  app = await buildApp({ config, fetchFn, logger: false, meter: provider.getMeter("test") });
  return { a: app, reader };
}

async function durationPoints(reader: TestReader) {
  const { resourceMetrics } = await reader.collect();
  const metric = resourceMetrics.scopeMetrics
    .flatMap((s) => s.metrics)
    .find((m) => m.descriptor.name === "http.server.request.duration");
  return (metric?.dataPoints ?? []) as { attributes: Record<string, unknown>; value: { count: number; buckets: { boundaries: number[] } } }[];
}

describe("metrics", () => {
  it("records API requests by route and status, not probes", async () => {
    const fetchFn = vi.fn<FetchFn>().mockImplementation(async () => jsonResponse({ results: [] }));
    const { a, reader } = await makeMeteredApp(fetchFn);
    await a.inject("/healthz");
    await a.inject("/api/search?q=tv");

    const points = await durationPoints(reader);

    expect(points).toHaveLength(1);
    expect(points[0].attributes).toEqual({
      "http.request.method": "GET",
      "http.route": "/api/search",
      "http.response.status_code": 200,
    });
    expect(points[0].value.count).toBe(1);
  });

  it("has a bucket boundary at the 300 ms latency objective", async () => {
    const { a, reader } = await makeMeteredApp(vi.fn<FetchFn>().mockResolvedValue(jsonResponse({})));
    await a.inject("/api/products/1");
    const [point] = await durationPoints(reader);
    expect(point.value.buckets.boundaries).toEqual(DURATION_BUCKETS_SECONDS);
    expect(DURATION_BUCKETS_SECONDS).toContain(0.3);
  });

  it("records rate limited requests", async () => {
    const fetchFn = vi.fn<FetchFn>().mockImplementation(async () => jsonResponse({ results: [] }));
    const { a, reader } = await makeMeteredApp(fetchFn);
    for (let i = 0; i < 4; i++) await a.inject("/api/search?q=tv");
    const codes = (await durationPoints(reader)).map((p) => p.attributes["http.response.status_code"]);
    expect(codes).toContain(429);
  });

  it("no longer serves a Prometheus endpoint: metrics are pushed over OTLP", async () => {
    const a = await makeApp(vi.fn<FetchFn>());
    expect((await a.inject("/metrics")).statusCode).toBe(404);
  });
});

describe("log correlation", () => {
  it("adds trace and span ids from the active span", () => {
    const span = trace.wrapSpanContext({
      traceId: "0af7651916cd43dd8448eb211c80319c",
      spanId: "b7ad6b7169203331",
      traceFlags: TraceFlags.SAMPLED,
    });
    expect(traceFields(span)).toEqual({ trace_id: "0af7651916cd43dd8448eb211c80319c", span_id: "b7ad6b7169203331" });
  });

  it("adds nothing outside a trace", () => {
    expect(traceFields(undefined)).toEqual({});
  });
});

describe("FAULT_ERROR_RATE", () => {
  it("is off by default", () => {
    expect(config.faultErrorRate).toBe(0);
  });

  it("rejects values outside 0..1", () => {
    expect(() => loadConfig({ FAULT_ERROR_RATE: "5" })).toThrow(/FAULT_ERROR_RATE/);
  });

  it("answers 500 for the configured share of API requests, never for probes", async () => {
    const fetchFn = vi.fn<FetchFn>().mockImplementation(async () => jsonResponse({ results: [] }));
    const draws = [0.04, 0.5];
    app = await buildApp({
      config: { ...config, faultErrorRate: 0.05 },
      fetchFn,
      logger: false,
      random: () => draws.shift() ?? 1,
    });

    expect((await app.inject("/healthz")).statusCode).toBe(200);
    const failed = await app.inject("/api/search?q=tv");
    expect(failed.statusCode).toBe(500);
    expect(failed.body).not.toMatch(/fault|inject/i); // looks like any other bug
    expect((await app.inject("/api/search?q=tv")).statusCode).toBe(200);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });
});

describe("FAULT_LATENCY_MS", () => {
  it("is off by default and rejects negative values", () => {
    expect(config.faultLatencyMs).toBe(0);
    expect(() => loadConfig({ FAULT_LATENCY_MS: "-5" })).toThrow(/FAULT_LATENCY_MS/);
    expect(() => loadConfig({ FAULT_LATENCY_MS: "2s" })).toThrow(/FAULT_LATENCY_MS/);
  });

  it("slows API requests down, but not probes", async () => {
    const fetchFn = vi.fn<FetchFn>().mockImplementation(async () => jsonResponse({ results: [] }));
    app = await buildApp({ config: { ...config, faultLatencyMs: 120 }, fetchFn, logger: false });

    let start = performance.now();
    expect((await app.inject("/healthz")).statusCode).toBe(200);
    expect(performance.now() - start).toBeLessThan(100);

    start = performance.now();
    expect((await app.inject("/api/search?q=tv")).statusCode).toBe(200);
    expect(performance.now() - start).toBeGreaterThanOrEqual(115);
  });
});

describe("GET /api/search", () => {
  it("forwards the query to search-api and relays the body", async () => {
    const fetchFn = vi.fn<FetchFn>().mockResolvedValue(jsonResponse({ total: 1, results: [{ id: 1 }] }));
    const a = await makeApp(fetchFn);

    const res = await a.inject("/api/search?q=gaming%20laptop&limit=5");

    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ total: 1, results: [{ id: 1 }] });
    expect(String(fetchFn.mock.calls[0][0])).toBe("http://search-api.test/search?q=gaming+laptop&limit=5");
  });

  it("rejects an empty query", async () => {
    const fetchFn = vi.fn<FetchFn>();
    const a = await makeApp(fetchFn);
    const res = await a.inject("/api/search?q=%20");
    expect(res.statusCode).toBe(400);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("maps a connection failure to 502", async () => {
    const a = await makeApp(vi.fn<FetchFn>().mockRejectedValue(new TypeError("fetch failed")));
    const res = await a.inject("/api/search?q=phone");
    expect(res.statusCode).toBe(502);
    expect(res.json()).toMatchObject({ error: "upstream_unavailable", upstream: "search-api" });
  });

  it("maps an upstream timeout to 504", async () => {
    const timeout = new DOMException("The operation timed out.", "TimeoutError");
    const a = await makeApp(vi.fn<FetchFn>().mockRejectedValue(timeout));
    const res = await a.inject("/api/search?q=phone");
    expect(res.statusCode).toBe(504);
  });

  it("maps a non-JSON upstream body to 502", async () => {
    const a = await makeApp(vi.fn<FetchFn>().mockResolvedValue(new Response("<html>oops</html>", { status: 200 })));
    const res = await a.inject("/api/search?q=phone");
    expect(res.statusCode).toBe(502);
    expect(res.json()).toMatchObject({ error: "upstream_bad_response" });
  });

  it("rate limits API routes", async () => {
    const fetchFn = vi.fn<FetchFn>().mockImplementation(async () => jsonResponse({ results: [] }));
    const a = await makeApp(fetchFn);
    const codes = [];
    for (let i = 0; i < 4; i++) {
      codes.push((await a.inject("/api/search?q=tv")).statusCode);
    }
    expect(codes).toEqual([200, 200, 200, 429]);
  });
});

describe("GET /api/products/:id", () => {
  it("relays the catalog status code", async () => {
    const fetchFn = vi.fn<FetchFn>().mockResolvedValue(jsonResponse({ error: "not_found" }, 404));
    const a = await makeApp(fetchFn);
    const res = await a.inject("/api/products/42");
    expect(res.statusCode).toBe(404);
    expect(String(fetchFn.mock.calls[0][0])).toBe("http://catalog.test/products/42");
  });

  it("rejects a non-numeric id", async () => {
    const fetchFn = vi.fn<FetchFn>();
    const a = await makeApp(fetchFn);
    expect((await a.inject("/api/products/abc")).statusCode).toBe(400);
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
