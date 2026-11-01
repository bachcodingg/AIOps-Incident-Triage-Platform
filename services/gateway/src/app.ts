import Fastify, { type FastifyInstance, type FastifyReply } from "fastify";
import rateLimit from "@fastify/rate-limit";
import type { Config } from "./config.js";

export type FetchFn = typeof fetch;

export interface AppOptions {
  config: Config;
  fetchFn?: FetchFn;
  logger?: boolean;
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

export async function buildApp({ config, fetchFn = fetch, logger = true }: AppOptions): Promise<FastifyInstance> {
  const app = Fastify({
    logger: logger ? { level: config.logLevel } : false,
  });

  await app.register(rateLimit, {
    max: config.rateLimitPerMinute,
    timeWindow: "1 minute",
  });

  // Probes are exempt from rate limiting so a traffic spike cannot make
  // Kubernetes kill healthy pods.
  app.get("/healthz", { config: { rateLimit: false } }, async () => ({ status: "ok" }));
  app.get("/readyz", { config: { rateLimit: false } }, async () => ({ status: "ready" }));

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
