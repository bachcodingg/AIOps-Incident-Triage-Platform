#!/usr/bin/env bash
# Removes the AWS environment and proves nothing billable is left.
#
# Kubernetes creates load balancers and volumes that Terraform does not know
# about. Left behind, they keep costing money and they hold the VPC hostage, so
# they go first. State, budget and the CI role stay: they cost nothing or cents.
set -euo pipefail

CLUSTER="${CLUSTER:-incidentlens}"
REGION="${AWS_REGION:-eu-central-1}"
TERRAGRUNT="${TERRAGRUNT:-terragrunt}"
KUBECTL="${KUBECTL:-kubectl}"
CONTEXT="${CONTEXT:-eks-$CLUSTER}"

cd "$(dirname "$0")/.."

if aws eks describe-cluster --region "$REGION" --name "$CLUSTER" >/dev/null 2>&1; then
  aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --alias "$CONTEXT" >/dev/null
  echo "==> removing the GitOps root, which cascades to every Application"
  "$KUBECTL" --context "$CONTEXT" -n argocd delete application root --timeout=300s --ignore-not-found || true
  echo "==> removing whatever Kubernetes still owns on AWS"
  "$KUBECTL" --context "$CONTEXT" get svc -A --no-headers \
    -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,TYPE:.spec.type' |
    awk '$3 == "LoadBalancer" {print $1, $2}' |
    while read -r ns name; do "$KUBECTL" --context "$CONTEXT" -n "$ns" delete svc "$name" --timeout=180s; done
  "$KUBECTL" --context "$CONTEXT" delete pvc -A --all --timeout=180s
fi

echo "==> terragrunt destroy"
(cd infra/live/aws && "$TERRAGRUNT" run --all --non-interactive \
  --queue-exclude-dir state --queue-exclude-dir budget --queue-exclude-dir ci \
  -- destroy -auto-approve)

scripts/verify-destroyed.sh
