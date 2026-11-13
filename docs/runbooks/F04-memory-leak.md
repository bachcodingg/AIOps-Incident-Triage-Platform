# F04 Memory leak

Root cause: the service, resource. First action: **restart**, then roll back.

![Memory climbing to the limit](img/F04.png)

## Symptoms

- One service's memory climbs in a straight line or a saw-tooth and never comes
  back down. *Platform*, panel *Memory, share of limit*; *Service RED*, *Memory per pod*.
- At the limit the pod is OOM-killed and restarts, then climbs again: a
  saw-tooth with a period of minutes to an hour.
- Around each kill, a burst of errors or a gap in the service's work (requests
  for search-api, change events for the indexer).

## Checks

1. Is it one service, and does the growth follow its traffic? A leak per
   request grows faster at peak; a leak per unit of time grows evenly.
   `max by (pod) (container_memory_working_set_bytes{namespace="shopsearch",container="<service>"})`
2. Was the last restart an OOM kill?
   `kubectl -n shopsearch get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}'` says `OOMKilled`.
3. When did the climb start? A release at that moment is the cause; the old
   release ran flat for days.

Rule out: memory that rises with load and falls with it is a cache or a larger
working set, not a leak. A crash loop right at start (F01) never gets to climb.

## Fix

Restart the service: it buys the time until the next limit hit, which is what
to do first at night. Then roll back the release that started the climb. A
restart alone is not a fix.

## Prevention

- An alert on memory over 80 % of the limit for 15 minutes, or on
  `predict_linear` of working set reaching the limit within 2 hours.
- A soak test in the canary: some leaks only show after half an hour, longer
  than any canary step here.
