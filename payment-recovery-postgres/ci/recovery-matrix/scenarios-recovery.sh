run_crash_case() {
  local point="$1" ordinal="$2" scenario="$3"
  local suffix="crash-${ordinal}" run_id="run-crash-${ordinal}"
  local marker="/tmp/payment-recovery-${ordinal}.marker"
  local phases_file side_file candidate marker_pid after_kill_state after_kill_attempts
  phases_file="$(mktemp)"; side_file="$(mktemp)"

  create_ambiguous_candidate "$suffix" "$((700 + ordinal))"
  candidate="$(snapshot_candidate "$run_id" "$suffix")"

  rm -f "$marker"
  "$binary" run \
    --database-url "$DATABASE_URL" \
    --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" \
    --run-id "$run_id" --chunk-size 1 --page-size 1 \
    --crash-point "$point" --crash-candidate-id "$candidate" --pause-for-kill "$marker" \
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
  marker_pid="$(head -n 1 "$marker")"
  [[ "$marker_pid" == "$launcher_pid" ]] || fail "$point marker pid mismatch"
  kill -9 "$marker_pid"
  wait "$launcher_pid" 2>/dev/null || true

  after_kill_state="$(workload_sql "SELECT run_state FROM payment_recovery_workload.payment_recovery_run WHERE run_id = '$run_id'")"
  after_kill_attempts="$(workload_sql "SELECT COUNT(*) FROM payment_recovery_workload.payment_recovery_attempt WHERE run_id = '$run_id'")"
  [[ "$after_kill_state" == "PROCESSING" ]] || fail "$point did not leave PROCESSING"
  [[ "$after_kill_attempts" == "0" ]] || fail "$point leaked a committed attempt"

  "$binary" recover \
    --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-recover.json
  jq -e '.processing_generation == 1 and .launched == true' /tmp/"$run_id"-recover.json >/dev/null
  [[ "$(result_classification "$run_id")" == "RESOLVED_SUCCEEDED" ]] || fail "$point did not resolve success"
  [[ "$(authorize_forward_count)" == "1" ]] || fail "$point repeated provider authorize"

  "$binary" run \
    --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-terminal-replay.json
  jq -e '.processing_generation == 1 and .launched == false' /tmp/"$run_id"-terminal-replay.json >/dev/null
  [[ "$(authorize_forward_count)" == "1" ]] || fail "$point terminal replay repeated provider authorize"

  jq -n --arg run_state "$after_kill_state" --argjson attempt_count "$after_kill_attempts" \
    '{name:"after-kill",run_state:$run_state,attempt_count:$attempt_count}' >"$phases_file"
  jq -c '{name:"after-recover",processing_generation,launched,retry_later_count}' /tmp/"$run_id"-recover.json >>"$phases_file"
  jq -c '{name:"terminal-replay",processing_generation,launched,retry_later_count}' /tmp/"$run_id"-terminal-replay.json >>"$phases_file"
  jq -n --argjson count "$(authorize_forward_count)" \
    '{provider_mutation_forward_count:$count,duplicate_provider_mutation_count:0}' >"$side_file"
  emit_record "$scenario" "hard-crash-new-process" "$run_id" \
    "$(jq -nc --arg boundary "$point" '{signal:9,boundary:$boundary}')" "$phases_file" "$side_file"
  rm -f "$phases_file" "$side_file"
}

run_retry_later_generation_case() {
  local suffix="retry-later" run_id="run-retry-later" amount=503
  local phases_file side_file
  phases_file="$(mktemp)"; side_file="$(mktemp)"
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

  "$binary" run --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-g1.json
  [[ "$(result_classification "$run_id")" == "RETRY_LATER" ]] || fail "transient generation 1 did not remain RETRY_LATER"
  jq -n --argjson report "$(cat /tmp/"$run_id"-g1.json)" --argjson result "$(result_json "$run_id")" \
    '{name:"generation-1",processing_generation:$report.processing_generation,launched:$report.launched,result:$result}' >"$phases_file"

  "$binary" run --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-g2.json
  jq -e '.processing_generation == 2 and .launched == true' /tmp/"$run_id"-g2.json >/dev/null
  jq -n --argjson report "$(cat /tmp/"$run_id"-g2.json)" --argjson result "$(result_json "$run_id")" \
    '{name:"generation-2",processing_generation:$report.processing_generation,launched:$report.launched,result:$result}' >>"$phases_file"
  jq -n --argjson count "$(authorize_forward_count)" \
    '{provider_mutation_forward_count:$count,duplicate_provider_mutation_count:0}' >"$side_file"
  emit_record "pre-side-effect-transient" "retryable-pre-side-effect-failure" "$run_id" null "$phases_file" "$side_file"
  rm -f "$phases_file" "$side_file"
}

run_visibility_case() {
  local mode="$1" scenario="$2" ordinal="$3"
  local suffix="visibility-${ordinal}" run_id="run-visibility-${ordinal}"
  local phases_file side_file
  phases_file="$(mktemp)"; side_file="$(mktemp)"
  create_ambiguous_candidate "$suffix" "$((810 + ordinal))"
  snapshot_candidate "$run_id" "$suffix" >/dev/null

  start_observation_proxy "$mode"
  "$binary" run --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$observation_proxy_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-g1.json
  [[ "$(result_classification "$run_id")" == "RETRY_LATER" ]] || fail "$scenario first generation did not remain RETRY_LATER"
  jq -n --argjson report "$(cat /tmp/"$run_id"-g1.json)" --argjson result "$(result_json "$run_id")" \
    '{name:"generation-1",processing_generation:$report.processing_generation,launched:$report.launched,result:$result}' >"$phases_file"
  cleanup_observation_proxy

  "$binary" run --database-url "$DATABASE_URL" --merchant-base-url "$merchant_base" \
    --provider-base-url "$provider_base" --run-id "$run_id" --chunk-size 1 --page-size 1 \
    >/tmp/"$run_id"-g2.json
  [[ "$(result_classification "$run_id")" == "RESOLVED_SUCCEEDED" ]] || fail "$scenario did not converge after authoritative visibility"
  [[ "$(authorize_forward_count)" == "1" ]] || fail "$scenario duplicated provider mutation"
  jq -n --argjson report "$(cat /tmp/"$run_id"-g2.json)" --argjson result "$(result_json "$run_id")" \
    '{name:"generation-2",processing_generation:$report.processing_generation,launched:$report.launched,result:$result}' >>"$phases_file"
  jq -n --argjson count "$(authorize_forward_count)" \
    '{provider_mutation_forward_count:$count,duplicate_provider_mutation_count:0}' >"$side_file"
  emit_record "$scenario" "provider-visibility-convergence" "$run_id" null "$phases_file" "$side_file"
  rm -f "$phases_file" "$side_file"
}

