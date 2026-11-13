# F06 Traffic spike

Root cause: the gateway, load. First action: **scale out**.

![Request rate stepping up](img/F06.png)

## Symptoms

- The gateway's request rate jumps to several times what the time of day would
  predict. *Platform*, *Requests per second*; *Service RED*, *Requests per second*.
- Latency and CPU rise on the gateway and on the services it calls; with a large
  spike, 5xx from saturated upstreams and 429s from the gateway's rate limit.

## Checks

1. Compare with the usual rate at this hour: the daily curve peaks at 20:00 and
   is lowest around 04:00. A spike is a step, not the evening ramp.
   `sum(rate(http_server_request_duration_seconds_count{job="shopsearch/gateway"}[5m]))`
   against the same with `offset 1d`.
2. Is the traffic real? Many clients with the usual mix of searches and product
   lookups is a flash crowd; one client, one URL, or a share of 429s that keeps
   growing is a scraper or an attack, which is not this runbook.
3. Which upstream saturates first? CPU throttling and latency per service
   (*Platform*).

## Fix

Scale out the gateway and whatever saturates behind it. The HPA does this on its
own if the spike lasts; raise the minimum replicas if it does not react fast
enough.

## Prevention

- Capacity for a few times the daily peak, and HPA limits that allow it.
- Per-client rate limits (the gateway allows 600 a minute per client) so one
  client cannot be the spike.
