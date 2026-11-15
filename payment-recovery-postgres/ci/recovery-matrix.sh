#!/usr/bin/env bash
set -euo pipefail

workload_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$(cd "$workload_dir/.." && pwd)"
binary="$workload_dir/target/debug/payment-recovery-postgres"
workload_compose=(docker compose -f "$workload_dir/docker-compose.yml")
reference_dir="$repo_root/payment-reference/deploy"
reference_compose=(docker compose -f "$reference_dir/compose.yaml")

export DATABASE_URL="${DATABASE_URL:-postgresql://oxide_batch_workload:oxide_batch_workload@127.0.0.1:5437/payment_recovery_workload}"
merchant_db="${MERCHANT_DATABASE_URL:-postgresql://merchant:merchant-local-only@127.0.0.1:15435/merchant}"
merchant_base="${MERCHANT_BASE_URL:-http://127.0.0.1:18083}"
gateway_base="${GATEWAY_BASE_URL:-http://127.0.0.1:18082}"
provider_base="${PROVIDER_BASE_URL:-http://127.0.0.1:18084}"
fault_proxy_base="${FAULT_PROXY_BASE_URL:-http://127.0.0.1:18081}"

export KILLBILL_USERNAME="${KILLBILL_USERNAME:-admin}"
export KILLBILL_PASSWORD="${KILLBILL_PASSWORD:-password}"
export KILLBILL_API_KEY="${KILLBILL_API_KEY:-recovery-matrix-${RANDOM}-${RANDOM}}"
export KILLBILL_API_SECRET="${KILLBILL_API_SECRET:-recovery-secret-${RANDOM}-${RANDOM}}"

fail() {
  echo "payment-recovery matrix failure: $*" >&2
  return 1
}

