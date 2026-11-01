export interface Config {
  port: number;
  searchApiUrl: string;
  catalogUrl: string;
  rateLimitPerMinute: number;
  upstreamTimeoutMs: number;
  logLevel: string;
}

function intFromEnv(value: string | undefined, fallback: number, name: string): number {
  if (value === undefined || value === "") return fallback;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer, got "${value}"`);
  }
  return parsed;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  return {
    port: intFromEnv(env.PORT, 8080, "PORT"),
    searchApiUrl: (env.SEARCH_API_URL ?? "http://search-api:8000").replace(/\/$/, ""),
    catalogUrl: (env.CATALOG_URL ?? "http://catalog:8080").replace(/\/$/, ""),
    rateLimitPerMinute: intFromEnv(env.RATE_LIMIT_PER_MINUTE, 600, "RATE_LIMIT_PER_MINUTE"),
    upstreamTimeoutMs: intFromEnv(env.UPSTREAM_TIMEOUT_MS, 2000, "UPSTREAM_TIMEOUT_MS"),
    logLevel: env.LOG_LEVEL ?? "info",
  };
}
