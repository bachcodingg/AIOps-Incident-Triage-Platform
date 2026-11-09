# F12 Slow database query

Root cause: PostgreSQL, bad release. First action: **rollback**.

![Catalog latency after the index was dropped](img/F12.png)

## Symptoms

- Product lookups get slow right after a catalog release, and stay slow:
  catalog p95 from a few milliseconds to hundreds, then seconds as PostgreSQL's
  CPU saturates and queries queue. Gateway lookups time out (504).
- The time is in one SQL statement: the rating aggregate,
  `SELECT count(*), avg(stars) FROM review WHERE product_id = ?`.
- PostgreSQL works hard: CPU at its limit, many rows read per query.

## Checks

1. A slow lookup trace: the long span is the SQL span under the catalog's
   server span.
2. The plan: `EXPLAIN ANALYZE SELECT count(*), avg(stars) FROM review WHERE product_id = 1;`
   A `Seq Scan on review` over two million rows instead of an index-only scan.
3. The index: `\d review` or `SELECT indexname FROM pg_indexes WHERE tablename = 'review';`.
   Is `idx_review_product` there?
4. Did a catalog release run a schema change just before? Releases run their
   migrations when they start.

Rule out: the network between catalog and PostgreSQL (F07) slows every query
by the same amount, and PostgreSQL stays idle; a connection pool problem (F11)
shows waits before the query, not in it.

## Fix

Roll back the catalog release. Every normal catalog release recreates
`idx_review_product` when it starts (`CREATE INDEX IF NOT EXISTS`), because a
schema change cannot be undone by deploying the old code alone: the old code
also has to put the index back. Creating it takes a few seconds on two million rows.

## Prevention

- Review migrations that drop or change indexes, with `EXPLAIN` on the queries
  that use them.
- A canary that also watches the database: CPU, rows read per query.
