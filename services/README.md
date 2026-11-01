# services/ — ShopSearch

The workload IncidentLens breaks and repairs. Four small services so failures
cascade and traces have more than one hop.

| Service | Stack | Port | Health | API |
|---|---|---|---|---|
| `gateway` | TypeScript, Node 24, Fastify | 8080 | `/healthz`, `/readyz` | `GET /api/search?q=&limit=`, `GET /api/products/{id}` |
| `search-api` | Python 3.12, FastAPI | 8000 | `/healthz`, `/readyz` (pings Redis) | `GET /search?q=&limit=` |
| `catalog` | Java 21, Quarkus, Panache, Flyway | 8080 | `/q/health/live`, `/q/health/ready` (DB) | `GET/POST /products`, `GET/PUT/DELETE /products/{id}` |
| `indexer` | Python 3.12 worker | 8081 | `/healthz` (heartbeat), `/readyz` (first reindex done) | none |

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
