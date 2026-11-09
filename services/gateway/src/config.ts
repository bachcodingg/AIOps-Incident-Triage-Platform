export interface Config {
  port: number;
  searchApiUrl: string;
  catalogUrl: string;
  rateLimitPerMinute: number;
  upstreamTimeoutMs: number;
  logLevel: string;
  faultErrorRate: number;
  faultLatencyMs: number;
}

function intFromEnv(value: string | undefined, fallback: number, name: string): number {
  if (value === undefined || value === "") return fallback;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer, got "${value}"`);
  }
  return parsed;
}

function nonNegativeIntFromEnv(value: string | undefined, name: string): number {
  if (value === undefined || value === "") return 0;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new Error(`${name} must be a whole number of 0 or more, got "${value}"`);
  }
  return parsed;
}

// Chaos flags. A bad release (chaos/catalog.yaml) is the normal image with one
// of these baked in, so the release history shows only a new image tag.
//   FAULT_ERROR_RATE   F02: share of API requests that fail with a 500
//   FAULT_LATENCY_MS   F03: extra time spent on every API request
function rateFromEnv(value: string | undefined, name: string): number {
  if (value === undefined || value === "") return 0;
  const parsed = Number.parseFloat(value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    throw new Error(`${name} must be between 0 and 1, got "${value}"`);
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
    faultErrorRate: rateFromEnv(env.FAULT_ERROR_RATE, "FAULT_ERROR_RATE"),
    faultLatencyMs: nonNegativeIntFromEnv(env.FAULT_LATENCY_MS, "FAULT_LATENCY_MS"),
  };
}
