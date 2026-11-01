# infra/

Infrastructure as code.

| Path | Status | What |
|---|---|---|
| `kind/cluster.yaml` | week 1 | local kind cluster used by `make up` |
| `modules/` | week 2 | Terraform modules: `network`, `cluster`, `registry`, `iam`, `state` |
| `live/` | week 2 | Terragrunt environments: `dev` (kind), `oci` (k3s on OCI Always Free), `aws` (EKS, recorded sessions only) |

Low-cost path: OCI is the always-on target; AWS EKS is applied only during
credit-funded sessions and destroyed afterwards. Both stay in CI with
`validate` and `plan`.
