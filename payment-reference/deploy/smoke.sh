#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
compose=(docker compose -f compose.yaml)

killbill_port="${KILLBILL_PORT:-8080}"
gateway_port="${GATEWAY_PORT:-18082}"
merchant_port="${MERCHANT_PORT:-18083}"
fault_proxy_port="${PAYMENT_SANDBOX_FAULT_PROXY_PORT:-18081}"

killbill_base="http://127.0.0.1:${killbill_port}"
gateway_base="http://127.0.0.1:${gateway_port}"
merchant_base="http://127.0.0.1:${merchant_port}"
fault_proxy_base="http://127.0.0.1:${fault_proxy_port}"

result_path="${PAYMENT_REFERENCE_RESULT_PATH:-${PWD}/evidence/payment-reference-scenario-result.json}"
source_revision="${PAYMENT_REFERENCE_SOURCE_REVISION:-${GITHUB_SHA:-local}}"
scenario_version=1
scenario_seed=256
logical_business_date="2026-11-13"

export KILLBILL_USERNAME="${KILLBILL_USERNAME:-admin}"
export KILLBILL_PASSWORD="${KILLBILL_PASSWORD:-password}"
export KILLBILL_API_KEY="${KILLBILL_API_KEY:-pr-smoke-${RANDOM}-${RANDOM}}"
export KILLBILL_API_SECRET="${KILLBILL_API_SECRET:-pr-secret-${RANDOM}-${RANDOM}-${RANDOM}}"

diagnose() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${compose[@]}" ps || true
    "${compose[@]}" logs --no-color --tail=250 \
      merchant merchant-db gateway gateway-db killbill killbill-db \
      payment-sandbox-fault-proxy payment-sandbox || true
  fi
  exit "$status"
}
trap diagnose EXIT

fail() {
  printf 'payment-reference smoke failure: %s\n' "$*" >&2
  return 1
}

wait_for_url() {
  local url="$1"
  local attempts="${2:-90}"
  for _ in $(seq 1 "$attempts"); do
    if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  curl --fail --silent --show-error "$url" >/dev/null
}

post_gateway() {
  local path="$1"
  local body="$2"
  local expected_status="$3"
  local output="$4"
  local status
  status="$(
    curl --silent --show-error \
      --output "$output" \
      --write-out '%{http_code}' \
      --request POST \
      --header 'Content-Type: application/json' \
      --data "$body" \
      "${gateway_base}${path}"
  )"
  if [ "$status" != "$expected_status" ]; then
    cat "$output" >&2 || true
    fail "POST ${path}: expected HTTP ${expected_status}, observed ${status}"
  fi
}

get_gateway() {
  local payment_id="$1"
  local output="$2"
  local status
  status="$(
    curl --silent --show-error \
      --output "$output" \
      --write-out '%{http_code}' \
      "${gateway_base}/payments/${payment_id}"
  )"
  if [ "$status" != "200" ]; then
    cat "$output" >&2 || true
    fail "GET /payments/${payment_id}: expected HTTP 200, observed ${status}"
  fi
}

post_merchant() {
  local path="$1"
  local body="$2"
  local expected_status="$3"
  local output="$4"
  local status
  status="$(
    curl --silent --show-error \
      --output "$output" \
      --write-out '%{http_code}' \
      --request POST \
      --header 'Content-Type: application/json' \
      --data "$body" \
      "${merchant_base}${path}"
  )"
  if [ "$status" != "$expected_status" ]; then
    cat "$output" >&2 || true
    fail "Merchant POST ${path}: expected HTTP ${expected_status}, observed ${status}"
  fi
}

provider_payment_status() {
  local provider_payment_id="$1"
  "${compose[@]}" exec -T payment-sandbox \
    sh -c 'curl --silent --output /dev/null --write-out "%{http_code}" "http://127.0.0.1:8080/payments/$1"' \
    sh "$provider_payment_id"
}

assert_view() {
  local file="$1"
  local payment_state="$2"
  local operation_state="$3"
  jq -e \
    --arg payment_state "$payment_state" \
    --arg operation_state "$operation_state" \
    '
      .paymentState == $payment_state
      and .lastOperationState == $operation_state
      and ((.mapping.killbillPaymentId | type) == "string")
      and ((.mapping.killbillPaymentId | length) > 0)
      and ((.mapping.killbillTransactionId | type) == "string")
      and ((.mapping.killbillTransactionId | length) > 0)
    ' "$file" >/dev/null
}

