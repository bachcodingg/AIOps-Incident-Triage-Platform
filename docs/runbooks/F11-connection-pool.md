# F11 Database connection pool exhaustion

Root cause: the catalog, bad release. First action: **rollback**.

![Product lookups failing](img/F11.png)

## Symptoms

- Product lookups get slow and then fail: the gateway answers 504 after its 2 s
  timeout, the catalog itself 500 after 5 s. Searches are not affected.
- It starts some minutes after a catalog release and gets worse over time, not
  at once.
- Catalog pods turn unready (their readiness check needs a database connection),
  so the Service has fewer and then no endpoints, and lookups fail with 502.
- PostgreSQL itself is fine: normal CPU, normal query times, and few queries.

## Checks

1. Catalog logs: "Acquisition timeout" or "Unable to acquire JDBC Connection".
2. PostgreSQL's view: `SELECT state, count(*) FROM pg_stat_activity WHERE datname = 'catalog' GROUP BY state;`
   Connections held `idle` while the catalog waits for one means they were
   taken and never given back: a leak, not too few connections.
3. Traces of failing lookups: the time is spent before the first SQL span, or
   there is no SQL span at all.
4. Did it start with a catalog release? A leak builds up with traffic, so the
   first errors can come long after the release finished.

Rule out: a slow query (F12) shows long SQL spans and a busy PostgreSQL; a
database that is down fails at once and every connection, not gradually.

## Fix

Roll back the catalog release. Restarting the catalog frees the leaked
connections and buys minutes, but the leak starts again.

## Prevention

- Leak detection in the pool (Agroal's `leak-detection-interval`) and an alert
  on pool usage near its maximum.
- A canary long enough for a slow leak to exhaust the pool.
