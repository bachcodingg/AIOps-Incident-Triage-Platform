# services/ — ShopSearch

The workload IncidentLens breaks and repairs. Four small services so failures
cascade and traces have more than one hop.

| Service | Stack | Port | Health | API |
|---|---|---|---|---|
| `gateway` | TypeScript, Node 24, Fastify | 8080 | `/healthz`, `/readyz` | `GET /api/search?q=&limit=`, `GET /api/products/{id}` |
| `search-api` | Python 3.12, FastAPI | 8000 | `/healthz`, `/readyz` (pings Redis) | `GET /search?q=&limit=` |
| `catalog` | Java 21, Quarkus, Panache, Flyway | 8080 | `/q/health/live`, `/q/health/ready` (DB) | `GET/POST /products`, `GET/PUT/DELETE /products/{id}` |
| `indexer` | Python 3.12 worker | 8081 | `/healthz` (heartbeat), `/readyz` (first reindex done) | none |

`FAULT_ERROR_RATE` on the gateway (0 to 1) answers that share of API requests with
a 500, which is fault F02 and how automatic rollback is tested.

## Telemetry

Every service uses the OpenTelemetry SDK of its language and sends traces and
metrics over OTLP/HTTP to the collector on its node
([gitops/README.md](../gitops/README.md#observability)). Logs are JSON lines on
stdout with `trace_id` and `span_id`, which the collector reads from the node.
Nothing is exported unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so tests and
local runs need no collector.

| Service | Traces | Request metric |
|---|---|---|
| `gateway` | http server span named after the route, `fetch` client spans (undici) | recorded in `src/metrics.ts` |
| `search-api` | FastAPI's built-in telemetry, one client span per Redis command | FastAPI's own, with SLO buckets set by a View |
| `catalog` | Quarkus: server span, one span per SQL statement (JDBC) | `HttpMetricsFilter` |
| `indexer` | one consumer span per change event, child of the catalog request; full reindex | `indexer.changes{outcome}`, `indexer.ready` |

The HTTP services all record `http.server.request.duration` with the same
attributes (`http.request.method`, `http.route`, `http.response.status_code`)
and buckets, one of them at 0.3 s for the latency SLO. In Prometheus that is
`http_server_request_duration_seconds_*{job="shopsearch/<service>"}`, which the
SLOs, dashboards and canary analysis query.

## Contracts between services

Redis keys (owned by indexer, read by search-api):

| Key | Type | Meaning |
|---|---|---|
| `catalog:changes` | list | change events, `LPUSH` by catalog, `BRPOP` by indexer |
| `catalog:changes:dead` | list | events the indexer could not apply |
| `search:docs` | hash | product id → product JSON |
| `search:version` | int | bumped on every index write; part of the search cache key |
| `search:cache:*` | string | cached search responses, 60 s TTL |

Change event: `{"op": "upsert", "id": 7, "product": {...}}` or `{"op": "delete", "id": 7}`,
plus `traceparent` (W3C trace context) when the change was made inside a traced
request, so the indexer applies it in the same trace.

## Tests

Every Dockerfile has a `test` stage, so `make test` (and CI) needs only Docker.
To run natively:

```bash
cd services/gateway    && npm ci && npm run lint && npm test
cd services/search-api && pip install -r requirements-dev.txt && pytest
cd services/indexer    && pip install -r requirements-dev.txt && pytest
cd services/catalog    && ./mvnw test        # JDK 21+, tests use in-memory H2
```