killbill_payment() {
  local payment_id="$1"
  local output="$2"
  curl --fail --silent --show-error \
    -u "${KILLBILL_USERNAME}:${KILLBILL_PASSWORD}" \
    -H "X-Killbill-ApiKey: ${KILLBILL_API_KEY}" \
    -H "X-Killbill-ApiSecret: ${KILLBILL_API_SECRET}" \
    -H 'X-Killbill-CreatedBy: payment-reference-smoke' \
    "${killbill_base}/1.0/kb/payments/${payment_id}/?withPluginInfo=true" \
    >"$output"
}

provider_payment() {
  local provider_payment_id="$1"
  local output="$2"
  "${compose[@]}" exec -T payment-sandbox \
    curl --fail --silent --show-error \
      "http://127.0.0.1:8080/payments/${provider_payment_id}" \
    >"$output"
}

"${compose[@]}" up -d --build

wait_for_url "${killbill_base}/1.0/healthcheck"

nodes="$(
  curl --fail --silent --show-error \
    -u "${KILLBILL_USERNAME}:${KILLBILL_PASSWORD}" \
    "${killbill_base}/1.0/kb/nodesInfo"
)"
printf '%s' "$nodes" | grep -q 'killbill-payment-sandbox'

wait_for_url "${fault_proxy_base}/health/ready"

tenant_body="$(
  jq -nc \
    --arg api_key "$KILLBILL_API_KEY" \
    --arg api_secret "$KILLBILL_API_SECRET" \
    '{apiKey:$api_key,apiSecret:$api_secret}'
)"
tenant_status="$(
  curl --silent --show-error \
    --output /tmp/payment-reference-tenant.json \
    --write-out '%{http_code}' \
    --request POST \
    -u "${KILLBILL_USERNAME}:${KILLBILL_PASSWORD}" \
    --header 'Content-Type: application/json' \
    --header 'X-Killbill-CreatedBy: payment-reference-smoke' \
    --data "$tenant_body" \
    "${killbill_base}/1.0/kb/tenants"
)"
if [ "$tenant_status" != "201" ]; then
  cat /tmp/payment-reference-tenant.json >&2 || true
  fail "tenant bootstrap expected HTTP 201, observed ${tenant_status}"
fi

wait_for_url "${gateway_base}/health"
wait_for_url "${merchant_base}/health"

curl --fail --silent --show-error \
  --request POST \
  "${fault_proxy_base}/__fault/reset" >/dev/null

# Happy path A: AUTHORIZE -> partial CAPTURE -> full CAPTURE
#               -> partial REFUND -> full REFUND.
lifecycle_authorize='{"merchantId":"merchant-smoke","merchantOrderId":"order-lifecycle","merchantPaymentId":"payment-lifecycle","operationId":"op-auth-001","amountMinor":1000,"currency":"USD","paymentMethodToken":"tok-lifecycle","idempotencyKey":"idem-auth-001"}'
post_gateway "/payments/authorize" "$lifecycle_authorize" 200 /tmp/pr-auth.json
assert_view /tmp/pr-auth.json AUTHORIZED SUCCEEDED

lifecycle_kb_id="$(jq -er '.mapping.killbillPaymentId' /tmp/pr-auth.json)"
lifecycle_provider_id="$(jq -er '.mapping.providerPaymentId' /tmp/pr-auth.json)"

capture_600='{"merchantId":"merchant-smoke","merchantOrderId":"order-lifecycle","merchantPaymentId":"payment-lifecycle","operationId":"op-capture-600","amountMinor":600,"currency":"USD","idempotencyKey":"idem-capture-600"}'
post_gateway "/payments/payment-lifecycle/capture" "$capture_600" 200 /tmp/pr-capture-600.json
assert_view /tmp/pr-capture-600.json PARTIALLY_CAPTURED SUCCEEDED

capture_400='{"merchantId":"merchant-smoke","merchantOrderId":"order-lifecycle","merchantPaymentId":"payment-lifecycle","operationId":"op-capture-400","amountMinor":400,"currency":"USD","idempotencyKey":"idem-capture-400"}'
post_gateway "/payments/payment-lifecycle/capture" "$capture_400" 200 /tmp/pr-capture-400.json
assert_view /tmp/pr-capture-400.json PAID SUCCEEDED

