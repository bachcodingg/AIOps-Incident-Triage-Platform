#!/usr/bin/env bash
# Proves the week 3 "done when" on a running cluster:
#
#   1. a good release walks the canary steps and is promoted by the analysis
#   2. a bad release is aborted by the analysis, with users left on the old one
#   3. admission control rejects pods that break the policies
#
# Releases are simulated by changing an env var on the gateway, which makes a new
# ReplicaSet exactly as a new image does. The real image path is the same Rollout.
set -euo pipefail

CONTEXT="${CONTEXT:-kind-incidentlens}"
KUBECTL="${KUBECTL:-kubectl}"
GATEWAY="${GATEWAY:-http://localhost:8080}"
ROLLOUT="${ROLLOUT:-gateway}"

A=("$KUBECTL" --context "$CONTEXT" -n argocd)
S=("$KUBECTL" --context "$CONTEXT" -n shopsearch)

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() {
  printf '  \033[31mFAIL\033[0m %s\n' "$1"
  "${S[@]}" get rollout "$ROLLOUT" -o yaml | sed -n '/^status:/,$p' || true
  "${S[@]}" get analysisruns -o wide || true
  exit 1
}

field() { "${S[@]}" get rollout "$ROLLOUT" -o jsonpath="{$1}"; }

latest_run() {
  "${S[@]}" get analysisruns --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' | grep "^$ROLLOUT-" | tail -1
}

# Releases are made on the gateway's Application, which root would put back.
hold_root() {
  "${A[@]}" patch application root --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]' >/dev/null
}
release_root() {
  "${A[@]}" patch application "$ROLLOUT" --type json -p '[{"op":"remove","path":"/spec/source/helm/parameters"}]' >/dev/null 2>&1 || true
  "${A[@]}" patch application root --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null
}

release() {
  "${A[@]}" patch application "$ROLLOUT" --type merge \
    -p "{\"spec\":{\"source\":{\"helm\":{\"parameters\":[{\"name\":\"env.$1\",\"value\":\"$2\"}]}}}}" >/dev/null
}

wait_new_hash() {
  local old=$1
  for _ in $(seq 1 60); do
    [[ "$(field .status.currentPodHash)" != "$old" ]] && return 0
    sleep 3
  done
  fail "ArgoCD did not roll out the new release"
}

trap release_root EXIT
hold_root

echo "scenario 1: a good release is promoted"
stable=$(field .status.stableRS)
release RELEASE_NONCE "good-$RANDOM"
wait_new_hash "$stable"
good=$(field .status.currentPodHash)
pass "new release $good started"
for _ in $(seq 1 100); do
  [[ "$(field .status.stableRS)" == "$good" ]] && break
  [[ "$(field .status.abort)" == "true" ]] && fail "healthy release was aborted"
  sleep 3
done
[[ "$(field .status.stableRS)" == "$good" ]] || fail "release was not promoted in time"
pass "release $good promoted to stable"
run=$(latest_run)
[[ "$run" == *" Successful" ]] && pass "analysis passed: $run" || fail "analysis did not pass: $run"

echo "scenario 2: a release that returns 500s is aborted"
release FAULT_ERROR_RATE 0.5
wait_new_hash "$good"
bad=$(field .status.currentPodHash)
pass "bad release $bad started"
for _ in $(seq 1 100); do
  [[ "$(field .status.abort)" == "true" ]] && break
  [[ "$(field .status.stableRS)" == "$bad" ]] && fail "bad release was promoted"
  sleep 3
done
[[ "$(field .status.abort)" == "true" ]] || fail "bad release was not aborted in time"
pass "rollout aborted without a human"
run=$(latest_run)
[[ "$run" == *" Failed" ]] && pass "analysis failed it: $run" || fail "unexpected analysis result: $run"
[[ "$(field .status.stableRS)" == "$good" ]] && pass "stable release is still $good" || fail "stable release changed"

for _ in $(seq 1 40); do
  [[ "$("${S[@]}" get pods -l "rollouts-pod-template-hash=$bad" -o name | wc -l)" -eq 0 ]] && break
  sleep 3
done
[[ "$("${S[@]}" get pods -l "rollouts-pod-template-hash=$bad" -o name | wc -l)" -eq 0 ]] \
  && pass "bad pods removed" || fail "bad pods are still running"
curl -fsS "$GATEWAY/api/search?q=laptop" >/dev/null && pass "users are served by the old release" \
  || fail "gateway stopped serving"

echo "scenario 3: admission control"
rejected() {
  local name=$1 pattern=$2 manifest=$3 out
  if out=$("$KUBECTL" --context "$CONTEXT" apply --dry-run=server -f - <<<"$manifest" 2>&1); then
    fail "$name was admitted"
  fi
  grep -qE "$pattern" <<<"$out" && pass "$name rejected" || fail "$name failed for another reason: $out"
}
pod() {
  cat <<EOF
apiVersion: v1
kind: Pod
metadata: {name: policy-test, namespace: shopsearch}
spec:
  containers:
    - name: app
      image: $1
      resources: $2
EOF
}
limits='{limits: {cpu: 100m, memory: 64Mi}}'
rejected "pod without limits" 'require-limits' "$(pod redis:8.2-alpine '{}')"
rejected "pod with a latest tag" 'disallow-latest' "$(pod redis:latest "$limits")"
rejected "pod from an unknown registry" 'restrict-registries' "$(pod quay.io/someone/miner:1.0 "$limits")"
rejected "unsigned image in our registry" 'verify-signatures|signed-by-the-release-workflow' \
  "$(pod 'ghcr.io/bachcodingg/incidentlens/gateway:unsigned' "$limits")"

echo "all canary and policy checks passed"
