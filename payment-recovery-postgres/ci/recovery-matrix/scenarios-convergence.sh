run_identity_conflict_case() {
  local suffix="identity-conflict" run_id="run-identity-conflict" amount=901
  local phases_file side_file
  phases_file="$(mktemp)"; side_file="$(mktemp)"
  create_ambiguous_candidate "$suffix" "$amount"
  snapshot_candidate "$run_id" "$suffix" >/dev/null
  start_observation_proxy identity-conflict

  "$binary" run --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$observation_proxy_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-g1.json
  cleanup_observation_proxy
  [[ "$(result_classification "$run_id")" == "HOLD_MANUAL" ]] || fail "identity conflict did not HOLD_MANUAL"
  [[ "$(authorize_forward_count)" == "1" ]] || fail "identity observation changed provider mutation count"

  "$binary" run --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-terminal-replay.json
  jq -e '.launched == false' /tmp/"$run_id"-terminal-replay.json >/dev/null
  jq -c '{name:"terminal-replay",processing_generation,launched,retry_later_count}' /tmp/"$run_id"-terminal-replay.json >"$phases_file"
  jq -n --argjson count "$(authorize_forward_count)" \
    '{provider_mutation_forward_count:$count,duplicate_provider_mutation_count:0}' >"$side_file"
  emit_record "identity-conflict-hold" "read-only-provider-identity-conflict" "$run_id" null "$phases_file" "$side_file"
  rm -f "$phases_file" "$side_file"
}

create_unknown_without_gateway() {
  local suffix="$1" amount="$2"
  create_prepared_payment "$suffix" "$amount"
  "${reference_compose[@]}" stop gateway >/dev/null
  post_json "$merchant_base/payments/payment-$suffix/authorize" \
    "{\"operationId\":\"op-$suffix\",\"amountMinor\":$amount,\"idempotencyKey\":\"idem-$suffix\"}" \
    502 /tmp/"$suffix"-merchant-unknown.json
  "${reference_compose[@]}" start gateway >/dev/null
  wait_for_url "$gateway_base/health"
  [[ "$(merchant_sql "SELECT operation_state FROM payment_reference_merchant.merchant_payment_operation WHERE operation_id = 'op-$suffix'")" == "UNKNOWN" ]] ||
    fail "$suffix merchant operation was not persisted UNKNOWN"
}

seed_failed_provider_overlay() {
  local provider_id="$1" amount="$2"
  local plan
  local status
  status="$(curl --silent --show-error --output /tmp/provider-failure-seed-create.json --write-out '%{http_code}' \
    --request POST --header 'Accept: application/json' \
    --header "Idempotency-Key: fixture-create-$provider_id" \
    --header "X-Correlation-ID: issue-271-create-$provider_id" \
    --header 'Content-Type: application/json' \
    --data "{\"id\":\"$provider_id\",\"amount\":$amount,\"currency\":\"USD\"}" \
    "$fault_proxy_base/payments")"
  [[ "$status" == "201" ]] || { cat /tmp/provider-failure-seed-create.json >&2; fail "failed to seed deterministic provider object"; }
  reset_faults
  plan="$(jq -nc --arg path "/payments/$provider_id/authorize" --arg status "/payments/$provider_id" '{
    fault_plan_version:1,scenario_version:1,seed:27105,
    logical_business_time:"2026-11-15T00:00:00Z",
    scenario_case:"issue-271-async-failure",
    expected_provider_truth:"FAILED",expected_canonical_outcome:"FAILED",
    steps:[
      {step_id:"defer",sequence:1,offset_ms:0,point:"REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",request_match:{method:"POST",path_suffix:$path},action:{kind:"DEFER_ASYNC",operation_id:"issue-271-failed-auth"}},
      {step_id:"fail",sequence:2,offset_ms:100,point:"PROVIDER_TRANSITION",action:{kind:"PROVIDER_TRANSITION",operation_id:"issue-271-failed-auth",outcome:"FAILED"}}
    ]
  }')"
  post_json "$fault_proxy_base/__fault/load-plan" "$plan" 200 /tmp/provider-failure-plan.json
  post_json "$fault_proxy_base/__fault/advance" '{"offset_ms":0}' 200 /tmp/provider-failure-advance0.json
  status="$(curl --silent --show-error --output /tmp/provider-failure-pending.json --write-out '%{http_code}' \
    --request POST --header 'Accept: application/json' \
    --header "Idempotency-Key: fixture-authorize-$provider_id" \
    --header "X-Correlation-ID: issue-271-authorize-$provider_id" \
    --data '' "$fault_proxy_base/payments/$provider_id/authorize")"
  [[ "$status" == "200" ]] || { cat /tmp/provider-failure-pending.json >&2; fail "failed to defer deterministic provider authorize"; }
  post_json "$fault_proxy_base/__fault/advance" '{"offset_ms":100}' 200 /tmp/provider-failure-advance100.json
  curl --fail --silent --show-error "$fault_proxy_base/payments/$provider_id" | jq -e '.status == "failed"' >/dev/null
}