refund_250='{"merchantId":"merchant-smoke","merchantOrderId":"order-lifecycle","merchantPaymentId":"payment-lifecycle","operationId":"op-refund-250","amountMinor":250,"currency":"USD","idempotencyKey":"idem-refund-250"}'
post_gateway "/payments/payment-lifecycle/refund" "$refund_250" 200 /tmp/pr-refund-250.json
assert_view /tmp/pr-refund-250.json PARTIALLY_REFUNDED SUCCEEDED

refund_750='{"merchantId":"merchant-smoke","merchantOrderId":"order-lifecycle","merchantPaymentId":"payment-lifecycle","operationId":"op-refund-750","amountMinor":750,"currency":"USD","idempotencyKey":"idem-refund-750"}'
post_gateway "/payments/payment-lifecycle/refund" "$refund_750" 200 /tmp/pr-refund-750.json
assert_view /tmp/pr-refund-750.json REFUNDED SUCCEEDED

get_gateway payment-lifecycle /tmp/pr-lifecycle-get.json
assert_view /tmp/pr-lifecycle-get.json REFUNDED SUCCEEDED

provider_payment "$lifecycle_provider_id" /tmp/pr-provider-lifecycle.json
jq -e '
  .status == "refunded"
  and .authorized_amount == 1000
  and .captured_amount == 1000
  and .refunded_amount == 1000
' /tmp/pr-provider-lifecycle.json >/dev/null

killbill_payment "$lifecycle_kb_id" /tmp/pr-kb-lifecycle.json
jq -e '
  (.transactions | length) == 5
  and ([.transactions[] | select(.transactionType == "AUTHORIZE")] | length) == 1
  and ([.transactions[] | select(.transactionType == "CAPTURE")] | length) == 2
  and ([.transactions[] | select(.transactionType == "REFUND")] | length) == 2
' /tmp/pr-kb-lifecycle.json >/dev/null

# Exact replay and conflicts must not add another Kill Bill transaction.
before_replay_count="$(jq -er '.transactions | length' /tmp/pr-kb-lifecycle.json)"

post_gateway "/payments/authorize" "$lifecycle_authorize" 200 /tmp/pr-auth-replay.json
jq -e \
  --arg transaction_id "$(jq -er '.mapping.killbillTransactionId' /tmp/pr-auth.json)" \
  '.mapping.killbillTransactionId == $transaction_id' \
  /tmp/pr-auth-replay.json >/dev/null

conflicting_replay='{"merchantId":"merchant-smoke","merchantOrderId":"order-lifecycle","merchantPaymentId":"payment-lifecycle","operationId":"op-auth-001","amountMinor":1100,"currency":"USD","paymentMethodToken":"tok-lifecycle","idempotencyKey":"idem-auth-001"}'
post_gateway "/payments/authorize" "$conflicting_replay" 409 /tmp/pr-auth-conflict.json

operation_id_conflict='{"merchantId":"merchant-smoke","merchantOrderId":"order-lifecycle","merchantPaymentId":"payment-lifecycle","operationId":"op-auth-001","amountMinor":1000,"currency":"USD","paymentMethodToken":"tok-lifecycle","idempotencyKey":"idem-auth-new-key"}'
post_gateway "/payments/authorize" "$operation_id_conflict" 409 /tmp/pr-operation-conflict.json

killbill_payment "$lifecycle_kb_id" /tmp/pr-kb-lifecycle-after-replays.json
after_replay_count="$(jq -er '.transactions | length' /tmp/pr-kb-lifecycle-after-replays.json)"
[ "$before_replay_count" = "$after_replay_count" ] ||
  fail "idempotency replay/conflict created a new Kill Bill transaction"

# Happy path B: AUTH_CAPTURE -> PURCHASE semantics.
purchase='{"merchantId":"merchant-smoke","merchantOrderId":"order-purchase","merchantPaymentId":"payment-purchase","operationId":"op-purchase-001","amountMinor":500,"currency":"USD","paymentMethodToken":"tok-purchase","idempotencyKey":"idem-purchase-001"}'
post_gateway "/payments/auth-capture" "$purchase" 200 /tmp/pr-purchase.json
assert_view /tmp/pr-purchase.json PAID SUCCEEDED

