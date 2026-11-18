#!/usr/bin/env bash
set -euo pipefail

workload_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$(cd "$workload_dir/.." && pwd)"
binary="$workload_dir/target/debug/payment-recovery-postgres"
workload_compose=(docker compose -f "$workload_dir/docker-compose.yml")
reference_dir="$repo_root/payment-reference/deploy"
reference_compose=(docker compose -f "$reference_dir/compose.yaml")
evidence_dir="${PAYMENT_RECOVERY_EVIDENCE_DIR:-}"

export DATABASE_URL="${DATABASE_URL:-postgresql://oxide_batch_workload:oxide_batch_workload@127.0.0.1:5437/payment_recovery_workload}"
merchant_db="${MERCHANT_DATABASE_URL:-postgresql://merchant:merchant-local-only@127.0.0.1:15435/merchant}"
merchant_base="${MERCHANT_BASE_URL:-http://127.0.0.1:18083}"
gateway_base="${GATEWAY_BASE_URL:-http://127.0.0.1:18082}"
killbill_base="http://127.0.0.1:${KILLBILL_PORT:-8080}"
provider_base="${PROVIDER_BASE_URL:-http://127.0.0.1:18084}"
fault_proxy_base="${FAULT_PROXY_BASE_URL:-http://127.0.0.1:18081}"
observation_proxy_base="http://127.0.0.1:18085"

export KILLBILL_USERNAME="${KILLBILL_USERNAME:-admin}"
export KILLBILL_PASSWORD="${KILLBILL_PASSWORD:-password}"
export KILLBILL_API_KEY="${KILLBILL_API_KEY:-recovery-matrix-${RANDOM}-${RANDOM}}"
export KILLBILL_API_SECRET="${KILLBILL_API_SECRET:-recovery-secret-${RANDOM}-${RANDOM}}"

observation_proxy_pid=""

fail() {
  echo "payment-recovery matrix failure: $*" >&2
  return 1
}

cleanup_observation_proxy() {
  if [[ -n "$observation_proxy_pid" ]]; then
    kill "$observation_proxy_pid" >/dev/null 2>&1 || true
    wait "$observation_proxy_pid" 2>/dev/null || true
    observation_proxy_pid=""
  fi
}

cleanup() {
  cleanup_observation_proxy
  "${workload_compose[@]}" down --volumes >/dev/null 2>&1 || true
  "${reference_compose[@]}" down --volumes >/dev/null 2>&1 || true
}
trap cleanup EXIT


source "$workload_dir/ci/recovery-matrix/lib-runtime.sh"
source "$workload_dir/ci/recovery-matrix/lib-evidence.sh"
source "$workload_dir/ci/recovery-matrix/scenarios-recovery.sh"
source "$workload_dir/ci/recovery-matrix/scenarios-convergence.sh"


"${workload_compose[@]}" up -d --wait --wait-timeout 60
"${reference_compose[@]}" up -d --wait --wait-timeout 120

# Reference Gateway health is tenant-authenticated. Reproduce the canonical
# payment-reference bootstrap contract before asking Gateway/Merchant to become
# ready; otherwise Gateway correctly reports upstream authentication failure.
wait_for_url "$killbill_base/1.0/healthcheck"
nodes="$(
  curl --fail --silent --show-error \
    -u "${KILLBILL_USERNAME}:${KILLBILL_PASSWORD}" \
    "$killbill_base/1.0/kb/nodesInfo"
)"
printf '%s' "$nodes" | grep -q 'killbill-payment-sandbox'
wait_for_url "$fault_proxy_base/health/ready"

tenant_body="$(
  jq -nc \
    --arg api_key "$KILLBILL_API_KEY" \
    --arg api_secret "$KILLBILL_API_SECRET" \
    '{apiKey:$api_key,apiSecret:$api_secret}'
)"
tenant_status="$(
  curl --silent --show-error \
    --output /tmp/payment-recovery-tenant.json \
    --write-out '%{http_code}' \
    --request POST \
    -u "${KILLBILL_USERNAME}:${KILLBILL_PASSWORD}" \
    --header 'Content-Type: application/json' \
    --header 'X-Killbill-CreatedBy: payment-recovery-matrix' \
    --data "$tenant_body" \
    "$killbill_base/1.0/kb/tenants"
)"
if [[ "$tenant_status" != "201" ]]; then
  cat /tmp/payment-recovery-tenant.json >&2 || true
  fail "tenant bootstrap expected HTTP 201, observed $tenant_status"
fi

wait_for_url "$gateway_base/health"
wait_for_url "$merchant_base/health"
wait_for_url "$fault_proxy_base/__fault/stats"
"$binary" migrate --database-url "$DATABASE_URL"

if [[ -n "$evidence_dir" ]]; then
  rm -rf "$evidence_dir"
  mkdir -p "$evidence_dir"
fi

run_crash_case "after-candidate-read-before-replay" 1 "crash-before-replay"
run_crash_case "while-replay-outcome-unknown" 2 "crash-request-outcome-unknown"
run_crash_case "after-external-convergence-before-local-commit" 3 "crash-after-external-convergence"
run_crash_case "after-local-write-before-checkpoint-commit" 4 "crash-after-local-write"
# The transient case intentionally leaves its Reference Merchant operation
# retry-pending after proving generation advancement. Run it last so later
# single-candidate evidence fixtures never acquire that unresolved source.
run_visibility_case stale "stale-visibility-convergence" 1
run_visibility_case not-found "not-found-visibility-convergence" 2
run_async_failure_case
run_identity_conflict_case
run_retry_later_generation_case

if [[ -n "$evidence_dir" ]]; then
  count="$(find "$evidence_dir" -maxdepth 1 -type f -name '*.json' | wc -l | tr -d ' ')"
  [[ "$count" == "9" ]] || fail "expected 9 retained runtime records, observed $count"
fi

echo "payment-recovery cross-resource recovery matrix: PASS"