run_async_failure_case() {
  local suffix="async-failure" run_id="run-async-failure" amount=912
  local phases_file side_file provider_id transition_effect
  phases_file="$(mktemp)"; side_file="$(mktemp)"
  create_unknown_without_gateway "$suffix" "$amount"
  snapshot_candidate "$run_id" "$suffix" >/dev/null

  reset_faults
  post_json "$fault_proxy_base/__fault/respond-next" \
    '{"method":"POST","path_suffix":"/payments","status":422,"error_code":"payment_rejected","message":"deterministic nonretryable provider failure"}' \
    200 /tmp/async-failure-reject-plan.json
  "$binary" run --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$fault_proxy_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-g1.json
  provider_id="$(provider_id_for_run "$run_id")"
  [[ -n "$provider_id" ]] || fail "async failure first replay did not expose provider identity"
  [[ "$(workload_sql "SELECT canonical_operation_state FROM payment_recovery_workload.payment_recovery_attempt WHERE run_id = '$run_id' ORDER BY attempt_no DESC LIMIT 1")" == "FAILED" ]] ||
    fail "async failure first replay did not produce canonical FAILED"
  [[ "$(result_classification "$run_id")" == "RETRY_LATER" ]] || fail "async failure first provider absence must remain RETRY_LATER"
  jq -n --argjson report "$(cat /tmp/"$run_id"-g1.json)" --argjson result "$(result_json "$run_id")" \
    '{name:"generation-1",processing_generation:$report.processing_generation,launched:$report.launched,result:$result}' >"$phases_file"

  reset_faults
  seed_failed_provider_overlay "$provider_id" "$amount"
  transition_effect="$(fault_stats | jq -r '.transition_history[-1].provider_side_effect')"
  [[ "$transition_effect" == "false" ]] || fail "failed provider transition unexpectedly recorded a successful side effect"

  "$binary" run --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$fault_proxy_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-g2.json
  [[ "$(result_classification "$run_id")" == "RESOLVED_FAILED" ]] || fail "async failure did not converge RESOLVED_FAILED"
  jq -n --argjson report "$(cat /tmp/"$run_id"-g2.json)" --argjson result "$(result_json "$run_id")" \
    '{name:"generation-2",processing_generation:$report.processing_generation,launched:$report.launched,result:$result}' >>"$phases_file"
  jq -n --argjson effect "$transition_effect" \
    '{successful_provider_effect_count:0,provider_transition_effect:$effect,provider_mutation_forward_count:0,duplicate_provider_mutation_count:0}' >"$side_file"
  emit_record "async-failure-convergence" "deterministic-provider-failure" "$run_id" null "$phases_file" "$side_file" "$fault_proxy_base"
  rm -f "$phases_file" "$side_file"
}