purchase_provider_id="$(jq -er '.mapping.providerPaymentId' /tmp/pr-purchase.json)"
provider_payment "$purchase_provider_id" /tmp/pr-provider-purchase.json
jq -e '
  .status == "captured"
  and .authorized_amount == 500
  and .captured_amount == 500
' /tmp/pr-provider-purchase.json >/dev/null

# Happy path C: AUTHORIZE -> CANCEL / VOID.
cancel_authorize='{"merchantId":"merchant-smoke","merchantOrderId":"order-cancel","merchantPaymentId":"payment-cancel","operationId":"op-cancel-auth","amountMinor":700,"currency":"USD","paymentMethodToken":"tok-cancel","idempotencyKey":"idem-cancel-auth"}'
post_gateway "/payments/authorize" "$cancel_authorize" 200 /tmp/pr-cancel-auth.json
assert_view /tmp/pr-cancel-auth.json AUTHORIZED SUCCEEDED

cancel_mutation='{"merchantId":"merchant-smoke","merchantOrderId":"order-cancel","merchantPaymentId":"payment-cancel","operationId":"op-cancel-void","amountMinor":700,"currency":"USD","idempotencyKey":"idem-cancel-void"}'
post_gateway "/payments/payment-cancel/cancel" "$cancel_mutation" 200 /tmp/pr-cancel.json
assert_view /tmp/pr-cancel.json CANCELLED SUCCEEDED

cancel_provider_id="$(jq -er '.mapping.providerPaymentId' /tmp/pr-cancel.json)"
provider_payment "$cancel_provider_id" /tmp/pr-provider-cancel.json
jq -e '.status == "cancelled"' /tmp/pr-provider-cancel.json >/dev/null

# Negative ambiguity: provider AUTHORIZE commits, but its HTTP response is
# dropped once. The plugin must expose UNKNOWN and Gateway replay must not
# blindly authorize again.
curl --fail --silent --show-error \
  --request POST \
  "${fault_proxy_base}/__fault/reset" >/dev/null
curl --fail --silent --show-error \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"method":"POST","path_suffix":"/authorize"}' \
  "${fault_proxy_base}/__fault/drop-next" >/dev/null

ambiguous_authorize='{"merchantId":"merchant-smoke","merchantOrderId":"order-ambiguous","merchantPaymentId":"payment-ambiguous","operationId":"op-ambiguous-auth","amountMinor":777,"currency":"USD","paymentMethodToken":"tok-ambiguous","idempotencyKey":"idem-ambiguous-auth"}'
post_gateway "/payments/authorize" "$ambiguous_authorize" 202 /tmp/pr-ambiguous.json
assert_view /tmp/pr-ambiguous.json PAYMENT_UNKNOWN UNKNOWN

ambiguous_provider_id="$(jq -er '.mapping.providerPaymentId' /tmp/pr-ambiguous.json)"
ambiguous_transaction_id="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-ambiguous.json)"

provider_payment "$ambiguous_provider_id" /tmp/pr-provider-ambiguous.json
jq -e '
  .status == "authorized"
  and .authorized_amount == 777
' /tmp/pr-provider-ambiguous.json >/dev/null

curl --fail --silent --show-error \
  "${fault_proxy_base}/__fault/stats" >/tmp/pr-fault-stats-before-replay.json
jq -e '
  .dropped == 1
  and (.last_dropped_path | endswith("/authorize"))
  and (
    [.forwarded_by_path | to_entries[]
      | select(.key | endswith("/authorize"))
      | .value] | add // 0
  ) == 1
' /tmp/pr-fault-stats-before-replay.json >/dev/null

post_gateway "/payments/authorize" "$ambiguous_authorize" 202 /tmp/pr-ambiguous-replay.json
assert_view /tmp/pr-ambiguous-replay.json PAYMENT_UNKNOWN UNKNOWN
jq -e \
  --arg transaction_id "$ambiguous_transaction_id" \
  '.mapping.killbillTransactionId == $transaction_id' \
  /tmp/pr-ambiguous-replay.json >/dev/null

curl --fail --silent --show-error \
  "${fault_proxy_base}/__fault/stats" >/tmp/pr-fault-stats-after-replay.json
jq -e '
  .dropped == 1
  and (
    [.forwarded_by_path | to_entries[]
      | select(.key | endswith("/authorize"))
      | .value] | add // 0
  ) == 1
' /tmp/pr-fault-stats-after-replay.json >/dev/null

