# Runbooks

One runbook per fault type in [chaos/catalog.yaml](../../chaos/catalog.yaml), in
the same four parts each: **symptoms** (what you see first), **checks** (how to
confirm it and rule out the look-alikes), **fix**, **prevention**.

The triage agent reads these (week 8, tool `read_runbook`), and one ablation
runs it without them. So they describe the system the way an on-call engineer
sees it, and say nothing about how the lab injects faults: that is in
[chaos/README.md](../../chaos/README.md), which the agent does not get.

| ID | Runbook | Root cause | First action |
|---|---|---|---|
| F01 | [Crash loop after a config change](F01-crash-loop.md) | service, bad release | rollback |
| F02 | [Release that returns 500s](F02-error-release.md) | service, bad release | rollback |
| F03 | [Slow release](F03-slow-release.md) | service, bad release | rollback |
| F04 | [Memory leak](F04-memory-leak.md) | service, resource | restart, then rollback |
| F05 | [CPU saturation](F05-cpu-saturation.md) | service, resource | scale out |
| F06 | [Traffic spike](F06-traffic-spike.md) | gateway, load | scale out |
| F07 | [Network latency between two services](F07-network-latency.md) | link, network | escalate |
| F08 | [Packet loss between two services](F08-packet-loss.md) | link, network | escalate |
| F09 | [DNS failures](F09-dns-failures.md) | cluster, DNS | escalate |
| F10 | [Redis down](F10-redis-down.md) | Redis, dependency | restart |
| F11 | [Database connection pool exhaustion](F11-connection-pool.md) | catalog, bad release | rollback |
| F12 | [Slow database query](F12-slow-query.md) | PostgreSQL, bad release | rollback |
| F13 | [Disk filling on the database volume](F13-disk-filling.md) | PostgreSQL, resource | escalate |
| F14 | [Node loss](F14-node-loss.md) | node, infrastructure | verify recovery |

## Where to look

- **Grafana** (`make grafana`): *ShopSearch / Service RED* per service (rate,
  errors, latency, slow traces, logs, pods), *ShopSearch / SLOs*, and
  *ShopSearch / Platform* for nodes, DNS, Redis, PostgreSQL and per-container
  CPU and memory.
- **Traces**: Grafana Explore, Tempo. The service graph shows which edge is
  slow or failing; a trace shows where inside one request the time went.
- **Logs**: Grafana Explore, Loki, `{service_name="<service>"}`. Every line
  carries its trace id.
- **Releases**: `kubectl -n argocd get applications`, and per service
  `kubectl -n shopsearch get rollout <service>` (its revision, stable and canary
  pod hashes). What changed and when is in each Application's history.
- **Kubernetes**: `kubectl -n shopsearch get pods,events`.

## Actions

The remediator (week 9) can only do these, and so the agent picks one of them:

| Action | What it means here |
|---|---|
| `rollback` | Put the previous release back. In GitOps that is a revert in Git; `kubectl argo rollouts undo <service> -n shopsearch` does it at once, and the revert stops ArgoCD from applying the bad one again. |
| `restart` | Restart the pods of one component: `kubectl argo rollouts restart <service>`, or delete the pod of a plain Deployment. |
| `scale_out` | Add replicas: raise the HorizontalPodAutoscaler's minimum for the service. |
| `none_escalate` | Nothing automatic helps. Page a human with the evidence. |
| `none_verify` | The platform heals this on its own. Confirm that it did. |

The `kubectl argo rollouts` commands need the Argo Rollouts kubectl plugin.

## Symptoms

Each runbook shows a screenshot of its fault's symptom, taken by the dry run
(`make chaos-test FAULT=<id>`, and the [chaos workflow](../../.github/workflows/chaos.yml)
for all of them) at the moment Prometheus first saw it.
