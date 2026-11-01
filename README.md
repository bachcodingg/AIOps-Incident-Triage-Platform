# IncidentLens — AIOps incident-triage platform

A production-style Kubernetes platform, built from code, observed with
OpenTelemetry, broken on purpose with chaos experiments, and triaged by an LLM
agent that is measured against plain threshold alerts and a rule-based baseline.

[![ci](https://github.com/bachcodingg/AIOps-Incident-Triage-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/bachcodingg/AIOps-Incident-Triage-Platform/actions/workflows/ci.yml)

## Results

Not measured yet. The evaluation runs in week 10 on a locked test set of 40+
injected incidents; the plan and targets are fixed in advance in
[docs/design.md](docs/design.md#6-evaluation-plan).

| Variant | Detection recall | MTTD | RCA top-1 | Action accuracy | Cost / incident |
|---|---|---|---|---|---|
| B1 static thresholds | – | – | – | – | – |
| B2 burn-rate + rule RCA | – | – | – | – | – |
| A1 burn-rate + anomaly + LLM agent | – | – | – | – | – |

## Architecture

```mermaid
flowchart LR
  user([client]) -->|:8080| gw[gateway<br/>TS / Fastify]
  gw --> sa[search-api<br/>Python / FastAPI]
  gw --> cat[catalog<br/>Java / Quarkus]
  sa -->|index + cache| redis[(Redis)]
  cat --> pg[(PostgreSQL)]
  cat -->|catalog:changes| redis
  redis -->|BRPOP| idx[indexer<br/>Python worker]
  idx -->|search:docs| redis
  idx -.->|full reindex| cat
```

Five layers: workload (above), platform (Terraform, ArgoCD), observability
(OpenTelemetry, Prometheus, Loki, Tempo, Sloth SLOs), chaos (Chaos Mesh,
sealed ground truth) and the AIOps loop (detectors, correlator, triage agent,
gated remediator). Details: [docs/design.md](docs/design.md).

## Quickstart (kind, free)

Requirements: Docker (4 GB+ for the workload), `make`, `bash`, `curl`.
`kind` and `kubectl` are downloaded into `.bin/` if they are not on your PATH.

```bash
git clone https://github.com/bachcodingg/AIOps-Incident-Triage-Platform.git
cd AIOps-Incident-Triage-Platform
make up          # kind cluster + build + deploy + end-to-end smoke test
curl 'http://localhost:8080/api/search?q=gaming%20lapotp'
make down        # delete the cluster
```

`make up` is verified by the [`e2e` CI job](.github/workflows/ci.yml) on every
push, from a fresh clone. Other targets: `make help`.

On Windows, run it from Git Bash (`winget install ezwinports.make` provides
`make`).

## Repository layout

| Path | What |
|---|---|
| [`services/`](services/) | ShopSearch: gateway, search-api, catalog, indexer |
| [`infra/`](infra/) | kind config now; Terraform modules for OCI and AWS EKS in week 2 |
| [`gitops/`](gitops/) | plain manifests now; Helm charts + ArgoCD app-of-apps in week 3 |
| [`chaos/`](chaos/) | fault catalog (F01–F14); Chaos Mesh experiments and chaos-runner in week 5 |
| [`aiops/`](aiops/) | detectors, correlator, triage agent, remediator (weeks 6–9) |
| [`eval/`](eval/) | evaluation harness and results (weeks 6–10) |
| [`docs/`](docs/) | design doc; runbooks and postmortems later |

## Roadmap

- [x] **Week 1 — Foundations:** monorepo, design doc, 4 services with health endpoints, Dockerfiles and unit tests, `make up` on kind
- [ ] Week 2 — Terraform: remote state, network/cluster/registry/iam modules for OCI and AWS EKS, infra CI
- [ ] Week 3 — GitOps: Helm charts, ArgoCD, signed images, Argo Rollouts canaries, Kyverno
- [ ] Week 4 — Observability and SLOs (**gate G1**)
- [ ] Week 5 — Chaos and runbooks
- [ ] Week 6 — Detection, correlation, dataset recording (**gate G2**)
- [ ] Week 8 — Triage agent
- [ ] Week 9 — Remediation and console
- [ ] Week 10 — Evaluation runs (**gate G3**)
- [ ] Week 11 — Write-up

## Cost

Low-cost path, target ≤ 10 in total: kind for development and the dataset,
an OCI Always Free VM for the always-on demo, AWS EKS only in a few recorded
sessions on free credits, GitHub Actions and GHCR for CI, a local or free-tier
LLM for tuning and one paid model only for the final A1 test runs. See
[docs/design.md §7](docs/design.md#7-environments-and-cost-low-cost-path).

## License

[MIT](LICENSE)
