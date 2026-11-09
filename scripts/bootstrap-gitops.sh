#!/usr/bin/env bash
# Installs ArgoCD and hands the cluster over to Git. This is the only
# `kubectl apply` in the project; everything after it comes from gitops/.
# It also creates the secrets Git must not hold.
#
#   ENVIRONMENT        kind | aws | oci, selects gitops/apps/values-<env>.yaml
#   REVISION           Git branch ArgoCD follows
#   LOCAL_IMAGES       true to run images built on this machine (kind only)
#   ALERT_WEBHOOK_URL  optional Discord or Slack webhook for SLO alerts
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-kind}"
REVISION="${REVISION:-main}"
LOCAL_IMAGES="${LOCAL_IMAGES:-false}"
CONTEXT="${CONTEXT:-kind-incidentlens}"
KUBECTL="${KUBECTL:-kubectl}"
HELM="${HELM:-helm}"
REPO_URL="${REPO_URL:-https://github.com/bachcodingg/AIOps-Incident-Triage-Platform.git}"
ARGOCD_CHART_VERSION="${ARGOCD_CHART_VERSION:-10.14.0}"

cd "$(dirname "$0")/.."

# ArgoCD reads the branch from GitHub, not this working tree.
if git rev-parse --verify --quiet "origin/$REVISION" >/dev/null; then
  if [[ -n "$(git log --oneline "origin/$REVISION..HEAD" 2>/dev/null)" ]]; then
    echo "warning: HEAD has commits that are not on origin/$REVISION; ArgoCD will not see them" >&2
  fi
else
  echo "warning: origin/$REVISION does not exist; ArgoCD cannot sync until it is pushed" >&2
fi

# Loading images right before this can stall a small node's API server for a while.
for _ in $(seq 1 30); do
  "$KUBECTL" --context "$CONTEXT" get namespace default --request-timeout=10s >/dev/null 2>&1 && break
  sleep 5
done

# Secrets are never in Git. Grafana's admin password is random, made once per
# cluster (`make grafana` prints it). The alert webhook is optional.
K=("$KUBECTL" --context "$CONTEXT")
"${K[@]}" create namespace monitoring --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
if ! "${K[@]}" -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  password=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true)
  "${K[@]}" -n monitoring create secret generic grafana-admin \
    --from-literal=admin-user=admin --from-literal=admin-password="$password" >/dev/null
  echo "created monitoring/grafana-admin"
fi
if [[ -n "${ALERT_WEBHOOK_URL:-}" ]]; then
  "${K[@]}" -n monitoring create secret generic alert-webhook --from-literal=url="$ALERT_WEBHOOK_URL" \
    --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
  echo "stored the alert webhook in monitoring/alert-webhook"
fi

"$HELM" upgrade --install argocd argo-cd \
  --repo https://argoproj.github.io/argo-helm \
  --version "$ARGOCD_CHART_VERSION" \
  --kube-context "$CONTEXT" \
  --namespace argocd --create-namespace \
  --values gitops/bootstrap/argocd-values.yaml \
  --wait --timeout 5m

"$KUBECTL" --context "$CONTEXT" apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: root
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  source:
    repoURL: $REPO_URL
    targetRevision: $REVISION
    path: gitops/apps
    helm:
      valueFiles:
        - values.yaml
        - values-$ENVIRONMENT.yaml
      parameters:
        - {name: revision, value: "$REVISION"}
        - {name: localImages, value: "$LOCAL_IMAGES"}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
EOF
