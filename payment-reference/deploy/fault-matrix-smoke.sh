#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"

[ "$#" -ge 2 ] || { echo "usage: fault-matrix-smoke.sh <fault-proxy-base> <output-path> [logical-business-time]" >&2; exit 2; }
fault_proxy_base="$1"
output_path="$2"
if [ "$#" -ge 3 ]; then logical_business_time="$3"; else logical_business_time="2026-11-14T00:00:00Z"; fi
smoke_namespace="${PAYMENT_REFERENCE_SMOKE_NAMESPACE:-}"
case "$smoke_namespace" in
  "" ) ;;
  *[!A-Za-z0-9._-]* )
    echo "PAYMENT_REFERENCE_SMOKE_NAMESPACE must contain only [A-Za-z0-9._-]" >&2
    exit 64
    ;;
esac
[ "${#smoke_namespace}" -le 32 ] || {
  echo "PAYMENT_REFERENCE_SMOKE_NAMESPACE must be at most 32 characters" >&2
  exit 64
}

qualified_id() {
  printf '%s%s' "$1" "$smoke_namespace"
}

fail() {
  echo "payment-reference fault matrix failure: $*" >&2
  return 1
}

request_json() {
  local method="$1"
  local path="$2"
  local body="$3"
  local expected_status="$4"
  local output="$5"
  local status
  status="$(curl --silent --show-error --output "$output" --write-out '%{http_code}' --request "$method" --header 'Content-Type: application/json' --data "$body" "$fault_proxy_base$path")"
  if [ "$status" != "$expected_status" ]; then
    cat "$output" >&2 || true
    fail "$method $path: expected HTTP $expected_status, observed $status"
  fi
}

provider_create() {
  local provider_payment_id="$1"
  local amount_minor="$2"
  local output="$3"
  local body
  local status
  body="$(jq -nc --arg id "$provider_payment_id" --argjson amount "$amount_minor" '{id:$id,amount:$amount,currency:"USD"}')"
  status="$(curl --silent --show-error --output "$output" --write-out '%{http_code}' --request POST --header 'Accept: application/json' --header "Idempotency-Key: create-$provider_payment_id" --header "X-Correlation-ID: fault-matrix-create-$provider_payment_id" --header 'Content-Type: application/json' --data "$body" "$fault_proxy_base/payments")"
  if [ "$status" != "201" ]; then
    cat "$output" >&2 || true
    fail "provider create $provider_payment_id: expected HTTP 201, observed $status"
  fi
  jq -e --arg id "$provider_payment_id" '.id == $id and .status == "pending"' "$output" >/dev/null
}

provider_authorize() {
  local provider_payment_id="$1"
  local expected_status="$2"
  local output="$3"
  local status
  status="$(curl --silent --show-error --output "$output" --write-out '%{http_code}' --request POST --header 'Accept: application/json' --header "Idempotency-Key: authorize-$provider_payment_id" --header "X-Correlation-ID: fault-matrix-authorize-$provider_payment_id" --data '' "$fault_proxy_base/payments/$provider_payment_id/authorize")"
  if [ "$status" != "$expected_status" ]; then
    cat "$output" >&2 || true
    fail "provider authorize $provider_payment_id: expected HTTP $expected_status, observed $status"
  fi
}

provider_get() {
  local provider_payment_id="$1"
  local expected_status="$2"
  local output="$3"
  local status
  status="$(curl --silent --show-error --output "$output" --write-out '%{http_code}' --header 'Accept: application/json' --header "X-Correlation-ID: fault-matrix-get-$provider_payment_id" "$fault_proxy_base/payments/$provider_payment_id")"
  if [ "$status" != "$expected_status" ]; then
    cat "$output" >&2 || true
    fail "provider GET $provider_payment_id: expected HTTP $expected_status, observed $status"
  fi
}

reset_faults() {
  request_json POST /__fault/reset '{}' 200 /tmp/pr-fault-reset.json
}