get_gateway payment-ambiguous /tmp/pr-ambiguous-get.json
assert_view /tmp/pr-ambiguous-get.json PAYMENT_UNKNOWN UNKNOWN

provider_payment "$ambiguous_provider_id" /tmp/pr-provider-ambiguous-after-replay.json
jq -e '
  .status == "authorized"
  and .authorized_amount == 777
' /tmp/pr-provider-ambiguous-after-replay.json >/dev/null

# ---------------------------------------------------------------------------
# Integrated Merchant -> Gateway negative: provider 503 before any provider
# side effect. The one-shot synthetic response is consumed before the proxy
# opens an upstream connection, so payment-sandbox must observe no create.
# ---------------------------------------------------------------------------
post_merchant "/merchants"   '{"merchantId":"merchant-provider-503","displayName":"Provider 503 merchant"}'   201 /tmp/pr-merchant-503-merchant.json
post_merchant "/customers"   '{"customerId":"customer-provider-503","displayName":"Provider 503 customer"}'   201 /tmp/pr-merchant-503-customer.json
post_merchant "/orders"   '{"merchantOrderId":"order-provider-503","merchantId":"merchant-provider-503","customerId":"customer-provider-503","amountMinor":503,"currency":"USD"}'   201 /tmp/pr-merchant-503-order.json
post_merchant "/orders/order-provider-503/payments"   '{"merchantPaymentId":"payment-provider-503","amountMinor":503,"currency":"USD","paymentMethodToken":"tok-provider-503"}'   201 /tmp/pr-merchant-503-payment.json

curl --fail --silent --show-error   --request POST   "${fault_proxy_base}/__fault/reset" >/dev/null
curl --fail --silent --show-error   --request POST   --header 'Content-Type: application/json'   --data '{"method":"POST","path_suffix":"/payments","status":503,"error_code":"provider_unavailable","message":"synthetic provider unavailable before side effect"}'   "${fault_proxy_base}/__fault/respond-next" >/dev/null

merchant_503_authorize='{"operationId":"op-provider-503","amountMinor":503,"idempotencyKey":"idem-provider-503"}'
post_merchant   "/payments/payment-provider-503/authorize"   "$merchant_503_authorize"   202   /tmp/pr-merchant-provider-503.json

jq -e '
  .merchantPaymentId == "payment-provider-503"
  and .operationId == "op-provider-503"
  and .paymentState == "PAYMENT_PENDING"
  and .operationState == "RETRY_PENDING"
  and ((.mapping.killbillPaymentId | type) == "string")
  and ((.mapping.killbillPaymentId | length) > 0)
  and ((.mapping.killbillTransactionId | type) == "string")
  and ((.mapping.killbillTransactionId | length) > 0)
  and ((.mapping.providerPaymentId | type) == "string")
  and ((.mapping.providerPaymentId | length) > 0)
' /tmp/pr-merchant-provider-503.json >/dev/null

provider_503_id="$(jq -er '.mapping.providerPaymentId' /tmp/pr-merchant-provider-503.json)"

