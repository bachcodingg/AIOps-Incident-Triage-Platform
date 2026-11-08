#!/usr/bin/env bash
# Gate G1, "platform observable", as a test on a running cluster:
#
#   1. metrics: every service pushes RED metrics over OTLP, with the labels the
#      SLOs and the canary analysis depend on, and exemplars that point at traces
#   2. traces: a product lookup sent with a known trace id is in Tempo as one
#      trace, from the gateway's server span down to the PostgreSQL query; a
#      search reaches its Redis calls the same way
#   3. logs: the gateway's log line for that request is in Loki, under the
#      same trace id
#   4. SLOs: Sloth's recording rules and burn-rate alerts are loaded
#   5. Grafana: up, with the RED, SLO and cost dashboards
#
# Backends are reached through the API server's service proxy, so no ports
# need forwarding. Needs curl and python3 (or python).
set -euo pipefail

CONTEXT="${CONTEXT:-kind-incidentlens}"
KUBECTL="${KUBECTL:-kubectl}"
GATEWAY="${GATEWAY:-http://localhost:8080}"
TIMEOUT="${TIMEOUT:-180}"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

# GET through the service proxy: svc <service:port> <path?query>
svc() { "$KUBECTL" --context "$CONTEXT" get --raw "/api/v1/namespaces/monitoring/services/http:$1/proxy$2" 2>/dev/null; }
prom() { svc kube-prometheus-stack-prometheus:9090 "/api/v1/query?query=$(urlencode "$1")"; }
urlencode() { "$PYTHON" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
# Number of series a PromQL query returns.
series() { prom "$1" | "$PYTHON" -c 'import json, sys; print(len(json.load(sys.stdin)["data"]["result"]))' 2>/dev/null || echo 0; }

# Retry a check until it passes or TIMEOUT runs out: retry <name> <command...>
retry() {
  local name=$1; shift
  local deadline=$((SECONDS + TIMEOUT))
  until "$@"; do
    ((SECONDS < deadline)) || fail "$name"
    sleep 5
  done
  pass "$name"
}
has_series() { (($(series "$1") > 0)); }

new_trace_id() { "$PYTHON" -c 'import secrets; print(secrets.token_hex(16))'; }
send() { curl -fsS -o /dev/null -H "traceparent: 00-$1-$("$PYTHON" -c 'import secrets; print(secrets.token_hex(8))')-01" "$2"; }

echo "observability test (gate G1)"

echo "metrics"
for s in gateway search-api catalog; do
  retry "$s pushes http.server.request.duration" \
    has_series "sum(rate(http_server_request_duration_seconds_count{job=\"shopsearch/$s\"}[2m])) > 0"
done
retry "indexer reports ready" has_series 'max(indexer_ready{job="shopsearch/indexer"}) == 1'
retry "series carry the rollout hash for canary analysis" \
  has_series 'http_server_request_duration_seconds_count{job="shopsearch/gateway",rollouts_pod_template_hash!=""}'
retry "0.3 s is a bucket boundary (latency SLO)" \
  has_series 'http_server_request_duration_seconds_bucket{job="shopsearch/gateway",le="0.3"}'
retry "Tempo writes the service graph" has_series 'traces_service_graph_request_total{client="gateway"}'
retry "CloudNativePG metrics are scraped" has_series 'cnpg_collector_up'

exemplars() {
  local now; now=$(date +%s)
  svc kube-prometheus-stack-prometheus:9090 \
    "/api/v1/query_exemplars?query=$(urlencode "http_server_request_duration_seconds_bucket{job=\"shopsearch/$1\"}")&start=$((now - 600))&end=$now" |
    "$PYTHON" -c 'import json, sys; d = json.load(sys.stdin)["data"]; sys.exit(0 if any(e["labels"].get("trace_id") for s in d for e in s["exemplars"]) else 1)'
}
retry "catalog latency buckets carry trace exemplars" exemplars catalog
retry "search-api latency buckets carry trace exemplars" exemplars search-api

echo "traces"
trace_has() { # trace_has <trace id> <python expression over `spans`>
  svc tempo:3200 "/api/traces/$1" | "$PYTHON" -c '
import json, sys
spans = []
for rs in json.load(sys.stdin).get("batches", []):
    res = {a["key"]: list(a["value"].values())[0] for a in rs["resource"]["attributes"]}
    for ss in rs.get("scopeSpans", []):
        for sp in ss["spans"]:
            attrs = {a["key"]: list(a["value"].values())[0] for a in sp.get("attributes", [])}
            spans.append({"service": res.get("service.name"), "name": sp["name"], **attrs})
services = {s["service"] for s in spans}
sys.exit(0 if eval(sys.argv[1]) else 1)' "$2" 2>/dev/null
}

lookup=$(new_trace_id)
send "$lookup" "$GATEWAY/api/products/1"
retry "product lookup $lookup: gateway -> catalog -> PostgreSQL in one trace" trace_has "$lookup" \
  '{"gateway", "catalog"} <= services and any("postgresql" in (s.get("db.system.name"), s.get("db.system")) for s in spans)'
retry "the gateway span is named after its route" trace_has "$lookup" \
  'any(s["service"] == "gateway" and s["name"] == "GET /api/products/:id" for s in spans)'

search=$(new_trace_id)
send "$search" "$GATEWAY/api/search?q=laptop"
retry "search $search: gateway -> search-api -> Redis in one trace" trace_has "$search" \
  '{"gateway", "search-api"} <= services and any("redis" in (s.get("db.system.name"), s.get("db.system")) for s in spans)'

echo "logs"
loki_has() {
  local now; now=$(date +%s)
  svc loki:3100 "/loki/api/v1/query_range?query=$(urlencode "{service_name=\"gateway\"} | trace_id=\"$1\"")&start=$((now - 900))000000000&end=${now}000000000&limit=5" |
    "$PYTHON" -c 'import json, sys; sys.exit(0 if json.load(sys.stdin)["data"]["result"] else 1)'
}
retry "gateway log line for trace $lookup is in Loki" loki_has "$lookup"

echo "SLOs"
retry "Sloth generated the 4 SLOs" has_series 'count(slo:objective:ratio{sloth_service="shopsearch"}) == 4'
alert_loaded() { svc kube-prometheus-stack-prometheus:9090 "/api/v1/rules?type=alert" | grep -q "\"name\":\"$1\""; }
for alert in SearchAvailabilityBudgetBurn SearchLatencyBudgetBurn ProductsAvailabilityBudgetBurn ProductsLatencyBudgetBurn; do
  retry "burn-rate alert $alert is loaded" alert_loaded "$alert"
done

echo "Grafana"
grafana_up() { svc kube-prometheus-stack-grafana:80 /api/health | grep -q '"database": *"ok"'; }
dashboard() { svc kube-prometheus-stack-grafana:80 "/api/search?type=dash-db" | grep -q "\"uid\":\"$1\""; }
retry "Grafana is up" grafana_up
for uid in shopsearch-red shopsearch-slo incidentlens-cost; do
  retry "dashboard $uid is provisioned" dashboard "$uid"
done

echo "all observability checks passed"
echo "follow it in Grafana: Explore -> Tempo -> trace $lookup, or the Slow requests table in ShopSearch / Service RED"