load_plan() {
  request_json POST /__fault/load-plan "$1" 200 /tmp/pr-fault-load.json
}

advance_to() {
  local body
  body="$(jq -nc --argjson offset_ms "$1" '{offset_ms:$offset_ms}')"
  request_json POST /__fault/advance "$body" 200 "$2"
}

stats() {
  local output="$1"
  local status
  status="$(curl --silent --show-error --output "$output" --write-out '%{http_code}' "$fault_proxy_base/__fault/stats")"
  [ "$status" = "200" ] || fail "GET /__fault/stats: expected HTTP 200, observed $status"
}

mkdir -p "$(dirname "$output_path")"
fault_catalog_cli="$here/longrun/campaign_fault_catalog.py"
python3 "$fault_catalog_cli" validate >/dev/null

pre_id="$(qualified_id provider-fault-plan-503)"
reset_faults
provider_create "$pre_id" 503 /tmp/pr-fault-pre-create.json
reset_faults
pre_plan="$(python3 "$fault_catalog_cli" render --fault-class plan-503-before-side-effect --payment-id "$pre_id" --logical-business-time "$logical_business_time")"
load_plan "$pre_plan"
advance_to 0 /tmp/pr-fault-pre-advance.json
provider_authorize "$pre_id" 503 /tmp/pr-fault-pre-response.json
provider_get "$pre_id" 200 /tmp/pr-fault-pre-provider.json
stats /tmp/pr-fault-pre-stats.json
jq -e --arg key "POST /payments/$pre_id/authorize" '(.plan.consumed_request_steps | map(.step_id)) == ["provider-503"] and ((.forwarded_by_path[$key] // 0) == 0)' /tmp/pr-fault-pre-stats.json >/dev/null
jq -e '.status == "pending"' /tmp/pr-fault-pre-provider.json >/dev/null

drop_id="$(qualified_id provider-fault-plan-drop)"
reset_faults
provider_create "$drop_id" 701 /tmp/pr-fault-drop-create.json
reset_faults
drop_plan="$(python3 "$fault_catalog_cli" render --fault-class post-commit-response-loss --payment-id "$drop_id" --logical-business-time "$logical_business_time")"
load_plan "$drop_plan"
advance_to 0 /tmp/pr-fault-drop-advance.json
if curl --silent --show-error --max-time 5 --output /tmp/pr-fault-drop-response.json --request POST --header 'Accept: application/json' --header "Idempotency-Key: authorize-$drop_id" --header "X-Correlation-ID: fault-matrix-authorize-$drop_id" --data '' "$fault_proxy_base/payments/$drop_id/authorize"; then
  fail "post-commit response-loss scenario unexpectedly returned a response"
fi
provider_get "$drop_id" 200 /tmp/pr-fault-drop-provider.json
stats /tmp/pr-fault-drop-stats.json
jq -e --arg key "POST /payments/$drop_id/authorize" '(.plan.consumed_request_steps | map(.step_id)) == ["commit-drop"] and ((.forwarded_by_path[$key] // 0) == 1)' /tmp/pr-fault-drop-stats.json >/dev/null
jq -e '.status == "authorized"' /tmp/pr-fault-drop-provider.json >/dev/null

