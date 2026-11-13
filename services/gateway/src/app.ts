import Fastify, { type FastifyInstance, type FastifyReply } from "fastify";
import rateLimit from "@fastify/rate-limit";
import { trace, type Meter } from "@opentelemetry/api";
import { ATTR_HTTP_ROUTE } from "@opentelemetry/semantic-conventions";
import type { Config } from "./config.js";
import { Metrics, traceFields } from "./metrics.js";

export type FetchFn = typeof fetch;

export interface AppOptions {
  config: Config;
  fetchFn?: FetchFn;
  logger?: boolean;
  random?: () => number;
  meter?: Meter;
}

// Calls an upstream service and relays its JSON body and status code.
// Timeouts become 504 and connection failures become 502, so the gateway's
// own error rate tells you which kind of upstream failure is happening.
async function proxyJson(
  reply: FastifyReply,
  fetchFn: FetchFn,
  url: string,
  timeoutMs: number,
  upstream: string,
): Promise<FastifyReply> {
  let res: Response;
  try {
    res = await fetchFn(url, {
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    const timedOut = err instanceof Error && err.name === "TimeoutError";
    reply.log.warn({ upstream, url, err }, "upstream request failed");
    return reply
      .code(timedOut ? 504 : 502)
      .send({ error: timedOut ? "upstream_timeout" : "upstream_unavailable", upstream });
  }

  const text = await res.text();
  let body: unknown;
  try {
    body = text === "" ? null : JSON.parse(text);
  } catch {
    reply.log.warn({ upstream, url, status: res.status }, "upstream returned non-JSON body");
    return reply.code(502).send({ error: "upstream_bad_response", upstream });
  }
  return reply.code(res.status).send(body);
}

export async function buildApp({
  config,
  fetchFn = fetch,
  logger = true,
  random = Math.random,
  meter,
}: AppOptions): Promise<FastifyInstance> {
  // JSON lines with an ISO time, a level name and the trace and span ids, so
  // the collector can parse them and Grafana can link a log line to its trace.
  const app = Fastify({
    logger: logger
      ? {
          level: config.logLevel,
          timestamp: () => `,"time":"${new Date().toISOString()}"`,
          formatters: { level: (label) => ({ level: label }) },
          mixin: () => traceFields(),
        }
      : false,
  });

  const metrics = new Metrics(meter);

  // The http instrumentation names server spans after the method only. Fastify
  // knows the route, so name the span "GET /api/search" and tag it.
  app.addHook("onRequest", async (req) => {
    const route = req.routeOptions.url;
    const span = trace.getActiveSpan();
    if (span && route) {
      span.setAttribute(ATTR_HTTP_ROUTE, route);
      span.updateName(`${req.method} ${route}`);
    }
  });

  // Fires for every response, including 429s from the rate limiter below.
  app.addHook("onResponse", async (req, reply) => {
    const route = req.routeOptions.url;
    if (route?.startsWith("/api/")) {
      metrics.observe(req.method, route, reply.statusCode, reply.elapsedTime / 1000);
    }
  });

  await app.register(rateLimit, {
    max: config.rateLimitPerMinute,
    timeWindow: "1 minute",
  });

  // Probes are exempt from rate limiting so a traffic spike cannot make
  // Kubernetes kill healthy pods.
  app.get("/healthz", { config: { rateLimit: false } }, async () => ({ status: "ok" }));
  app.get("/readyz", { config: { rateLimit: false } }, async () => ({ status: "ready" }));

  // Deliberately bad releases (chaos/catalog.yaml). They fail the way a real bug
  // does, a slow code path or an unhandled exception that Fastify logs with its
  // stack and answers with a plain 500, so nothing in a response or a log line
  // says the fault was injected.
  if (config.faultLatencyMs > 0) {
    app.addHook("preHandler", async (req) => {
      if (req.routeOptions.url?.startsWith("/api/")) {
        await new Promise((resolve) => setTimeout(resolve, config.faultLatencyMs));
      }
    });
  }
  if (config.faultErrorRate > 0) {
    app.addHook("preHandler", async (req) => {
      if (req.routeOptions.url?.startsWith("/api/") && random() < config.faultErrorRate) {
        throw new TypeError("Cannot read properties of undefined (reading 'locale')");
      }
    });
  }

  app.get<{ Querystring: { q?: string; limit?: string } }>("/api/search", async (req, reply) => {
    const q = (req.query.q ?? "").trim();
    if (q === "") {
      return reply.code(400).send({ error: "missing_query", message: "query parameter q is required" });
    }
    const params = new URLSearchParams({ q });
    if (req.query.limit) params.set("limit", req.query.limit);
    return proxyJson(reply, fetchFn, `${config.searchApiUrl}/search?${params}`, config.upstreamTimeoutMs, "search-api");
  });

  app.get<{ Params: { id: string } }>("/api/products/:id", async (req, reply) => {
    if (!/^\d+$/.test(req.params.id)) {
      return reply.code(400).send({ error: "invalid_id", message: "product id must be numeric" });
    }
    return proxyJson(reply, fetchFn, `${config.catalogUrl}/products/${req.params.id}`, config.upstreamTimeoutMs, "catalog");
  });

  return app;
}
