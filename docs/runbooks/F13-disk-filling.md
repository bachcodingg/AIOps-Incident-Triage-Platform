# F13 Disk filling on the database volume

Root cause: PostgreSQL, resource. First action: **escalate**.

![Database size growing](img/F13.png)

## Symptoms

- The catalog database grows by megabytes a minute when it normally barely
  changes. *Platform*, *Catalog database size* and *Catalog database growth*.
- No user-facing symptom yet. The danger is ahead: when the volume is full,
  PostgreSQL stops accepting writes, then may stop altogether.

## Checks

1. How fast, and how long until full? With the volume's size from
   `kubectl -n shopsearch get pvc`:
   `predict_linear(sum(cnpg_pg_database_size_bytes{datname="catalog"})[30m:1m], 6 * 3600)`
   against that size.
2. What is growing? The largest tables:
   `SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 5;`
3. Who writes to it? `pg_stat_user_tables.n_tup_ins` for that table, and
   `pg_stat_activity` for the sessions inserting.
4. Not WAL: `cnpg_collector_pg_wal` stays flat, so it is data, not replication
   or archiving falling behind.

## Fix

Escalate: the fix is a human decision. Stop the writer, or grow the volume
(CloudNativePG can resize it if the storage class allows), or delete data that
is safe to lose. Nothing automatic should delete data.

## Prevention

- An alert on predicted time to full, not on a fixed percentage: 80 % full and
  flat is fine, 40 % full and doubling every hour is not.
- Retention for every table that only grows, such as audit logs.

On kind the volume is a host directory whose size Kubernetes does not enforce,
so in the lab the database never actually runs out of space; the growth and
its prediction are the incident.
