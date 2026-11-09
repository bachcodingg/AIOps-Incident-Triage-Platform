# F03 Slow release

Root cause: the service, bad release. First action: **rollback**.

![p95 latency after the release](img/F03.png)

## Symptoms

- One service's p95 latency steps up by a fixed amount (150 ms, 250 ms,
  500 ms) at a release, and stays there at every traffic level.
  *Service RED*, panel *Latency*.
- At +250 ms and above the latency SLO (95 % under 300 ms) starts to burn; at
  +150 ms it may not, and only the step gives it away.
- No errors, unless callers time out (the gateway gives up after 2 s).

## Checks

1. Which service got slower? Its own server-side duration, not only the
   gateway's: the gateway's latency includes everything below it.
   `histogram_quantile(0.95, sum by (job, le) (rate(http_server_request_duration_seconds_bucket[5m])))`
2. Is the extra time spent in the service itself? Open a slow trace (exemplar or
   *Slow requests* table): the service's span is long, and its child spans to
   Redis or PostgreSQL are not.
3. Did it start with a release, on the new pods only?
4. A constant offset that does not grow with load means code, not saturation.
   Check that CPU is not throttled (*Platform*, *CPU throttling*), which would
   point to F05 instead.

Rule out: slow child spans (database, Redis) point to F12 or the network (F07);
latency that rises with traffic points to F05 or F06.

## Fix

Roll back the release.

## Prevention

The canary analysis fails a release whose p95 is 100 ms worse than the stable
one's, even when it is still under the SLO. Keep that relative check: an SLO
threshold alone lets +150 ms through.