curl --fail --silent --show-error   "${fault_proxy_base}/__fault/stats" >/tmp/pr-fault-stats-provider-503.json
jq -e '
  .responded == 1
  and (.last_responded_path == "POST /payments")
  and (.responded_by_path["POST /payments"] == 1)
  and ((.forwarded_by_path["POST /payments"] // 0) == 0)
  and .dropped == 0
' /tmp/pr-fault-stats-provider-503.json >/dev/null

provider_503_status="$(provider_payment_status "$provider_503_id")"
[ "$provider_503_status" = "404" ] ||
  fail "provider 503-before-side-effect unexpectedly created payment ${provider_503_id}: HTTP ${provider_503_status}"

# Exact Merchant replay must retain the same retryable canonical operation and
# still must not forward a provider create.
post_merchant   "/payments/payment-provider-503/authorize"   "$merchant_503_authorize"   202   /tmp/pr-merchant-provider-503-replay.json
jq -e   --arg kb_payment_id "$(jq -er '.mapping.killbillPaymentId' /tmp/pr-merchant-provider-503.json)"   --arg kb_transaction_id "$(jq -er '.mapping.killbillTransactionId' /tmp/pr-merchant-provider-503.json)"   '
    .paymentState == "PAYMENT_PENDING"
    and .operationState == "RETRY_PENDING"
    and .mapping.killbillPaymentId == $kb_payment_id
    and .mapping.killbillTransactionId == $kb_transaction_id
  ' /tmp/pr-merchant-provider-503-replay.json >/dev/null

curl --fail --silent --show-error   "${fault_proxy_base}/__fault/stats" >/tmp/pr-fault-stats-provider-503-after-replay.json
jq -e '
  .responded == 1
  and (.responded_by_path["POST /payments"] == 1)
  and ((.forwarded_by_path["POST /payments"] // 0) == 0)
' /tmp/pr-fault-stats-provider-503-after-replay.json >/dev/null

# Run the deterministic #263 fault/async matrix against the real payment-sandbox
# proxy runtime. This writes compact evidence consumed by the retained suite
# artifact below; it does not create a second evidence format.
fault_matrix_path="/tmp/payment-reference-fault-matrix.json"
bash ./fault-matrix-smoke.sh "$fault_proxy_base" "$fault_matrix_path"

# Retain one machine-readable result artifact for the bounded online-path suite.
# Every value below is sourced from assertions that have already passed above;
# the result is evidence, not a second execution path.
mkdir -p "$(dirname "$result_path")"

lifecycle_auth_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-auth.json)"
lifecycle_capture_600_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-capture-600.json)"
lifecycle_capture_400_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-capture-400.json)"
lifecycle_refund_250_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-refund-250.json)"
lifecycle_refund_750_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-refund-750.json)"
purchase_kb_id="$(jq -er '.mapping.killbillPaymentId' /tmp/pr-purchase.json)"
purchase_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-purchase.json)"
cancel_kb_id="$(jq -er '.mapping.killbillPaymentId' /tmp/pr-cancel.json)"
cancel_auth_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-cancel-auth.json)"
cancel_void_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-cancel.json)"
ambiguous_kb_id="$(jq -er '.mapping.killbillPaymentId' /tmp/pr-ambiguous.json)"
provider_503_kb_id="$(jq -er '.mapping.killbillPaymentId' /tmp/pr-merchant-provider-503.json)"
provider_503_tx="$(jq -er '.mapping.killbillTransactionId' /tmp/pr-merchant-provider-503.json)"

