#!/usr/bin/env bash
# Screenshot of a Grafana dashboard, for `chaos-runner inject --on-symptom`.
# The runner calls this once a fault's symptom shows up and sets
#   CHAOS_FAULT            e.g. F05
#   CHAOS_TARGET           e.g. catalog
#   CHAOS_DASHBOARD_PATH   /d/<uid>?from=...&var-service=... (kiosk mode)
#
#   scripts/grafana-screenshot.sh <output dir>
#
# Grafana lets anonymous users view dashboards (gitops/apps/values.yaml), so a
# headless Chrome needs no login. Waits SCREENSHOT_DELAY seconds first, so the
# graphs show the symptom building up rather than its first sample.
set -euo pipefail

OUT="${1:?usage: grafana-screenshot.sh <output dir>}"
GRAFANA="${GRAFANA_URL:-http://localhost:3000}"
DELAY="${SCREENSHOT_DELAY:-45}"
CHROME="${CHROME:-$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)}"

if [[ -z "$CHROME" ]]; then
  echo "no Chrome or Chromium found, skipping the screenshot" >&2
  exit 0
fi
mkdir -p "$OUT"
sleep "$DELAY"
target="${CHAOS_TARGET//->/-to-}" # gateway->search-api becomes gateway-to-search-api
file="$OUT/${CHAOS_FAULT}-${target}.png"
# Network fetches pause Chrome's virtual clock, so the panels' queries finish
# inside the budget.
"$CHROME" --headless --no-sandbox --disable-gpu --hide-scrollbars \
  --window-size=1680,1050 --virtual-time-budget=30000 \
  --screenshot="$file" "$GRAFANA$CHAOS_DASHBOARD_PATH" >/dev/null 2>&1 || {
  echo "screenshot failed for $CHAOS_FAULT" >&2
  exit 0
}
echo "screenshot: $file"
