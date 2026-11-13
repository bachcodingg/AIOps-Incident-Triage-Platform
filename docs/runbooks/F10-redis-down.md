# F10 Redis down

Root cause: Redis, dependency. First action: **restart** Redis, then the indexer.

![Redis not ready](img/F10.png)

## Symptoms

- Redis is not ready (*Platform*, *Redis ready* at 0).
- search-api fails its readiness check (it needs Redis) and leaves the Service:
  searches fail through the gateway with 502 or 503. Product lookups keep
  working, because the catalog reads from PostgreSQL.
- The indexer logs Redis errors and stops applying catalog changes.
- After Redis is back, searches succeed but **find nothing**: Redis keeps no data
  on disk, so it comes back empty, and the search index is gone until the
  indexer's next full reindex.

## Checks

1. `kubectl -n shopsearch get pods -l app.kubernetes.io/name=redis` and its events.
2. search-api's logs: "redis error during search", and its `/readyz` answering
   `redis_unavailable`.
3. Only search is broken; `GET /api/products/1` still works. That separates it
   from DNS (F09) or a node loss (F14), which break more.
4. Once Redis is up: does `GET /api/search?q=laptop` return results? If it
   returns none, the index is empty.

## Fix

1. Restart Redis if it does not come back by itself.
2. Restart the indexer (`kubectl argo rollouts restart indexer -n shopsearch`):
   it rebuilds the index from the catalog when it starts.
3. Check that search returns results again.

## Prevention

- Rebuild the index automatically when it is found empty, instead of on the
  indexer's start only.
- Redis with persistence or a replica, if the index rebuild becomes slow.
- An alert on Redis readiness, which fires before search-api's error rate.
