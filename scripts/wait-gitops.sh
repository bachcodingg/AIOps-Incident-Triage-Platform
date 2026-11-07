#!/usr/bin/env bash
# Waits until ArgoCD reports every Application as Synced and Healthy.
set -euo pipefail

CONTEXT="${CONTEXT:-kind-incidentlens}"
KUBECTL="${KUBECTL:-kubectl}"
TIMEOUT="${TIMEOUT:-900}"

apps() {
  "$KUBECTL" --context "$CONTEXT" -n argocd get applications --no-headers \
    -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
}

deadline=$((SECONDS + TIMEOUT))
last=""
while ((SECONDS < deadline)); do
  state=$(apps 2>/dev/null || true)
  pending=$(awk '$2 != "Synced" || $3 != "Healthy"' <<<"$state")
  # root plus at least one add-on: root alone is healthy before it has rendered anything.
  if [[ -n "$state" && -z "$pending" && $(wc -l <<<"$state") -gt 1 ]]; then
    echo "all $(wc -l <<<"$state") applications are Synced and Healthy after ${SECONDS}s"
    exit 0
  fi
  if [[ "$pending" != "$last" ]]; then
    printf '[%4ss] waiting for:\n%s\n' "$SECONDS" "$pending"
    last=$pending
  fi
  sleep 10
done

echo "timed out after ${TIMEOUT}s:" >&2
apps >&2
exit 1
