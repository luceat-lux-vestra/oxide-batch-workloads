wait_for_url() {
  local url="$1"
  for _ in $(seq 1 120); do
    if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  curl --fail --silent --show-error "$url" >/dev/null
}

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

fault_stats() {
  curl --fail --silent --show-error "$fault_proxy_base/__fault/stats"
}

authorize_forward_count() {
  fault_stats | jq '[.forwarded_by_path | to_entries[] | select(.key | endswith("/authorize")) | .value] | add // 0'
}

provider_create_forward_count() {
  fault_stats | jq '(.forwarded_by_path["POST /payments"] // 0)'
}

fault_summary_json() {
  fault_stats | jq '{
    dropped,
    responded,
    forwarded_by_path,
    responded_by_path,
    transition_history:[.transition_history[]? | {
      operation_id,outcome,provider_side_effect,upstream_status,status_path
    }],
    plan:{
      identity:(.plan.identity // null),
      consumed_request_steps:[.plan.consumed_request_steps[]? | .step_id],
      dispatched_timeline_steps:[.plan.dispatched_timeline_steps[]? | .step_id]
    }
  }'
}

provider_truth_json() {
  local provider_id="$1" base="${2:-$provider_base}"
  local output status
  output="$(mktemp)"
  status="$(curl --silent --show-error --output "$output" --write-out '%{http_code}' \
    "$base/payments/$provider_id")"
  if [[ "$status" == "200" ]]; then
    jq -c '{http_status:200,exists:true,id,currency,status,authorized_amount,captured_amount,refunded_amount}' "$output"
  elif [[ "$status" == "404" ]]; then
    jq -nc --arg id "$provider_id" '{http_status:404,exists:false,id:$id}'
  else
    jq -nc --arg id "$provider_id" --argjson status "$status" '{http_status:$status,exists:false,id:$id}'
  fi
  rm -f "$output"
}

run_json() {
  local run_id="$1"
  workload_sql "
    SELECT row_to_json(x)::text
    FROM (
      SELECT run_id, contract_version, oxide_batch_version,
             business_date::text, eligibility_cutoff::text, scenario_set_id,
             policy_version, candidate_set_digest, processing_generation, run_state
      FROM payment_recovery_workload.payment_recovery_run
      WHERE run_id = '$run_id'
    ) x
  "
}

candidate_json() {
  local run_id="$1"
  workload_sql "
    SELECT row_to_json(x)::text
    FROM (
      SELECT candidate_id, merchant_id, merchant_order_id, merchant_payment_id,
             operation_id, operation_kind, payment_state, operation_state,
             amount_minor, currency, idempotency_key_digest,
             payment_method_token_digest, provider_payment_id,
             gateway_operation_id, source_updated_at::text
      FROM payment_recovery_workload.payment_recovery_candidate
      WHERE run_id = '$run_id'
    ) x
  "
}

attempts_json() {
  local run_id="$1"
  workload_sql "
    SELECT COALESCE(json_agg(row_to_json(x) ORDER BY x.attempt_no), '[]'::json)::text
    FROM (
      SELECT attempt_no, processing_generation, attempt_state, merchant_http_status,
             canonical_payment_state, canonical_operation_state,
             canonical_provider_payment_id, canonical_gateway_operation_id,
             provider_visibility, provider_payment_id, provider_currency, provider_status,
             provider_authorized_amount_minor, provider_captured_amount_minor,
             provider_refunded_amount_minor, outcome_kind
      FROM payment_recovery_workload.payment_recovery_attempt
      WHERE run_id = '$run_id'
      ORDER BY attempt_no
    ) x
  "
}

result_json() {
  local run_id="$1"
  workload_sql "
    SELECT row_to_json(x)::text
    FROM (
      SELECT classification, duplicate_provider_mutation_count,
             final_canonical_payment_state, final_canonical_operation_state,
             final_provider_status, result_digest, oracle_violations
      FROM payment_recovery_workload.payment_recovery_result
      WHERE run_id = '$run_id'
    ) x
  "
}

result_classification() {
  local run_id="$1"
  workload_sql "SELECT classification FROM payment_recovery_workload.payment_recovery_result WHERE run_id = '$run_id'"
}

provider_id_for_run() {
  local run_id="$1"
  workload_sql "
    SELECT COALESCE(
      (SELECT canonical_provider_payment_id
       FROM payment_recovery_workload.payment_recovery_attempt
       WHERE run_id = '$run_id'
       ORDER BY attempt_no DESC LIMIT 1),
      (SELECT provider_payment_id
       FROM payment_recovery_workload.payment_recovery_candidate
       WHERE run_id = '$run_id')
    )
  "
}

