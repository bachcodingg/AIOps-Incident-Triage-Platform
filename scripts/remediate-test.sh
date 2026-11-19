#!/usr/bin/env bash
# The week 9 remediator, as a test on a running cluster, changing nothing:
#
#   1. the aiops-remediator account can make exactly the changes it needs, on
#      the objects named in gitops/aiops, and nothing else: no other object,
#      no create or delete, no secrets, not the root Application, not the
#      chaos namespace
#   2. the read-only aiops account still cannot write
#   3. the remediator's restart and scale-out plans, read from the live
#      objects, pass the API server with --dry-run=server (RBAC, admission
#      and schema included), so the patches it would send are real ones
#
# Needs the aiops virtualenv (make remediate-test builds it).
set -euo pipefail

CONTEXT="${CONTEXT:-kind-incidentlens}"
KUBECTL="${KUBECTL:-kubectl}"
AIOPS_PY="${AIOPS_PY:-.bin/aiops-venv/bin/python}"
PATHSEP="${PATHSEP:-:}"
TIMEOUT="${TIMEOUT:-180}"
REM=system:serviceaccount:aiops:aiops-remediator
RO=system:serviceaccount:aiops:aiops

cd "$(dirname "$0")/.."
export PYTHONPATH="aiops${PATHSEP}chaos/runner${PATHSEP}eval"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
k() { "$KUBECTL" --context "$CONTEXT" "$@"; }
can() { [[ "$(k auth can-i "$@" 2>/dev/null)" == yes ]]; }

echo "remediation test (week 9)"

deadline=$((SECONDS + TIMEOUT))
until k -n aiops get serviceaccount aiops-remediator >/dev/null 2>&1; do
  ((SECONDS < deadline)) || fail "the aiops-remediator account exists (ArgoCD app aiops)"
  sleep 5
done
pass "the aiops-remediator account exists"

allowed=(
  "patch rollouts.argoproj.io/catalog -n shopsearch"
  "patch rollouts.argoproj.io/gateway -n shopsearch --subresource=status"
  "patch horizontalpodautoscalers.autoscaling/search-api -n shopsearch"
  "patch deployments.apps/redis -n shopsearch"
  "patch clusters.postgresql.cnpg.io/postgres -n shopsearch"
  "patch applications.argoproj.io/indexer -n argocd"
)
for a in "${allowed[@]}"; do
  # shellcheck disable=SC2086
  can $a --as "$REM" || fail "remediator may: $a"
done
pass "the remediator may make each change it needs (${#allowed[@]} checked)"

denied=(
  "patch applications.argoproj.io/root -n argocd"
  "patch applications.argoproj.io/aiops -n argocd"
  "patch deployments.apps/coredns -n kube-system"
  "patch statefulsets.apps/anything -n shopsearch"
  "delete pods -n shopsearch"
  "delete rollouts.argoproj.io/catalog -n shopsearch"
  "create deployments.apps -n shopsearch"
  "get secrets -n shopsearch"
  "create pods/exec -n shopsearch"
  "patch networkchaos.chaos-mesh.org -n chaos"
  "list rollouts.argoproj.io -n shopsearch"
)
for d in "${denied[@]}"; do
  # shellcheck disable=SC2086
  can $d --as "$REM" && fail "remediator must not: $d"
done
pass "the remediator cannot do anything else (${#denied[@]} checked)"

for d in "patch rollouts.argoproj.io/catalog -n shopsearch" "patch applications.argoproj.io/catalog -n argocd" \
         "patch horizontalpodautoscalers.autoscaling/gateway -n shopsearch"; do
  # shellcheck disable=SC2086
  can $d --as "$RO" && fail "the read-only aiops account must not: $d"
done
pass "the read-only aiops account still cannot write"

rem() { "$AIOPS_PY" -m remediator plan "$@" --context "$CONTEXT" --kubectl "$KUBECTL" --dry-run; }
for a in "restart catalog" "restart redis" "restart postgres" "scale_out gateway"; do
  # shellcheck disable=SC2086
  out=$(rem $a 2>&1) || { echo "$out"; fail "dry run: $a"; }
done
pass "restart catalog, redis, postgres and scale out gateway pass the API server (--dry-run=server)"

# Rollback needs a release to go back to; a fresh cluster has one. Planning
# must then refuse cleanly, not fail.
set +e
out=$(rem rollback catalog 2>&1); rc=$?
set -e
case $rc in
  0) pass "rollback catalog passes the API server (--dry-run=server)" ;;
  3) pass "rollback catalog refused without an earlier release: ${out#no plan: }" ;;
  *) echo "$out"; fail "rollback catalog: unexpected failure" ;;
esac
