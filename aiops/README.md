# aiops/

The AIOps loop. Started after gate G1 (platform observable, end of week 4).

| Path | Week | What |
|---|---|---|
| [`recorder/`](recorder/) | 6 | exports what the AIOps layer can see, in 5-minute chunks, read as the `aiops` service account |
| [`detector/`](detector/) | 6 | reads back the static (B0) and burn-rate (B1) alerts, and adds anomaly detection: rolling median/MAD, Isolation Forest, missing telemetry |
| [`correlator/`](correlator/) | 6 | groups alerts into incidents by time and the service graph |
| [`triage/`](triage/) | 8 | LLM agent with read-only, typed tools, replay mode and strict JSON verdicts; the rule baseline (B2) in the same format |
| `remediator/` | 9 | `rollback`, `restart`, `scale_out`, `none_escalate`, behind policy and approval |

Design and reasoning: [docs/design.md](../docs/design.md) sections 6, 11 and 12.

```bash
make aiops-unit                                  # unit tests, no cluster
make record NAME=baseline HOURS=72               # fault-free traffic, read as the aiops account
make detector-train RECORDING=baseline           # Isolation Forests -> ~/.incidentlens/models/baseline.pkl
make dataset-run                                 # chaos-run with the recorder alongside, ~70 h
make detect RECORDING=<name> MODEL=baseline      # alerts.jsonl and incidents-{B0,B1,B2}.jsonl in the recording
make detect-test                                 # on a running cluster: rules, RBAC, one recorded chunk (CI)
make triage-one RECORDING=<name> INCIDENT=inc-0007 VARIANT=B2   # replay one incident (B2 free, A1 calls Claude)
make triage RUN=<run> RECORDING=<name> VARIANTS="B2 A1" LIMIT=3  # triage and score the tuning set
```

Recordings and models live in `~/.incidentlens/`, never in this repository.

## Recorder

`python -m recorder record --name NAME` records live from now on, one chunk
every 5 minutes once the chunk is 90 seconds old (OTLP batches and 1-minute
rate windows need that long to settle). Stopped, the same command resumes; with
`--from now-2h` it catches up on the past first. Each chunk is gzipped JSON:

| Section | What | From |
|---|---|---|
| `metrics` | the fixed list in [`queries.yaml`](recorder/queries.yaml) every 15 s: RED per service and per gateway route, CPU, memory, restarts, readiness, nodes, CoreDNS, indexer, database size and connections, Tempo's service graph | Prometheus |
| `alerts` | `ALERTS` for the static and burn-rate rules | Prometheus |
| `events` | Kubernetes events in shopsearch and kube-system | API server |
| `deploys` | ArgoCD history entries, application health, rollout state | API server |
| `logs` | log volume per service and level; warning and error lines (300 at most) | Loki |
| `traces` | summaries of failed and slow requests; 3 condensed full traces | Tempo |

A section that fails is stored as `null` with its error, and the others go on.
Kubernetes keeps events for about an hour, so a chunk recorded later than that
says `events_complete: false`. `python -m recorder info NAME` lists gaps and
failed sections.

Two rules keep the recording from leaking the ground truth: it covers the
whole timeline in fixed chunks, so no chunk boundary marks a fault, and it reads
as the `aiops` service account ([gitops/aiops](../gitops/aiops/)), which has no
access to the chaos namespace, with queries scoped to the workload.

## Detector

| Detector | Alert | Fires when |
|---|---|---|
| static (a) | `Static*` | a Prometheus rule in [static-rules.yaml](../gitops/observability/files/static-rules.yaml) fires |
| burn-rate (b) | `*BudgetBurn` | a Sloth alert fires (page or ticket) |
| anomaly (c) | `AnomalyMAD` | a service's request rate (up or down), error ratio or p95 is more than 6 robust z-scores from the median of the last hour, for a minute |
| anomaly (c) | `AnomalyIForest` | a component's Isolation Forest score passes the 99.9th percentile of fault-free training, or a metric leaves the training envelope; the label names the feature furthest from normal |
| anomaly (c) | `AnomalyAbsent` | a component that reported for the last 30 minutes stops reporting for 2 |