async_success_id="$(qualified_id provider-fault-async-success)"
reset_faults
provider_create "$async_success_id" 811 /tmp/pr-fault-async-success-create.json
reset_faults
async_success_plan="$(python3 "$fault_catalog_cli" render --fault-class async-success-stale-convergence --payment-id "$async_success_id" --logical-business-time "$logical_business_time")"
load_plan "$async_success_plan"
advance_to 0 /tmp/pr-fault-async-success-0.json
provider_authorize "$async_success_id" 200 /tmp/pr-fault-async-success-accepted.json
jq -e '.status == "pending"' /tmp/pr-fault-async-success-accepted.json >/dev/null
advance_to 50 /tmp/pr-fault-async-success-50.json
advance_to 100 /tmp/pr-fault-async-success-100.json
provider_get "$async_success_id" 200 /tmp/pr-fault-async-success-stale.json
jq -e '.status == "pending"' /tmp/pr-fault-async-success-stale.json >/dev/null
advance_to 200 /tmp/pr-fault-async-success-200.json
provider_get "$async_success_id" 200 /tmp/pr-fault-async-success-current.json
jq -e '.status == "authorized"' /tmp/pr-fault-async-success-current.json >/dev/null
stats /tmp/pr-fault-async-success-stats.json
jq -e --arg key "POST /payments/$async_success_id/authorize" '((.forwarded_by_path[$key] // 0) == 1) and (.transition_history | length == 1) and .transition_history[0].outcome == "SUCCEEDED" and .transition_history[0].provider_side_effect == true and (.plan.consumed_request_steps | map(.step_id)) == ["defer-authorize"] and (.plan.dispatched_timeline_steps | map(.step_id)) == ["stale-status","authorize-success","status-converges"]' /tmp/pr-fault-async-success-stats.json >/dev/null

async_failure_id="$(qualified_id provider-fault-async-failure)"
reset_faults
provider_create "$async_failure_id" 812 /tmp/pr-fault-async-failure-create.json
reset_faults
async_failure_plan="$(python3 "$fault_catalog_cli" render --fault-class async-failure --payment-id "$async_failure_id" --logical-business-time "$logical_business_time")"
load_plan "$async_failure_plan"
advance_to 0 /tmp/pr-fault-async-failure-0.json
provider_authorize "$async_failure_id" 200 /tmp/pr-fault-async-failure-accepted.json
jq -e '.status == "pending"' /tmp/pr-fault-async-failure-accepted.json >/dev/null
advance_to 100 /tmp/pr-fault-async-failure-100.json
provider_get "$async_failure_id" 200 /tmp/pr-fault-async-failure-current.json
jq -e '.status == "failed"' /tmp/pr-fault-async-failure-current.json >/dev/null
stats /tmp/pr-fault-async-failure-stats.json
jq -e --arg key "POST /payments/$async_failure_id/authorize" '((.forwarded_by_path[$key] // 0) == 0) and (.transition_history | length == 1) and .transition_history[0].outcome == "FAILED" and .transition_history[0].provider_side_effect == false and (.plan.consumed_request_steps | map(.step_id)) == ["defer-authorize"] and (.plan.dispatched_timeline_steps | map(.step_id)) == ["authorize-failed"]' /tmp/pr-fault-async-failure-stats.json >/dev/null

reset_faults
webhook_plan="$(python3 "$fault_catalog_cli" render --fault-class webhook-ordering-duplicate-drop-retry --logical-business-time "$logical_business_time")"
load_plan "$webhook_plan"
request_json POST /__fault/webhook-events '{"event_id":"event-created","payload":{"type":"payment.created"}}' 201 /tmp/pr-fault-webhook-created.json
request_json POST /__fault/webhook-events '{"event_id":"event-authorized","payload":{"type":"payment.authorized"}}' 201 /tmp/pr-fault-webhook-authorized.json
advance_to 99 /tmp/pr-fault-webhook-99.json
advance_to 100 /tmp/pr-fault-webhook-100.json
advance_to 500 /tmp/pr-fault-webhook-500.json
stats /tmp/pr-fault-webhook-stats.json
jq -e '(.webhook.delivered_envelopes | map(.event_id)) == ["event-authorized","event-created","event-authorized","event-created"] and (.webhook.attempts | map(.outcome)) == ["DELIVERED","DELIVERED","DELIVERED","DROPPED","DELIVERED"] and (.plan.dispatched_timeline_steps | map(.step_id)) == ["authorized-first","created-late","authorized-duplicate","created-drop","created-retry"]' /tmp/pr-fault-webhook-stats.json >/dev/null

