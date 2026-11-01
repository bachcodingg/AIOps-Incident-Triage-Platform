# gitops/

Everything the cluster runs is declared here.

| Path | Status | What |
|---|---|---|
| `local/` | week 1 | plain manifests + kustomization, applied by `make up` |
| `charts/` | week 3 | one Helm chart per service (limits, probes, PDB, HPA) |
| `apps/` | week 3 | ArgoCD app-of-apps: platform add-ons, then the workload |

From week 3 on, nothing is applied with `kubectl apply` by hand.
