import { afterEach, describe, expect, it, vi } from "vitest";
import type { FastifyInstance } from "fastify";
import { buildApp, type FetchFn } from "../src/app.js";
import { loadConfig } from "../src/config.js";

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