jq -n   --arg source_revision "$source_revision"   --argjson scenario_version "$scenario_version"   --argjson seed "$scenario_seed"   --arg logical_business_date "$logical_business_date"   --arg lifecycle_kb_id "$lifecycle_kb_id"   --arg lifecycle_provider_id "$lifecycle_provider_id"   --arg lifecycle_auth_tx "$lifecycle_auth_tx"   --arg lifecycle_capture_600_tx "$lifecycle_capture_600_tx"   --arg lifecycle_capture_400_tx "$lifecycle_capture_400_tx"   --arg lifecycle_refund_250_tx "$lifecycle_refund_250_tx"   --arg lifecycle_refund_750_tx "$lifecycle_refund_750_tx"   --arg lifecycle_replay_kb_id "$(jq -er '.mapping.killbillPaymentId' /tmp/pr-auth-replay.json)"   --arg lifecycle_replay_tx "$(jq -er '.mapping.killbillTransactionId' /tmp/pr-auth-replay.json)"   --arg purchase_kb_id "$purchase_kb_id"   --arg purchase_provider_id "$purchase_provider_id"   --arg purchase_tx "$purchase_tx"   --arg cancel_kb_id "$cancel_kb_id"   --arg cancel_provider_id "$cancel_provider_id"   --arg cancel_auth_tx "$cancel_auth_tx"   --arg cancel_void_tx "$cancel_void_tx"   --arg ambiguous_kb_id "$ambiguous_kb_id"   --arg ambiguous_provider_id "$ambiguous_provider_id"   --arg ambiguous_tx "$ambiguous_transaction_id"   --arg ambiguous_replay_kb_id "$(jq -er '.mapping.killbillPaymentId' /tmp/pr-ambiguous-replay.json)"   --arg ambiguous_replay_tx "$(jq -er '.mapping.killbillTransactionId' /tmp/pr-ambiguous-replay.json)"   --arg provider_503_kb_id "$provider_503_kb_id"   --arg provider_503_provider_id "$provider_503_id"   --arg provider_503_tx "$provider_503_tx"   --arg provider_503_replay_kb_id "$(jq -er '.mapping.killbillPaymentId' /tmp/pr-merchant-provider-503-replay.json)"   --arg provider_503_replay_tx "$(jq -er '.mapping.killbillTransactionId' /tmp/pr-merchant-provider-503-replay.json)"   --slurpfile lifecycle_provider /tmp/pr-provider-lifecycle.json   --slurpfile purchase_provider /tmp/pr-provider-purchase.json   --slurpfile cancel_provider /tmp/pr-provider-cancel.json   --slurpfile ambiguous_provider /tmp/pr-provider-ambiguous-after-replay.json   --slurpfile fault_matrix "$fault_matrix_path"   '
  def identity($case; $expected):
    {
      scenario_version: $scenario_version,
      seed: $seed,
      logical_business_date: $logical_business_date,
      scenario_case: $case,
      expected_outcome: $expected
    };
  def provider($p):
    {
      exists: true,
      status: ($p.status // null),
      authorized_amount_minor: ($p.authorized_amount // null),
      captured_amount_minor: ($p.captured_amount // null),
      refunded_amount_minor: ($p.refunded_amount // null)
    };
  {
    result_version: 1,
    schema_id: "https://oxide-batch.dev/payment-reference/scenario-result-v1.schema.json",
    source_revision: $source_revision,
    suite: identity(
      "payment-reference-online-path-v1";
      "bounded online path invariants hold"
    ),
    fault_matrix: $fault_matrix[0],
    scenarios: [
      {
        identity: identity(
          "authorize-capture-refund-lifecycle";
          "REFUNDED/SUCCEEDED with exact replay and conflicts creating no extra effect"
        ),
        system_ids: {
          merchant_id: "merchant-smoke",
          merchant_order_id: "order-lifecycle",
          merchant_payment_id: "payment-lifecycle",
          operation_ids: [
            "op-auth-001",
            "op-capture-600",
            "op-capture-400",
            "op-refund-250",
            "op-refund-750"
          ],
          killbill_payment_id: $lifecycle_kb_id,
          killbill_transaction_ids: [
            $lifecycle_auth_tx,
            $lifecycle_capture_600_tx,
            $lifecycle_capture_400_tx,
            $lifecycle_refund_250_tx,
            $lifecycle_refund_750_tx
          ],
          provider_payment_id: $lifecycle_provider_id
        },
        final_canonical_state: {
          payment_state: "REFUNDED",
          operation_state: "SUCCEEDED"
        },
        provider_state: provider($lifecycle_provider[0]),
        replay: {
          performed: true,
          same_killbill_payment_id: ($lifecycle_replay_kb_id == $lifecycle_kb_id),
          same_killbill_transaction_id: ($lifecycle_replay_tx == $lifecycle_auth_tx),
          duplicate_provider_mutation_observed: false,
          conflict_http_statuses: [409, 409]
        },
        unexpected_invariant_violations: []
      },
      {
        identity: identity(
          "auth-capture-purchase";
          "PAID/SUCCEEDED and provider captured"
        ),
        system_ids: {
          merchant_id: "merchant-smoke",
          merchant_order_id: "order-purchase",
          merchant_payment_id: "payment-purchase",
          operation_ids: ["op-purchase-001"],
          killbill_payment_id: $purchase_kb_id,
          killbill_transaction_ids: [$purchase_tx],
          provider_payment_id: $purchase_provider_id
        },
        final_canonical_state: {
          payment_state: "PAID",
          operation_state: "SUCCEEDED"
        },
        provider_state: provider($purchase_provider[0]),
        replay: {
          performed: false,
          same_killbill_payment_id: null,
          same_killbill_transaction_id: null,
          duplicate_provider_mutation_observed: null,
          conflict_http_statuses: []
        },
        unexpected_invariant_violations: []
      },
      {
        identity: identity(
          "authorize-cancel-void";
          "CANCELLED/SUCCEEDED and provider cancelled"
        ),
        system_ids: {
          merchant_id: "merchant-smoke",
          merchant_order_id: "order-cancel",
          merchant_payment_id: "payment-cancel",
          operation_ids: ["op-cancel-auth", "op-cancel-void"],
          killbill_payment_id: $cancel_kb_id,
          killbill_transaction_ids: [$cancel_auth_tx, $cancel_void_tx],
          provider_payment_id: $cancel_provider_id
        },
        final_canonical_state: {
          payment_state: "CANCELLED",
          operation_state: "SUCCEEDED"
        },
        provider_state: provider($cancel_provider[0]),
        replay: {
          performed: false,
          same_killbill_payment_id: null,
          same_killbill_transaction_id: null,
          duplicate_provider_mutation_observed: null,
          conflict_http_statuses: []
        },
        unexpected_invariant_violations: []
      },
      {
        identity: identity(
          "provider-commit-response-loss";
          "PAYMENT_UNKNOWN/UNKNOWN with one provider authorize and no blind replay"
        ),
        system_ids: {
          merchant_id: "merchant-smoke",
          merchant_order_id: "order-ambiguous",
          merchant_payment_id: "payment-ambiguous",
          operation_ids: ["op-ambiguous-auth"],
          killbill_payment_id: $ambiguous_kb_id,
          killbill_transaction_ids: [$ambiguous_tx],
          provider_payment_id: $ambiguous_provider_id
        },
        final_canonical_state: {
          payment_state: "PAYMENT_UNKNOWN",
          operation_state: "UNKNOWN"
        },
        provider_state: provider($ambiguous_provider[0]),
        replay: {
          performed: true,
          same_killbill_payment_id: ($ambiguous_replay_kb_id == $ambiguous_kb_id),
          same_killbill_transaction_id: ($ambiguous_replay_tx == $ambiguous_tx),
          duplicate_provider_mutation_observed: false,
          conflict_http_statuses: []
        },
        unexpected_invariant_violations: []
      },
      {
        identity: identity(
          "provider-503-before-side-effect";
          "PAYMENT_PENDING/RETRY_PENDING with provider payment absent"
        ),
        system_ids: {
          merchant_id: "merchant-provider-503",
          merchant_order_id: "order-provider-503",
          merchant_payment_id: "payment-provider-503",
          operation_ids: ["op-provider-503"],
          killbill_payment_id: $provider_503_kb_id,
          killbill_transaction_ids: [$provider_503_tx],
          provider_payment_id: $provider_503_provider_id
        },
        final_canonical_state: {
          payment_state: "PAYMENT_PENDING",
          operation_state: "RETRY_PENDING"
        },
        provider_state: {
          exists: false,
          status: null,
          authorized_amount_minor: null,
          captured_amount_minor: null,
          refunded_amount_minor: null
        },
        replay: {
          performed: true,
          same_killbill_payment_id: ($provider_503_replay_kb_id == $provider_503_kb_id),
          same_killbill_transaction_id: ($provider_503_replay_tx == $provider_503_tx),
          duplicate_provider_mutation_observed: false,
          conflict_http_statuses: []
        },
        unexpected_invariant_violations: []
      }
    ],
    unexpected_invariant_violations: []
  }
  ' >"$result_path"

jq -e '
  .result_version == 1
  and .schema_id == "https://oxide-batch.dev/payment-reference/scenario-result-v1.schema.json"
  and (.source_revision | type == "string" and length > 0)
  and (.suite.scenario_version == 1)
  and (.fault_matrix.fault_plan_version == 1)
  and (.fault_matrix.cases | length == 5)
  and ([.fault_matrix.cases[].unexpected_invariant_violations | length] | add) == 0
  and (.fault_matrix.unexpected_invariant_violations | length) == 0
  and (.scenarios | length == 5)
  and ([.scenarios[].unexpected_invariant_violations | length] | add) == 0
  and (.unexpected_invariant_violations | length) == 0
  and all(
    .scenarios[].replay;
    if .performed then
      .same_killbill_payment_id == true
      and .same_killbill_transaction_id == true
      and .duplicate_provider_mutation_observed == false
    else
      true
    end
  )
' "$result_path" >/dev/null

printf 'payment-reference end-to-end smoke PASS\n'
printf 'Scenario result: %s\n' "$result_path"
printf 'Merchant: %s\n' "$merchant_base"
printf 'Gateway: %s\n' "$gateway_base"
printf 'Kill Bill: %s\n' "$killbill_base"
printf 'Plugin registration: killbill-payment-sandbox\n'
printf 'Happy paths: authorize/capture/refund, auth-capture, cancel\n'
printf 'Negative paths: idempotency conflict, operation-id conflict, response-loss ambiguity, provider-503-before-side-effect\n'
printf 'Fault matrix: plan-503, post-commit-loss, async-success/stale-convergence, async-failure, webhook ordering/duplicate/drop/retry\n'

trap - EXIT
