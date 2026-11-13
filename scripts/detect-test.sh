#!/usr/bin/env bash
# The week 6 detection layer, as a test on a running cluster:
#
#   1. rules: the static thresholds (detector a, baseline B0) and Sloth's
#      burn-rate alerts (detector b, B1) are loaded and labelled by detector
#   2. the aiops service account reads what triage needs, and nothing that
#      would leak the ground truth or change the cluster: no chaos namespace,
#      no Chaos Mesh objects, no secrets, no logs or exec, no write verbs
#   3. the recorder, running as that account, records a chunk with every
#      section filled
#   4. the detectors and the correlator run on that recording
#
# Needs the aiops virtualenv (make detect-test builds it).
set -euo pipefail

CONTEXT="${CONTEXT:-kind-incidentlens}"
KUBECTL="${KUBECTL:-kubectl}"
AIOPS_PY="${AIOPS_PY:-.bin/aiops-venv/bin/python}"
PATHSEP="${PATHSEP:-:}"
TIMEOUT="${TIMEOUT:-180}"
AS=system:serviceaccount:aiops:aiops

cd "$(dirname "$0")/.."
export PYTHONPATH="aiops${PATHSEP}chaos/runner${PATHSEP}eval"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
retry() {
  local name=$1; shift
  local deadline=$((SECONDS + TIMEOUT))
  until "$@"; do
    ((SECONDS < deadline)) || fail "$name"
    sleep 5
  done
  pass "$name"
}
k() { "$KUBECTL" --context "$CONTEXT" "$@"; }
py() { "$AIOPS_PY" "$@"; }

echo "detection test (week 6)"

echo "rules"
rules() { k get --raw "/api/v1/namespaces/monitoring/services/http:kube-prometheus-stack-prometheus:9090/proxy/api/v1/rules?type=alert"; }
count_rules() { # count_rules <detector>: alerting rules carrying that detector label
  rules | py -c 'import json, sys; d = json.load(sys.stdin)["data"]["groups"]; print(sum(r.get("labels", {}).get("detector") == sys.argv[1] for g in d for r in g["rules"]))' "$1"
}
retry "9 static-threshold rules (B0) are loaded" test "$(count_rules static 2>/dev/null || echo 0)" -eq 9
retry "8 burn-rate alerts (B1, page and ticket for 4 SLOs) carry detector=burn-rate" \
  test "$(count_rules burn-rate 2>/dev/null || echo 0)" -eq 8

echo "the aiops service account"
retry "it exists (ArgoCD app aiops)" k -n aiops get serviceaccount aiops -o name
can() { k auth can-i --as="$AS" "$@" >/dev/null 2>&1; }
allowed() { if can "$@"; then pass "may $*"; else fail "may $*"; fi; }
denied() { if can "$@"; then fail "may not $*"; else pass "may not $*"; fi; }
allowed list pods -n shopsearch
allowed list events -n shopsearch
allowed get rollouts.argoproj.io -n shopsearch
allowed list pods -n kube-system
allowed list nodes
allowed list applications.argoproj.io -n argocd
allowed get services/http:kube-prometheus-stack-prometheus:9090 --subresource=proxy -n monitoring
denied list pods -n chaos
denied list events -n chaos
denied list podchaos.chaos-mesh.org -n chaos
denied list networkchaos.chaos-mesh.org --all-namespaces
denied get secrets -n shopsearch
denied get pods --subresource=log -n shopsearch
denied create pods --subresource=exec -n shopsearch
denied patch rollouts.argoproj.io -n shopsearch
denied delete pods -n shopsearch
denied get services/http:kube-prometheus-stack-grafana:80 --subresource=proxy -n monitoring
reach() { k --as="$AS" get --raw "/api/v1/namespaces/monitoring/services/http:$1" >/dev/null 2>&1; }
for backend in "kube-prometheus-stack-prometheus:9090/api/v1/query?query=up" "loki:3100/loki/api/v1/labels" "tempo:3200/api/echo"; do
  retry "it reaches ${backend%%/*} through the service proxy" reach "$backend"
done

echo "recorder"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
py -m recorder record --context "$CONTEXT" --kubectl "$KUBECTL" --out "$work/rec" --from now-11m --to now-6m >/dev/null
py - "$work/rec" <<'PY' || fail "a recorded chunk has every section"
import sys
from recorder.store import Recording
chunks = list(Recording(sys.argv[1]).chunks())
assert chunks, "no chunk recorded"
c = chunks[-1]
problems = [f"{k}: {v}" for k, v in c["errors"].items()]
if not any(s["labels"].get("job") == "shopsearch/gateway" for s in c["metrics"]["requests"] or []):
    problems.append("no gateway request rate")
if not (c["logs"] or {}).get("volume"):
    problems.append("no log volume")
if not (c["deploys"] or {}).get("rollouts"):
    problems.append("no rollouts")
if c["traces"] is None:
    problems.append("no traces section")
for p in problems:
    print(f"    {p}", file=sys.stderr)
sys.exit(1 if problems else 0)
PY
pass "a recorded chunk has every section, read as $AS"

echo "detectors and correlator"
py -m detector run --recording "$work/rec" >/dev/null || fail "detector runs on the recording"
pass "detector runs on the recording"
for v in B0 B1 B2; do
  py -m correlator --recording "$work/rec" --variant "$v" >/dev/null || fail "correlator ($v) runs on the recording"
done
pass "correlator runs for B0, B1 and B2"

echo "detection layer ok"
