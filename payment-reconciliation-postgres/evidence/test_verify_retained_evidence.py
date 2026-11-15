#!/usr/bin/env python3
import copy, importlib.util, json, tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("verifier",HERE/"verify-retained-evidence.py")
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

def row(i=0):
    return {"source_row_id":f"internal:{i}","merchant_payment_id":f"m:{i}","provider_payment_id":f"p:{i}","currency":"USD","normalized_status":"CAPTURED","authorized_amount_minor":100,"captured_amount_minor":100,"refunded_amount_minor":0}

def provider(i=0):
    return {"source_row_id":f"provider:{i}","provider_payment_id":f"p:{i}","currency":"USD","normalized_status":"CAPTURED","authorized_amount_minor":100,"captured_amount_minor":100,"refunded_amount_minor":0,"provider_version":1}

def fixture():
    internal=[row()]; prov=[provider()]
    snap={"metadata":{"snapshot_id":"s","business_date":"2026-11-14","provider_scope":"merchant","scenario_case":"CLEAR","base_snapshot_id":None,"internal_snapshot_digest":mod.row_digest(internal),"provider_snapshot_digest":mod.row_digest(prov),"policy_version":"payment-reconciliation-policy-v1"},"internal":internal,"provider":prov,"mutation_recipe":{}}
    oracle=mod.oracle(snap)
    actual={"run_id":"r","processing_state":"PROCESSED","business_state":"CLEAR","mismatch_count":0,"aggregate_delta_minor":0,"result_digest":oracle["result_digest"],"details":oracle["details"],"published_count":1}
    return {"schema_version":1,"campaign_issue":148,"classification":mod.CLASSIFICATION,"scenario":"business-clear","source_kind":"test","failure_point":None,"observations":{},"producer_report":{"schema_version":1,"campaign_issue":148,"snapshot":snap,"base_snapshot":None,"oracle":oracle,"actual":actual,"violations":[]}}

def violations_for(value):
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); (root/"validation").mkdir()
        artifact=root/"validation/business-clear.json"; artifact.write_text(json.dumps(value))
        record={"scenario":"business-clear","artifact":{"path":"validation/business-clear.json"},"failure_point":None}
        old=Path.cwd()
        try:
            import os; os.chdir(root)
            v=[]; mod.verify_artifact(Path("validation/business-clear.json"),record,v); return v
        finally: os.chdir(old)

clean=fixture()
assert violations_for(clean)==[], violations_for(clean)
tampered=copy.deepcopy(clean)
tampered["producer_report"]["snapshot"]["internal"][0]["captured_amount_minor"]+=1
assert any("internal snapshot digest mismatch" in x for x in violations_for(tampered))
tampered_result=copy.deepcopy(clean)
tampered_result["producer_report"]["actual"]["published_count"]=0
assert any("publication cardinality" in x for x in violations_for(tampered_result))
print("retained evidence negative corruption controls PASS")
