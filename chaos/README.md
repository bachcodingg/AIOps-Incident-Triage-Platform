# chaos/

Breaks ShopSearch on purpose, and writes down exactly what was broken, where
the AIOps layer cannot read it.

| Path | What |
|---|---|
| [`catalog.yaml`](catalog.yaml) | the 14 faults (F01–F14): how each is injected, its targets and intensities, the root-cause label, the correct action, and the PromQL symptom that proves it happened |
| [`experiments/`](experiments/) | Chaos Mesh manifests (F05, F07–F10) and the k6 spike (F06), as templates |
| [`runner/`](runner/) | `chaos-runner`: injects faults, on its own or on a random schedule, and seals the ground truth |
| [`../docs/runbooks/`](../docs/runbooks/) | one runbook per fault, for humans and the triage agent |

```bash
make chaos-list                 # the catalog
make chaos-test FAULT=F05       # dry run: inject once, prove the symptom, wait for recovery
make chaos-run                  # dataset run: every fault 5 times at random, ~70 h (week 6)
make chaos-cleanup              # undo what an interrupted run left behind
make chaos-unit                 # unit tests and catalog validation, no cluster
```

## How each fault is injected

| Injector | Faults | How |
|---|---|---|
| `config-release` | F01 | A typo in one environment variable, set on the service's ArgoCD Application. ArgoCD rolls it out like any config change. |
| `image-release` | F02 F03 F04 F11 F12 | A new image tag: the service's normal image with one `FAULT_*` variable baked in (`FROM incidentlens/<svc>:dev` plus `ENV`), loaded into kind and deployed through ArgoCD. |
| `chaos-mesh` | F05 F07 F08 F09 F10 | A Chaos Mesh experiment in the `chaos` namespace: StressChaos, NetworkChaos (delay, loss), PodChaos (pod-failure). |
| `load` | F06 | k6 pods in the `chaos` namespace, each under the gateway's per-client rate limit, adding 2 to 7 times the current traffic. |
| `sql-fill` | F13 | A table in the catalog database that grows by 10 or 25 MB a minute. |
| `node-stop` | F14 | `docker stop` on the kind worker node, `docker start` at the end. |

Decisions worth knowing:

- **Bad releases look like releases.** The fault flags (`FAULT_ERROR_RATE`,
  `FAULT_LATENCY_MS`, `FAULT_LEAK_KB_*`, `FAULT_CONNECTION_LEAK_RATE`,
  `FAULT_DROP_REVIEW_INDEX`) are baked into the image, so the release history
  shows a tag like `3f9c2a1`, never the flag. They fail the way a real bug does:
  an unhandled exception with a stack trace, a slow path, memory never freed, a
  connection never closed. No response or log line says "injected".
- **Bad releases skip the canary.** They are promoted fully, as a hotfix would
  be. Week 3's canary already catches them (`make canary-test`); the dataset
  needs releases that got past it. Benign releases take the same path, 15 per
  run, so "something was just deployed" is not a tell on its own.
- **F09 is CoreDNS down, not DNSChaos.** DNSChaos edits `/etc/resolv.conf`
  inside the target containers, and containerd mounts that file read-only when
  the root filesystem is read-only, as it is in every ShopSearch pod.
- **F07 and F08 shape the destination's replies.** kube-proxy rewrites a
  Service's ClusterIP outside the calling pod, so a filter inside the caller
  never sees a destination pod's IP. The replies carry the caller's real IP, so
  the destination's traffic to the caller is delayed or dropped instead: the
  round trips on that one link suffer, nothing else does.
- **F11 is a connection leak, not a small pool.** With a pool of 2 and the
  load here (a few lookups a second, each a few milliseconds), nothing would
  happen. A release that never returns some of its connections exhausts the
  pool of 10 within a minute or two, which is how this incident usually happens.
- **F12 needed a big table.** 40 products are too few for a missing index to
  matter, so product lookups now show a rating aggregated from a `review` table
  of 2 million rows (PostgreSQL only; the unit tests run on H2 without it).
  Every normal catalog release recreates the index on start, because Flyway
  cannot undo a migration and a rollback has to put it back.
- **F13 is bounded on kind.** kind's volumes are host directories whose size is
  not enforced, so the fault is the growth an alert predicts from, not an actual
  full disk. On EBS the same growth would fill the volume.
- **F14 needs the worker node.** kind now has a worker that only the four
  stateless services tolerate (`incidentlens.dev/pool=workload:NoSchedule`), so
  a node loss takes part of the workload down but never Prometheus, Loki,
  PostgreSQL or Redis with it.
- **F10 leaves an empty index.** Redis keeps nothing on disk. After it comes
  back, searches succeed and find nothing until the indexer's next full
  reindex, a second-order effect the runbook covers. The runner restarts the
  indexer after F10 so the next incident starts from a working system.

## Sealed ground truth

Each label records the fault, target, intensity, start, end, recovery time,
root cause, correct action and the symptom query, plus what was created (object
names, image tag). They are written to `~/.incidentlens/labels/<run>/labels.jsonl`.
Sealed means:

1. **Outside the repository.** The AIOps layer, its prompts and its tools read
   the repository; `chaos-runner` refuses a labels directory inside it.
2. **Outside what the agent can query.** Chaos objects and the k6 pods live in
   the `chaos` namespace. The AIOps service account (week 6) gets no access to
   it, or to `chaos-mesh.org` resources, or to the runner's image builds.
3. **Tamper-evident.** Every line carries the SHA-256 of the line before it.
   When a run ends, `make chaos-run` writes the last hash (the head) to
   `eval/seals/<time>.json`, which is committed. `chaos-runner verify labels.jsonl --seal eval/seals/<time>.json`
   fails if a single label changed afterwards.
4. **Unpredictable.** The schedule comes from a random seed that is only in the
   sealed file. Fault order, targets, intensities, durations and gaps are drawn
   from it; targets and intensities are rotated so that 5 repeats cover all of
   them rather than whatever the dice picked.

What still leaks, and is handled in week 6: the canary analysis runs and
Rollout events of a bad release (also present for benign ones), and Kubernetes
events in `shopsearch` such as a pod's image changing to `pause` under F10.
Those are symptoms an on-call engineer would see too.

## Dry run

`make chaos-test FAULT=<id>` injects one fault at its strongest intensity, and
fails unless (1) its symptom query is false before, (2) true within 6 minutes,
and (3) the system recovers afterwards (all pods ready, a search returns
results, the symptom gone). With `SCREENSHOT_DIR` it saves a Grafana screenshot
45 seconds after the symptom appears. The [chaos workflow](../.github/workflows/chaos.yml)
runs it for all 14 faults, each on a fresh kind cluster, on pull requests that
touch the chaos layer or the fault flags.

## Not done yet

- A recorded dataset: `make chaos-run` for ~70 hours is week 6.
- Cloud injection. Image releases build on the local Docker host and load into
  kind, and F14 stops a kind node; on EKS a bad release would go through the
  real release pipeline and F14 would terminate an instance. Neither is
  implemented, because the dataset is recorded on kind.
