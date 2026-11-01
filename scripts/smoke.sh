#!/usr/bin/env bash
# End-to-end smoke test for the local kind deployment.
#
#   1. gateway health
#   2. search through gateway -> search-api -> Redis index (with a typo)
#   3. product lookup through gateway -> catalog -> Postgres
#   4. write path: create a product in catalog, wait until the indexer has
#      consumed the change event and the product is searchable, then delete it
set -euo pipefail

GATEWAY="${GATEWAY:-http://localhost:8080}"
KUBECTL="${KUBECTL:-kubectl}"
CONTEXT="${CONTEXT:-kind-incidentlens}"
NAMESPACE="${NAMESPACE:-shopsearch}"
CATALOG_PORT="${CATALOG_PORT:-18080}"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

# Retry until the body matches, so the test tolerates pods that just became ready.
expect() {
  local name=$1 url=$2 pattern=$3 body=""
  for _ in $(seq 1 30); do
    body=$(curl -fsS "$url" 2>/dev/null || true)
    if grep -qE "$pattern" <<<"$body"; then pass "$name"; return 0; fi
    sleep 2
  done
  echo "    last body: ${body:-<none>}"
  fail "$name"
}

echo "smoke test against $GATEWAY"
expect "gateway /healthz" "$GATEWAY/healthz" '"status":"ok"'
expect "search with typo is corrected" "$GATEWAY/api/search?q=lapotp" '"corrected_query":"laptop"'
expect "search returns ranked laptops" "$GATEWAY/api/search?q=gaming%20laptop" '"name":"Gaming Laptop 15 RTX"'
expect "product lookup via catalog" "$GATEWAY/api/products/1" '"sku":"LT-001"'

code=$(curl -s -o /dev/null -w '%{http_code}' "$GATEWAY/api/products/999999")
[[ $code == 404 ]] && pass "unknown product is 404" || fail "unknown product returned $code"

# Write path. Catalog is not exposed publicly, so go through a port-forward.
"$KUBECTL" --context "$CONTEXT" -n "$NAMESPACE" port-forward svc/catalog "$CATALOG_PORT:8080" >/dev/null 2>&1 &
pf=$!
trap 'kill $pf 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do curl -fsS "http://localhost:$CATALOG_PORT/q/health/live" >/dev/null 2>&1 && break; sleep 0.5; done

sku="SMOKE-$RANDOM$RANDOM"
created=$(curl -fsS -X POST "http://localhost:$CATALOG_PORT/products" -H 'Content-Type: application/json' \
  -d "{\"sku\":\"$sku\",\"name\":\"Smoketest Zeppelin\",\"description\":\"created by scripts/smoke.sh\",\"category\":\"test\",\"price\":1.0,\"stock\":1}")
id=$(sed -E 's/.*"id":([0-9]+).*/\1/' <<<"$created")
[[ $id =~ ^[0-9]+$ ]] && pass "catalog created product $id" || fail "create product: $created"

expect "indexer consumed the change event" "$GATEWAY/api/search?q=zeppelin" "\"id\":$id"

curl -fsS -X DELETE "http://localhost:$CATALOG_PORT/products/$id" >/dev/null && pass "catalog deleted product $id"
expect "deleted product left the index" "$GATEWAY/api/search?q=zeppelin" '"total":0'

echo "all smoke checks passed"
