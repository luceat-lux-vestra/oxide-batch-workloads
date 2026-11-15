#!/usr/bin/env python3
import copy, importlib.util, json, tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("verifier",HERE/"verify-retained-evidence.py")
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

def fixture():
    candidate={
      "candidate_id":"candidate-1","merchant_payment_id":"payment-1","merchant_order_id":"order-1",
      "operation_id":"operation-1","operation_kind":"AUTHORIZE","amount_minor":100,"currency":"USD",
      "idempotency_key_digest":"a"*64,"payment_method_token_digest":"b"*64,
      "provider_payment_id":"provider-1","gateway_operation_id":"operation-1"
    }
    attempt={
      "attempt_no":1,"processing_generation":1,"attempt_state":"OBSERVED","merchant_http_status":200,
      "canonical_payment_state":"AUTHORIZED","canonical_operation_state":"SUCCEEDED",
      "canonical_provider_payment_id":"provider-1","canonical_gateway_operation_id":"operation-1",
      "provider_visibility":"IMMEDIATE","provider_payment_id":"provider-1","provider_currency":"USD",
      "provider_status":"authorized","provider_authorized_amount_minor":100,"provider_captured_amount_minor":0,
      "provider_refunded_amount_minor":0,"outcome_kind":"MERCHANT_CANONICAL_RESPONSE"
    }
    result={
      "classification":"RESOLVED_SUCCEEDED","duplicate_provider_mutation_count":0,
      "final_canonical_payment_state":"AUTHORIZED","final_canonical_operation_state":"SUCCEEDED",
      "final_provider_status":"authorized","oracle_violations":[]
    }
    result["result_digest"]=mod.digest(candidate,attempt,result)
    return {
      "schema_version":1,"campaign_issue":271,"contract_version":"payment-recovery-v1",
      "validation_subject":"oxide-batch 0.7.0","scenario":"crash-before-replay","scenario_kind":"COMMIT_RESPONSE_LOSS",
      "failure_point":{"boundary":"after-candidate-read-before-replay","signal":9},
      "candidate":candidate,
      "observations":{"run":{"run_id":"run-1","run_state":"PROCESSED","processing_generation":1},"attempts":[attempt],"result":result,"provider_truth":{"http_status":200,"status":"authorized"},"fault":{}},
      "side_effects":{"provider_mutation_forward_count":1,"duplicate_provider_mutation_count":0,"successful_provider_effect_count":1},
      "phases":[
        {"name":"after-kill","run_state":"PROCESSING","attempt_count":0},
        {"name":"after-recover","processing_generation":1,"launched":True},
        {"name":"terminal-replay","processing_generation":1,"launched":False}
      ]
    }

def violations_for(value):
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/"artifact.json"; p.write_text(json.dumps(value))
        record={"scenario":"crash-before-replay","failure_point":value["failure_point"]}
        v=[]; mod.verify_artifact(p,record,v); return v

clean=fixture()
assert violations_for(clean)==[], violations_for(clean)

tampered=copy.deepcopy(clean)
tampered["observations"]["attempts"][0]["provider_authorized_amount_minor"]=99
assert any("classification recomputation" in x or "result digest recomputation" in x for x in violations_for(tampered))

tampered_effect=copy.deepcopy(clean)
tampered_effect["side_effects"]["provider_mutation_forward_count"]=2
assert any("provider mutation cardinality" in x for x in violations_for(tampered_effect))

secret=copy.deepcopy(clean)
secret["candidate"]["idempotency_key"]="idem-leak"
assert any("forbidden raw secret field" in x for x in violations_for(secret))

print("retained evidence negative corruption controls PASS")