jq -n --arg pre_id "$pre_id" --arg drop_id "$drop_id" --arg async_success_id "$async_success_id" --arg async_failure_id "$async_failure_id" --slurpfile pre /tmp/pr-fault-pre-stats.json --slurpfile drop /tmp/pr-fault-drop-stats.json --slurpfile async_success /tmp/pr-fault-async-success-stats.json --slurpfile async_failure /tmp/pr-fault-async-failure-stats.json --slurpfile webhook /tmp/pr-fault-webhook-stats.json --slurpfile pre_provider /tmp/pr-fault-pre-provider.json --slurpfile drop_create /tmp/pr-fault-drop-create.json --slurpfile drop_provider /tmp/pr-fault-drop-provider.json --slurpfile async_success_accepted /tmp/pr-fault-async-success-accepted.json --slurpfile async_success_stale /tmp/pr-fault-async-success-stale.json --slurpfile async_success_current /tmp/pr-fault-async-success-current.json --slurpfile async_failure_accepted /tmp/pr-fault-async-failure-accepted.json --slurpfile async_failure_current /tmp/pr-fault-async-failure-current.json '
  def step_ids($steps): [$steps[]?.step_id];
  def base_case($stats; $provider_id; $caller; $forward_count; $side_effect; $states; $inquiries):
    {
      scenario_case: $stats.plan.identity.scenario_case,
      plan_identity: $stats.plan.identity,
      provider_payment_id: $provider_id,
      caller_outcome: $caller,
      target_forward_count: $forward_count,
      provider_side_effect_observed: $side_effect,
      provider_state_sequence: $states,
      inquiry_sequence: $inquiries,
      consumed_request_steps: step_ids($stats.plan.consumed_request_steps),
      dispatched_timeline_steps: step_ids($stats.plan.dispatched_timeline_steps),
      webhook_attempts: [],
      unexpected_invariant_violations: []
    };
  {
    fault_plan_version: 1,
    cases: [
      base_case($pre[0]; $pre_id; "HTTP_503"; 0; false; [$pre_provider[0].status]; [{offset_ms:0,visibility:"IMMEDIATE",observed_status:$pre_provider[0].status}]),
      base_case($drop[0]; $drop_id; "CONNECTION_DROP"; 1; true; [$drop_create[0].status,$drop_provider[0].status]; [{offset_ms:0,visibility:"IMMEDIATE",observed_status:$drop_provider[0].status}]),
      base_case($async_success[0]; $async_success_id; "HTTP_200_PENDING_ACCEPTED"; 1; true; [$async_success_accepted[0].status,$async_success_stale[0].status,$async_success_current[0].status]; [{offset_ms:100,visibility:"STALE",observed_status:$async_success_stale[0].status},{offset_ms:200,visibility:"IMMEDIATE",observed_status:$async_success_current[0].status}]),
      base_case($async_failure[0]; $async_failure_id; "HTTP_200_PENDING_ACCEPTED"; 0; false; [$async_failure_accepted[0].status,$async_failure_current[0].status]; [{offset_ms:100,visibility:"OVERLAY",observed_status:$async_failure_current[0].status}]),
      (base_case($webhook[0]; null; "WEBHOOK_SCHEDULE_REPLAYED"; 0; false; []; []) | .webhook_attempts = [$webhook[0].webhook.attempts[] | {event_id,attempt,outcome,dispatched_at_ms}])
    ],
    unexpected_invariant_violations: []
  }
' >"$output_path"

jq -e '.fault_plan_version == 1 and (.cases | length == 5) and ([.cases[].unexpected_invariant_violations | length] | add) == 0 and (.unexpected_invariant_violations | length) == 0 and (.cases | map(.scenario_case)) == ["fault-plan-pre-side-effect-503","fault-plan-post-commit-response-loss","fault-plan-async-success-stale-convergence","fault-plan-async-failure","fault-plan-webhook-delivery"]' "$output_path" >/dev/null
