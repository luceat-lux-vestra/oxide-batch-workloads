#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
compose=(docker compose -f compose.yaml)
port="${KILLBILL_PORT:-8080}"
base="http://127.0.0.1:${port}"

diagnose() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${compose[@]}" ps || true
    "${compose[@]}" logs --no-color --tail=200 killbill killbill-db payment-sandbox || true
  fi
  exit "$status"
}
trap diagnose EXIT

"${compose[@]}" up -d --build

for _ in $(seq 1 90); do
  if curl --fail --silent --show-error "${base}/1.0/healthcheck" >/tmp/payment-reference-health.json 2>/dev/null; then
    break
  fi
  sleep 2
done

curl --fail --silent --show-error "${base}/1.0/healthcheck" >/tmp/payment-reference-health.json

nodes="$(
  curl --fail --silent --show-error \
    -u "${KILLBILL_ADMIN_USER:-admin}:${KILLBILL_ADMIN_PASSWORD:-password}" \
    "${base}/1.0/kb/nodesInfo"
)"

printf '%s' "$nodes" | grep -q 'killbill-payment-sandbox'

"${compose[@]}" exec -T payment-sandbox \
  curl --fail --silent --show-error http://127.0.0.1:8080/health/ready >/dev/null

printf 'payment-reference runtime smoke PASS\n'
printf 'Kill Bill: %s\n' "$base"
printf 'Plugin registration: killbill-payment-sandbox\n'

trap - EXIT
