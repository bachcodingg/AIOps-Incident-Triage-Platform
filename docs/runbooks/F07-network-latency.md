# F07 Network latency between two services

Root cause: the link between two components, network. First action: **escalate**.

![Latency on one edge](img/F07.png)

## Symptoms

- Latency rises for one caller, by about the same amount on every call to one
  dependency. Calls that make several round trips (a search makes three Redis
  calls; a product lookup two queries) get slower by a multiple of it.
- Nothing changed in either service: no release, normal CPU and memory.
- The service graph shows one edge slower than before, while the server side of
  that edge reports normal durations.

## Checks

1. Which edge? In the service graph (*Service RED*, *Service graph*, or Tempo),
   compare client-side and server-side durations of each edge:
   `histogram_quantile(0.95, sum by (client, server, le) (rate(traces_service_graph_request_client_seconds_bucket[5m])))`
   against the same with `_server_seconds_`. The gap between them is the network.
2. In a slow trace, the caller's client span is long and the callee's server
   span inside it is short.
3. Is it one pair of pods or every pod of each? Same nodes or across nodes?
   Everything on one node points to the node (F14) or its network.
4. No release, no throttling, no change in the callee's own latency.

Rule out: a slow callee (F03, F12) has a long server span too; packet loss (F08)
looks like jumps of 200 ms and more rather than a constant offset.

## Fix

Nothing in the services fixes the network. Escalate to whoever owns the network
or the nodes, with the edge, the start time and a trace. In the meantime, check
that timeouts and retries upstream are not making it worse.

## Prevention

- Timeouts per call that fit the latency budget, and retries only with backoff.
- Alerts on the client/server gap per edge, which tells the network apart from
  the service at once.
