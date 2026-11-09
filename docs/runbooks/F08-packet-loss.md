# F08 Packet loss between two services

Root cause: the link between two components, network. First action: **escalate**.

![Latency spikes on one edge](img/F08.png)

## Symptoms

- One caller's latency becomes spiky: most calls are normal, some take 200 ms
  more or several times that. TCP retransmits what is lost, and the first retry
  waits at least 200 ms. p95 and p99 jump while the median barely moves.
- At higher loss, timeouts: the gateway answers 504 after 2 s.
- Nothing changed in either service.

## Checks

1. Which edge? As in F07, compare client-side and server-side durations per
   edge in the service graph. With loss, the client side has a long tail the
   server side does not.
2. The spread: `histogram_quantile(0.5, ...)` against `histogram_quantile(0.99, ...)`
   for the caller. Loss widens the gap; a slow callee moves both.
3. Node-level evidence, if the node exporter shows it:
   `rate(node_netstat_Tcp_RetransSegs[5m])` up on the nodes involved.

Rule out: a constant offset is latency (F07); errors with no slow requests are
a service answering 500 (F02).

## Fix

Escalate to whoever owns the network, with the edge, the time and examples.
Retries with backoff hide part of it from users meanwhile; make sure they do not
pile up.

## Prevention

Alerts on retransmissions per node and on p99/p50 per edge. Timeouts short
enough that a lost packet costs a retry, not a stuck request.
