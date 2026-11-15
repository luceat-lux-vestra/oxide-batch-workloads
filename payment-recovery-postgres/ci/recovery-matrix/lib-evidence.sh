emit_record() {
  [[ -n "$evidence_dir" ]] || return 0
  local scenario="$1" scenario_kind="$2" run_id="$3" failure_point_json="$4"
  local phases_file="$5" side_effects_file="$6" provider_id
  mkdir -p "$evidence_dir"
  provider_id="$(provider_id_for_run "$run_id")"
  [[ -n "$provider_id" ]] || fail "$scenario has no provider identity for retained provider truth"
  jq -n \
    --arg scenario "$scenario" \
    --arg scenario_kind "$scenario_kind" \
    --argjson failure_point "$failure_point_json" \
    --argjson candidate "$(candidate_json "$run_id")" \
    --argjson run "$(run_json "$run_id")" \
    --argjson attempts "$(attempts_json "$run_id")" \
    --argjson result "$(result_json "$run_id")" \
    --argjson provider_truth "$(provider_truth_json "$provider_id" "${7:-$provider_base}")" \
    --argjson fault "$(fault_summary_json)" \
    --slurpfile phases "$phases_file" \
    --slurpfile side "$side_effects_file" \
    '{
      schema_version:1,
      campaign_issue:271,
      contract_version:"payment-recovery-v1",
      validation_subject:"oxide-batch 0.7.0",
      scenario:$scenario,
      scenario_kind:$scenario_kind,
      failure_point:$failure_point,
      candidate:$candidate,
      observations:{run:$run,attempts:$attempts,result:$result,provider_truth:$provider_truth,fault:$fault},
      side_effects:$side[0],
      phases:$phases
    }' >"$evidence_dir/$scenario.json"
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
  local business_date candidate
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

  # Retained evidence assumes one frozen initial operation per scenario.
  # Detect cross-scenario contamination instead of silently merging candidates.
  jq -e '.candidate_count == 1' /tmp/"$run_id"-snapshot.json >/dev/null ||
    fail "$run_id candidate acquisition was not isolated to one payment"

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
  local visibility_plan
  reset_faults
  visibility_plan="$(jq -nc --arg suffix "$suffix" '{
    fault_plan_version:1,
    scenario_version:1,
    seed:27101,
    logical_business_time:"2026-11-15T00:00:00Z",
    scenario_case:("issue-271-unknown-seed-" + $suffix),
    expected_provider_truth:"AUTHORIZED",
    expected_canonical_outcome:"PAYMENT_UNKNOWN/UNKNOWN",
    steps:[
      {
        step_id:"hide-initial-provider-status",
        sequence:1,
        offset_ms:0,
        point:"STATUS_INQUIRY",
        request_match:{method:"GET",path_prefix:"/payments/"},
        action:{kind:"STATUS_VISIBILITY",visibility:"NOT_FOUND"}
      },
      {
        step_id:"reveal-provider-status",
        sequence:2,
        offset_ms:1,
        point:"STATUS_INQUIRY",
        request_match:{method:"GET",path_prefix:"/payments/"},
        action:{kind:"STATUS_VISIBILITY",visibility:"IMMEDIATE"}
      }
    ]
  }')"
  post_json "$fault_proxy_base/__fault/load-plan" "$visibility_plan" \
    200 "/tmp/recovery-$suffix-visibility-plan.json"
  post_json "$fault_proxy_base/__fault/advance" '{"offset_ms":0}' \
    200 "/tmp/recovery-$suffix-hide-provider-status.json"
  post_json "$fault_proxy_base/__fault/drop-next" \
    '{"method":"POST","path_suffix":"/authorize"}' 200 "/tmp/recovery-$suffix-drop.json"
  post_json "$merchant_base/payments/payment-$suffix/authorize" \
    "{\"operationId\":\"op-$suffix\",\"amountMinor\":$amount,\"idempotencyKey\":\"idem-$suffix\"}" \
    202 "$output"
  post_json "$fault_proxy_base/__fault/advance" '{"offset_ms":1}' \
    200 "/tmp/recovery-$suffix-reveal-provider-status.json"
  jq -e '
    .operationState == "UNKNOWN"
    and .paymentState == "PAYMENT_UNKNOWN"
    and ((.mapping.providerPaymentId | type) == "string")
    and ((.mapping.providerPaymentId | length) > 0)
  ' "$output" >/dev/null
  [[ "$(authorize_forward_count)" == "1" ]] ||
    fail "ambiguous setup did not produce exactly one provider authorize forward"
}

start_observation_proxy() {
  local mode="$1"
  cleanup_observation_proxy
  python3 "$workload_dir/evidence/provider-observation-proxy.py" \
    --listen 127.0.0.1:18085 --upstream "$provider_base" --mode "$mode" \
    >/tmp/payment-recovery-observation-proxy.log 2>&1 &
  observation_proxy_pid=$!
  wait_for_url "$observation_proxy_base/health"
}

