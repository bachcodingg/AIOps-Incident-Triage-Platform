# F02 Release that returns HTTP 500s

Root cause: the service, bad release. First action: **rollback**.

![Error ratio after the release](img/F02.png)

## Symptoms

- One service's 5xx ratio steps up from about 0 to a few percent, at the moment
  its new pods took traffic. *Service RED*, panel *Error ratio (5xx)*.
- The availability SLO burns: at 2 % errors against a 99.5 % objective the budget
  goes 4 times faster than allowed, at 10 % 20 times (*ShopSearch / SLOs*).
- Services upstream of it pass the errors on. The gateway's error ratio rises
  too, but its own logs only say the upstream answered 500.

## Checks

1. Where do the errors start? Compare error ratios per service: the lowest
   service in the call chain with errors is the origin.
   `sum by (job) (rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[5m])) / sum by (job) (rate(http_server_request_duration_seconds_count[5m]))`
2. Did it start with a release? The step lines up with the service's newest
   rollout (`kubectl -n shopsearch get rollout <service>`), and only pods with
   the new `rollouts-pod-template-hash` answer 500.
3. Logs of that service at error level: an unhandled exception with a stack
   trace, the same one over and over, is a code bug.
   `{service_name="<service>"} | detected_level="error"`
4. Traces: open an exemplar from the error panel. The failing span is in the
   service itself, not in a call it makes.

Rule out: errors spread over several services at once, or in calls to Redis or
PostgreSQL, point to a dependency (F10, F11) or the network (F08).

## Fix

Roll back the release. The error ratio falls back as soon as the old pods take
the traffic again.

## Prevention

The canary analysis (week 3) compares the new release's error ratio with the
old one's and aborts at +1 %. This fault is a release that got past it: a hotfix
promoted straight to 100 %, or an error rate that only shows under full traffic.
Do not skip the canary, and keep the comparison relative, not absolute.