All anomaly alerts fire like Prometheus alerts do: above the threshold for 60
seconds before firing, below it for 120 before resolving. Every number is in
[`detector/config.yaml`](detector/config.yaml), so tuning shows up in Git.

The Isolation Forests need a fault-free recording, 72 hours in the plan;
`detector train` refuses less than 24, because a model that never saw a night
flags every night. The model file is a pickle: load only ones you trained.

## Correlator

Alerts are taken in the order they started. One joins an open incident when its
component is the same as, or a neighbour of, one of the incident's in the
service graph; `node` and `cluster` are everyone's neighbour. An alert related
to two open incidents merges them. An incident takes new alerts while one of its
alerts started less than 5 minutes ago, or is still firing and started less
than 30 minutes ago.

The graph is [`correlator/graph.yaml`](correlator/graph.yaml) joined with the
client/server pairs Tempo derived from traces during the recording.

`python -m correlator --recording NAME --variant B2` uses the detectors of a
variant (B0 static, B1 burn-rate, B2 and A1 burn-rate plus anomaly) and prints
the alert noise before and after.

## Triage

The agent ([`triage/`](triage/)) triages one incident at a time, in **replay
mode**: it runs against the recording as it looked two minutes after the
incident opened, so a run is repeatable, costs nothing but the model, and needs
no cluster. [`replay.py`](triage/replay.py) is the one place telemetry passes
through on the way to the agent, and it drops everything recorded after that
moment: later samples, events, logs and releases, the rollout status the
recorder took after the chunk ended, and the end of alerts that were still
firing.

| Tool | Reads |
|---|---|
| `query_metric` | one recorded metric ([`queries.yaml`](recorder/queries.yaml)), per series, against the median of the hour before |
| `list_alerts` | static, burn-rate and anomaly alerts in a window |
| `search_logs` | warning and error lines, and log volume per service and level |
| `get_traces` | failed and slow requests, and the spans of one trace |
| `k8s_events` | events in shopsearch and kube-system |
| `deploy_history` | ArgoCD releases with their Helm parameters, rollout status |
| `service_graph` | the call graph, and per edge the traffic and failures traces saw |
| `read_runbook` | one runbook from [docs/runbooks](../docs/runbooks) |

Each tool is a Pydantic model of its arguments and a function over the replay
view, with a 10-second timeout and a 6,000-character cap. A bad call comes back
as an error the model can read. There is no shell and no free-form query:
`query_metric` only knows the recorded queries, which are scoped to the workload.

The loop ([`agent.py`](triage/agent.py)) stops at 12 turns, 20 tool calls,
400k input tokens, 40k output tokens, $0.50 or 5 minutes, whichever comes
first, and then asks for the answer with tools off. The answer must validate
against [`verdict.py`](triage/verdict.py): up to 3 hypotheses (component and
category), evidence, and one action from `rollback`, `restart`, `scale_out`,
`none_escalate`, `none_verify`, with a target the action can apply to. An
invalid answer gets one retry with the validation errors; after that, a
refusal or a provider error, the verdict is `none_escalate`. Every run is
recorded: system prompt, brief, each model call (tokens, cost, which model
answered), each tool call with its output, the raw answer and the verdict.

| Variant | Model | Differs from A1 |
|---|---|---|
| B2 | none: [`rule_rca.py`](triage/rule_rca.py) | blames the deepest component whose errors, latency or health changed first; a fixed table gives category and action |
| A1 | `claude-opus-5-5`, effort medium | |
| A2 | same | no `get_traces`, and no trace-derived metrics or edge rates |
| A3 | same | no runbook index in the prompt, no `read_runbook` |
| A4 | `qwen3:8b` on a local Ollama | a small open-weight model |

Models, prices, limits and variants are in [`triage/config.yaml`](triage/config.yaml).
Claude needs `ANTHROPIC_API_KEY` or an `ant auth login` profile. Requests go
through the API's server-side refusal fallback, and the run log names the model
that answered each turn. `python -m triage prompt` prints the system prompt and
the tool definitions without calling anything.