cleanup() {
  "${workload_compose[@]}" down --volumes >/dev/null 2>&1 || true
  "${reference_compose[@]}" down --volumes >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${workload_compose[@]}" up -d --wait --wait-timeout 60
"${reference_compose[@]}" up -d --wait --wait-timeout 120

wait_for_url() {
  local url="$1"
  for _ in $(seq 1 90); do
    if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  curl --fail --silent --show-error "$url" >/dev/null
}

wait_for_url "$merchant_base/health"
wait_for_url "$gateway_base/health"
wait_for_url "$fault_proxy_base/__fault/stats"
"$binary" migrate --database-url "$DATABASE_URL"

post_json() {
  local url="$1" body="$2" expected="$3" output="$4"
  local status
  status="$(curl --silent --show-error --output "$output" --write-out '%{http_code}' \
    --request POST --header 'Content-Type: application/json' --data "$body" "$url")"
  [[ "$status" == "$expected" ]] || {
    cat "$output" >&2 || true
    fail "POST $url expected $expected observed $status"
  }
}

reset_faults() {
  curl --fail --silent --show-error --request POST "$fault_proxy_base/__fault/reset" >/dev/null
}

workload_sql() {
  "${workload_compose[@]}" exec -T postgres \
    psql -U oxide_batch_workload -d payment_recovery_workload -Atc "$1"
}

merchant_sql() {
  "${reference_compose[@]}" exec -T merchant-db \
    psql -U merchant -d merchant -Atc "$1"
}

authorize_forward_count() {
  curl --fail --silent --show-error "$fault_proxy_base/__fault/stats" |
    jq '[.forwarded_by_path | to_entries[] | select(.key | endswith("/authorize")) | .value] | add // 0'
}

create_prepared_payment() {
  local suffix="$1" amount="$2"
  post_json "$merchant_base/merchants" \
    "{\"merchantId\":\"merchant-$suffix\",\"displayName\":\"Recovery $suffix\"}" \
    201 "/tmp/recovery-$suffix-merchant.json"
  post_json "$merchant_base/customers" \
    "{\"customerId\":\"customer-$suffix\",\"displayName\":\"Recovery $suffix\"}" \
    201 "/tmp/recovery-$suffix-customer.json"
  post_json "$merchant_base/orders" \
    "{\"merchantOrderId\":\"order-$suffix\",\"merchantId\":\"merchant-$suffix\",\"customerId\":\"customer-$suffix\",\"amountMinor\":$amount,\"currency\":\"USD\"}" \
    201 "/tmp/recovery-$suffix-order.json"
  post_json "$merchant_base/orders/order-$suffix/payments" \
    "{\"merchantPaymentId\":\"payment-$suffix\",\"amountMinor\":$amount,\"currency\":\"USD\",\"paymentMethodToken\":\"tok-$suffix\"}" \
    201 "/tmp/recovery-$suffix-payment.json"
}

snapshot_candidate() {
  local run_id="$1" suffix="$2"
  local business_date
  business_date="$(merchant_sql "
    SELECT (mp.created_at AT TIME ZONE 'UTC')::date::text
    FROM payment_reference_merchant.merchant_payment mp
    WHERE mp.merchant_payment_id = 'payment-$suffix'
  ")"
  [[ -n "$business_date" ]] || fail "missing business date for $suffix"

  "$binary" snapshot-candidates \
    --database-url "$DATABASE_URL" \
    --merchant-database-url "$merchant_db" \
    --run-id "$run_id" \
    --business-date "$business_date" \
    --eligibility-cutoff "2099-01-01T00:00:00Z" \
    --scenario-set-id "$run_id" >/tmp/"$run_id"-snapshot.json

  local candidate
  candidate="$(workload_sql "
    SELECT candidate_id
    FROM payment_recovery_workload.payment_recovery_candidate
    WHERE run_id = '$run_id'
      AND merchant_payment_id = 'payment-$suffix'
  ")"
  [[ -n "$candidate" ]] || fail "candidate not frozen for $suffix"
  printf '%s\n' "$candidate"
}

create_ambiguous_candidate() {
  local suffix="$1" amount="$2" output="/tmp/recovery-$suffix-authorize.json"
  create_prepared_payment "$suffix" "$amount"
  reset_faults
  post_json "$fault_proxy_base/__fault/drop-next" \
    '{"method":"POST","path_suffix":"/authorize"}' 200 "/tmp/recovery-$suffix-drop.json"
  post_json "$merchant_base/payments/payment-$suffix/authorize" \
    "{\"operationId\":\"op-$suffix\",\"amountMinor\":$amount,\"idempotencyKey\":\"idem-$suffix\"}" \
    202 "$output"
  jq -e '
    .operationState == "UNKNOWN"
    and .paymentState == "PAYMENT_UNKNOWN"
    and ((.mapping.providerPaymentId | type) == "string")
    and ((.mapping.providerPaymentId | length) > 0)
  ' "$output" >/dev/null
  [[ "$(authorize_forward_count)" == "1" ]] ||
    fail "ambiguous setup did not produce exactly one provider authorize forward"
}

run_crash_case() {
  local point="$1" ordinal="$2"
  local suffix="crash-${ordinal}" run_id="run-crash-${ordinal}"
  local marker="/tmp/payment-recovery-${ordinal}.marker"
  create_ambiguous_candidate "$suffix" "$((700 + ordinal))"
  local candidate
  candidate="$(snapshot_candidate "$run_id" "$suffix")"

  rm -f "$marker"
  "$binary" run \
    --database-url "$DATABASE_URL" \
    --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" \
    --run-id "$run_id" \
    --chunk-size 1 \
    --page-size 1 \
    --crash-point "$point" \
    --crash-candidate-id "$candidate" \
    --pause-for-kill "$marker" \
    >/tmp/"$run_id"-crash.log 2>&1 &
  local launcher_pid=$!

  for _ in $(seq 1 300); do
    [[ -f "$marker" ]] && break
    if ! kill -0 "$launcher_pid" 2>/dev/null; then
      cat /tmp/"$run_id"-crash.log >&2 || true
      fail "$point process exited before crash marker"
    fi
    sleep 0.1
  done
  [[ -f "$marker" ]] || fail "$point marker was not produced"

  local marker_pid
  marker_pid="$(head -n 1 "$marker")"
  [[ "$marker_pid" == "$launcher_pid" ]] ||
    fail "$point marker pid $marker_pid differs from launcher pid $launcher_pid"
  kill -9 "$marker_pid"
  wait "$launcher_pid" 2>/dev/null || true

  [[ "$(workload_sql "SELECT run_state FROM payment_recovery_workload.payment_recovery_run WHERE run_id = '$run_id'")" == "PROCESSING" ]] ||
    fail "$point did not leave workload state PROCESSING"
  [[ "$(workload_sql "SELECT COUNT(*) FROM payment_recovery_workload.payment_recovery_attempt WHERE run_id = '$run_id'")" == "0" ]] ||
    fail "$point leaked a committed attempt before recovery"

  "$binary" recover \
    --database-url "$DATABASE_URL" \
    --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" \
    --run-id "$run_id" \
    --chunk-size 1 \
    --page-size 1 \
    >/tmp/"$run_id"-recover.json

  jq -e '.processing_generation == 1 and .launched == true' /tmp/"$run_id"-recover.json >/dev/null
  [[ "$(workload_sql "SELECT run_state FROM payment_recovery_workload.payment_recovery_run WHERE run_id = '$run_id'")" == "PROCESSED" ]] ||
    fail "$point recovery did not finish PROCESSED"
  [[ "$(workload_sql "SELECT classification FROM payment_recovery_workload.payment_recovery_result WHERE run_id = '$run_id'")" == "RESOLVED_SUCCEEDED" ]] ||
    fail "$point recovery did not converge to RESOLVED_SUCCEEDED"
  [[ "$(workload_sql "SELECT COUNT(*) FROM payment_recovery_workload.payment_recovery_attempt WHERE run_id = '$run_id'")" == "1" ]] ||
    fail "$point recovery did not commit exactly one attempt"
  [[ "$(authorize_forward_count)" == "1" ]] ||
    fail "$point repeated the provider authorize side effect"

  "$binary" run \
    --database-url "$DATABASE_URL" \
    --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" \
    --run-id "$run_id" \
    --chunk-size 1 \
    --page-size 1 >/tmp/"$run_id"-terminal-replay.json
  jq -e '.processing_generation == 1 and .launched == false' /tmp/"$run_id"-terminal-replay.json >/dev/null
  [[ "$(authorize_forward_count)" == "1" ]] ||
    fail "$point replayed an already RESOLVED_SUCCEEEDED candidate"
}

run_retry_later_generation_case() {
  local suffix="retry-later" run_id="run-retry-later" amount=503
  create_prepared_payment "$suffix" "$amount"
  reset_faults
  post_json "$fault_proxy_base/__fault/respond-next" \
    '{"method":"POST","path_suffix":"/payments","status":503,"error_code":"provider_unavailable","message":"recovery matrix pre-side-effect 503"}' \
    200 /tmp/recovery-retry-503-plan.json
  post_json "$merchant_base/payments/payment-$suffix/authorize" \
    "{\"operationId\":\"op-$suffix\",\"amountMinor\":$amount,\"idempotencyKey\":\"idem-$suffix\"}" \
    202 /tmp/recovery-retry-503.json
  jq -e '.operationState == "RETRY_PENDING"' /tmp/recovery-retry-503.json >/dev/null

  snapshot_candidate "$run_id" "$suffix" >/dev/null

  "$binary" run \
    --database-url "$DATABASE_URL" \
    --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" \
    --run-id "$run_id" \
    --chunk-size 1 \
    --page-size 1 >/tmp/"$run_id"-g1.json
  jq -e '.processing_generation == 1 and .launched == true' /tmp/"$run_id"-g1.json >/dev/null
  [[ "$(workload_sql "SELECT classification FROM payment_recovery_workload.payment_recovery_result WHERE run_id = '$run_id'")" == "RETRY_LATER" ]] ||
    fail "generation 1 did not retain RETRY_LATER"

  "$binary" run \
    --database-url "$DATABASE_URL" \
    --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" \
    --run-id "$run_id" \
    --chunk-size 1 \
    --page-size 1 >/tmp/"$run_id"-g2.json
  jq -e '.processing_generation == 2 and .launched == true' /tmp/"$run_id"-g2.json >/dev/null

  local distinct_generations
  distinct_generations="$(workload_sql "
    SELECT COUNT(DISTINCT processing_generation)
    FROM payment_recovery_workload.payment_recovery_attempt
    WHERE run_id = '$run_id'
  ")"
  [[ "$distinct_generations" == "2" ]] ||
    fail "RETRY_LATER rerun did not create a distinct processing generation"
}

run_crash_case "after-candidate-read-before-replay" 1
run_crash_case "while-replay-outcome-unknown" 2
run_crash_case "after-external-convergence-before-local-commit" 3
run_crash_case "after-local-write-before-checkpoint-commit" 4
run_retry_later_generation_case

echo "payment-recovery cross-resource recovery matrix: PASS"
