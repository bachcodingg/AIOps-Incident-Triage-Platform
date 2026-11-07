# services/ — ShopSearch

The workload IncidentLens breaks and repairs. Four small services so failures
cascade and traces have more than one hop.

| Service | Stack | Port | Health | Metrics | API |
|---|---|---|---|---|---|
| `gateway` | TypeScript, Node 24, Fastify | 8080 | `/healthz`, `/readyz` | `/metrics` | `GET /api/search?q=&limit=`, `GET /api/products/{id}` |
| `search-api` | Python 3.12, FastAPI | 8000 | `/healthz`, `/readyz` (pings Redis) | `/metrics` | `GET /search?q=&limit=` |
| `catalog` | Java 21, Quarkus, Panache, Flyway | 8080 | `/q/health/live`, `/q/health/ready` (DB) | `/q/metrics` | `GET/POST /products`, `GET/PUT/DELETE /products/{id}` |
| `indexer` | Python 3.12 worker | 8081 | `/healthz` (heartbeat), `/readyz` (first reindex done) | `/metrics` | none |

Metrics feed canary analysis ([gitops/README.md](../gitops/README.md#canary-analysis)).
`FAULT_ERROR_RATE` on the gateway (0 to 1) answers that share of API requests with
a 500, which is fault F02 and how automatic rollback is tested.

## Contracts between services

Redis keys (owned by indexer, read by search-api):

| Key | Type | Meaning |
|---|---|---|
| `catalog:changes` | list | change events, `LPUSH` by catalog, `BRPOP` by indexer |
| `catalog:changes:dead` | list | events the indexer could not apply |
| `search:docs` | hash | product id → product JSON |
| `search:version` | int | bumped on every index write; part of the search cache key |
| `search:cache:*` | string | cached search responses, 60 s TTL |

Change event: `{"op": "upsert", "id": 7, "product": {...}}` or `{"op": "delete", "id": 7}`.

## Tests

Every Dockerfile has a `test` stage, so `make test` (and CI) needs only Docker.
To run natively:

```bash
cd services/gateway    && npm ci && npm run lint && npm test
cd services/search-api && pip install -r requirements-dev.txt && pytest
cd services/indexer    && pip install -r requirements-dev.txt && pytest
cd services/catalog    && ./mvnw test        # JDK 21+, tests use in-memory H2
```
