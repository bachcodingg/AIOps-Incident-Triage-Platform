#!/usr/bin/env bash
# Static checks for everything under gitops/. Needs no cluster.
set -euo pipefail

HELM="${HELM:-helm}"
KUBECONFORM="${KUBECONFORM:-kubeconform}"
KYVERNO="${KYVERNO:-kyverno}"
CHARTS="${CHARTS:-gateway search-api catalog indexer redis postgres loadgen}"

cd "$(dirname "$0")/.."

# Rollout, PodMonitor and the like are CRDs, so they need community schemas.
CRD_SCHEMAS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
conform() { "$KUBECONFORM" -strict -summary -schema-location default -schema-location "$CRD_SCHEMAS"; }

for chart in $CHARTS; do
  echo "==> chart $chart"
  grep -q '^dependencies:' "gitops/charts/$chart/Chart.yaml" && "$HELM" dependency build "gitops/charts/$chart" >/dev/null
  "$HELM" lint --quiet "gitops/charts/$chart"
  "$HELM" template "$chart" "gitops/charts/$chart" --namespace shopsearch | conform
done

for env in kind aws oci; do
  echo "==> apps for $env"
  "$HELM" template root gitops/apps -f "gitops/apps/values-$env.yaml" | conform
done

echo "==> observability"
"$HELM" lint --quiet gitops/observability
"$HELM" template observability gitops/observability --namespace monitoring | conform
# Every dashboard must be valid JSON with a unique uid.
python3 - gitops/observability/files/dashboards/*.json <<'PY'
import json, sys
uids = [json.load(open(p))["uid"] for p in sys.argv[1:]]
assert len(uids) == len(set(uids)), f"duplicate dashboard uid in {uids}"
print(f"{len(uids)} dashboards: {', '.join(uids)}")
PY

echo "==> policies"
"$HELM" lint --quiet gitops/policies
"$HELM" template policies gitops/policies | conform
"$HELM" template policies gitops/policies > gitops/policies/tests/rendered.yaml
"$KYVERNO" test gitops/policies/tests
