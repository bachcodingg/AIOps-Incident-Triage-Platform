# F09 DNS failures

Root cause: the cluster, DNS. First action: **escalate**.

![CoreDNS without a ready replica](img/F09.png)

## Symptoms

- CoreDNS has no ready replica (*Platform*, *CoreDNS available* at 0), or
  answers with errors.
- Errors build up over minutes, not at once: connections that are already open
  keep working; every new connection needs a name lookup and fails. Clients that
  open a connection per request fail first; pooled connections last until
  they are recycled.
- Errors in several services at once, with "could not resolve", "ENOTFOUND",
  "Name or service not known" or "UnknownHostException" in their logs.

## Checks

1. CoreDNS itself: `kubectl -n kube-system get pods -l k8s-app=kube-dns` and
   their events.
2. Is it every service, or one? DNS breaks all of them; one service failing to
   resolve one name is a config error (F01).
3. From inside a pod: a lookup of `redis.shopsearch.svc.cluster.local` fails
   or times out.
4. The gateway answers 502 (connection failed), not 500 (the upstream failed).

## Fix

DNS is the platform's, not a service's: escalate to whoever runs the cluster.
If CoreDNS pods are stuck, restarting them is their call. Services recover on
their own once lookups work again.

## Prevention

- CoreDNS with more than one replica, spread over nodes, and NodeLocal DNSCache
  so a short CoreDNS outage does not reach the pods.
- An alert on CoreDNS availability and its SERVFAIL rate.
