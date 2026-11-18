#!/usr/bin/env python3
import argparse, hashlib, json
from pathlib import Path

ISSUE=271
CONTRACT="payment-recovery-v1"
SUBJECT="oxide-batch 0.7.0"
EXPECTED={
  "crash-before-replay",
  "crash-request-outcome-unknown",
  "crash-after-external-convergence",
  "crash-after-local-write",
  "pre-side-effect-transient",
  "stale-visibility-convergence",
  "not-found-visibility-convergence",
  "async-failure-convergence",
  "identity-conflict-hold",
}
CRASH_SCENARIOS={
  "crash-before-replay",
  "crash-request-outcome-unknown",
  "crash-after-external-convergence",
  "crash-after-local-write",
}
FORBIDDEN_KEYS={"idempotency_key","payment_method_token"}


def add(v, ok, msg):
    if not ok: v.append(msg)

def hupdate(h, value):
    h.update(str(value).encode()); h.update(b"\0")

def digest(candidate, attempt, result):
    h=hashlib.sha256()
    for value in [
      candidate.get("candidate_id",""), candidate.get("merchant_payment_id",""),
      candidate.get("operation_id",""), candidate.get("operation_kind",""),
      candidate.get("idempotency_key_digest",""), candidate.get("currency",""),
      attempt.get("canonical_payment_state") or "", attempt.get("canonical_operation_state") or "",
      attempt.get("canonical_provider_payment_id") or "", attempt.get("canonical_gateway_operation_id") or "",
      attempt.get("provider_visibility") or "", attempt.get("provider_payment_id") or "",
      attempt.get("provider_currency") or "", attempt.get("provider_status") or "",
      result.get("classification",""), attempt.get("outcome_kind",""),
    ]: hupdate(h,value)
    h.update(int(attempt["attempt_no"]).to_bytes(8,"big",signed=True))
    h.update(int(attempt["processing_generation"]).to_bytes(8,"big",signed=True))
    h.update(int(candidate["amount_minor"]).to_bytes(8,"big",signed=True))
    for key in ["provider_authorized_amount_minor","provider_captured_amount_minor","provider_refunded_amount_minor"]:
        value=attempt.get(key)
        if value is None: h.update(b"\x00")
        else:
            h.update(b"\x01"); h.update(int(value).to_bytes(8,"big",signed=True))
    return h.hexdigest()

def classify(candidate, attempt, duplicate_count):
    violations=[]
    if attempt.get("canonical_gateway_operation_id") not in (None,candidate.get("operation_id")):
        violations.append("gateway operation identity mismatch")
    cp=attempt.get("canonical_provider_payment_id")
    pp=attempt.get("provider_payment_id")
    expected=candidate.get("provider_payment_id")
    if expected and cp and expected!=cp: violations.append("canonical provider identity mismatch")
    if cp and pp and cp!=pp: violations.append("provider GET identity mismatch")
    if attempt.get("provider_currency") not in (None,candidate.get("currency")):
        violations.append("provider currency mismatch")
    if duplicate_count:
        violations.append("duplicate provider mutation observed")
    if violations: return "HOLD_MANUAL"

    op=attempt.get("canonical_operation_state")
    vis=attempt.get("provider_visibility")
    if op=="MANUAL_REVIEW": return "HOLD_MANUAL"
    if vis in {"NOT_FOUND","STALE","UNAVAILABLE"}: return "RETRY_LATER"
    if op in {"PENDING","UNKNOWN","RETRY_PENDING"}: return "RETRY_LATER"
    if op=="SUCCEEDED":
        kind=candidate.get("operation_kind")
        expected_payment="AUTHORIZED" if kind=="AUTHORIZE" else "PAID"
        expected_status="authorized" if kind=="AUTHORIZE" else "captured"
        if attempt.get("canonical_payment_state")!=expected_payment: return "HOLD_MANUAL"
        if not cp or attempt.get("canonical_gateway_operation_id")!=candidate.get("operation_id"): return "HOLD_MANUAL"
        if attempt.get("provider_status")!=expected_status: return "HOLD_MANUAL"
        if attempt.get("provider_authorized_amount_minor")!=candidate.get("amount_minor"): return "HOLD_MANUAL"
        if kind=="AUTH_CAPTURE" and attempt.get("provider_captured_amount_minor")!=candidate.get("amount_minor"):
            return "HOLD_MANUAL"
        return "RESOLVED_SUCCEEDED"
    if op=="FAILED":
        if attempt.get("canonical_payment_state")!="PAYMENT_FAILED": return "HOLD_MANUAL"
        status=attempt.get("provider_status")
        amounts=[attempt.get(k) or 0 for k in ["provider_authorized_amount_minor","provider_captured_amount_minor","provider_refunded_amount_minor"]]
        if (pp is None and status is None) or (status=="failed" and amounts==[0,0,0]):
            return "RESOLVED_FAILED"
        return "HOLD_MANUAL"
    return "HOLD_MANUAL"

def secret_scan(value, path="$"):
    violations=[]
    if isinstance(value,dict):
        for k,v in value.items():
            if k in FORBIDDEN_KEYS: violations.append(f"{path}: forbidden raw secret field {k}")
            violations += secret_scan(v,f"{path}.{k}")
    elif isinstance(value,list):
        for i,v in enumerate(value): violations += secret_scan(v,f"{path}[{i}]")
    elif isinstance(value,str) and (value.startswith("idem-") or value.startswith("tok-")):
        violations.append(f"{path}: raw replay secret leaked")
    return violations

def phase(artifact,name):
    return next((p for p in artifact.get("phases",[]) if p.get("name")==name),{})

def verify_artifact(path, record, violations):
    try: a=json.loads(path.read_text())
    except Exception as exc:
        violations.append(f"{path}: cannot parse: {exc}"); return
    label=record.get("scenario","unknown")
    add(violations,a.get("schema_version")==1,f"{label}: schema_version")
    add(violations,a.get("campaign_issue")==ISSUE,f"{label}: campaign issue")
    add(violations,a.get("contract_version")==CONTRACT,f"{label}: contract version")
    add(violations,a.get("validation_subject")==SUBJECT,f"{label}: validation subject")
    add(violations,a.get("scenario")==label,f"{label}: scenario mismatch")
    add(violations,a.get("failure_point")==record.get("failure_point"),f"{label}: failure point mismatch")
    violations += [f"{label}: {x}" for x in secret_scan(a)]

    candidate=a.get("candidate") or {}
    add(violations,len(candidate.get("idempotency_key_digest",''))==64,f"{label}: idempotency digest")
    add(violations,len(candidate.get("payment_method_token_digest",''))==64,f"{label}: token digest")
    obs=a.get("observations") or {}
    attempts=obs.get("attempts") or []
    result=obs.get("result") or {}
    add(violations,bool(attempts),f"{label}: no attempts retained")
    if attempts:
        latest=attempts[-1]
        recomputed=classify(candidate,latest,int(result.get("duplicate_provider_mutation_count",0)))
        add(violations,result.get("classification")==recomputed,f"{label}: classification recomputation")
        add(violations,result.get("result_digest")==digest(candidate,latest,result),f"{label}: result digest recomputation")
        oracle_violations=result.get("oracle_violations") or []
        if label=="identity-conflict-hold":
            add(violations,
                any("provider GET identity mismatch" in str(item) for item in oracle_violations),
                f"{label}: expected identity violation not retained")
        else:
            add(violations,oracle_violations==[],f"{label}: unexpected oracle violations")

    side=a.get("side_effects") or {}
    duplicate=int(side.get("duplicate_provider_mutation_count",0))
    add(violations,duplicate==0,f"{label}: duplicate provider mutation count")
    add(violations,int(result.get("duplicate_provider_mutation_count",0))==0,f"{label}: persisted duplicate provider mutation count")

    if label in CRASH_SCENARIOS:
        add(violations,(a.get("failure_point") or {}).get("signal")==9,f"{label}: crash signal")
        killed=phase(a,"after-kill"); recovered=phase(a,"after-recover"); terminal=phase(a,"terminal-replay")
        add(violations,killed.get("run_state")=="PROCESSING",f"{label}: state after kill")
        add(violations,killed.get("attempt_count")==0,f"{label}: uncommitted attempt leaked")
        add(violations,recovered.get("processing_generation")==1 and recovered.get("launched") is True,f"{label}: same-generation recovery")
        add(violations,result.get("classification")=="RESOLVED_SUCCEEDED",f"{label}: recovery classification")
        add(violations,side.get("provider_mutation_forward_count")==1,f"{label}: provider mutation cardinality")
        add(violations,terminal.get("launched") is False,f"{label}: terminal replay must not launch")
    elif label=="pre-side-effect-transient":
        g1=phase(a,"generation-1"); g2=phase(a,"generation-2")
        add(violations,(g1.get("result") or {}).get("classification")=="RETRY_LATER",f"{label}: generation 1 must remain retryable")
        add(violations,g1.get("processing_generation")==1 and g2.get("processing_generation")==2,f"{label}: generation advance")
        add(violations,side.get("provider_mutation_forward_count",0)<=1,f"{label}: more than one legitimate provider mutation")
    elif label in {"stale-visibility-convergence","not-found-visibility-convergence"}:
        g1=phase(a,"generation-1"); g2=phase(a,"generation-2")
        add(violations,(g1.get("result") or {}).get("classification")=="RETRY_LATER",f"{label}: first visibility must remain unresolved")
        add(violations,(g2.get("result") or {}).get("classification")=="RESOLVED_SUCCEEDED",f"{label}: later authoritative visibility must converge")
        add(violations,side.get("provider_mutation_forward_count")==1,f"{label}: provider mutation duplicated")
    elif label=="async-failure-convergence":
        add(violations,result.get("classification")=="RESOLVED_FAILED",f"{label}: terminal failure classification")
        add(violations,side.get("successful_provider_effect_count")==0,f"{label}: successful provider effect invented")
        add(violations,side.get("provider_transition_effect") is False,f"{label}: failed transition must be side-effect free")
    elif label=="identity-conflict-hold":
        add(violations,result.get("classification")=="HOLD_MANUAL",f"{label}: identity conflict must hold")
        add(violations,side.get("provider_mutation_forward_count")==1,f"{label}: identity observation changed provider mutation count")
        add(violations,phase(a,"terminal-replay").get("launched") is False,f"{label}: HOLD_MANUAL must not auto replay")

def verify_manifest(path):
    violations=[]
    try:m=json.loads(Path(path).read_text())
    except Exception as exc:return [f"cannot read manifest: {exc}"]
    records=m.get("records") or []
    names={r.get("scenario") for r in records}
    add(violations,names==EXPECTED,"retained scenario set mismatch")
    add(violations,len(records)==len(EXPECTED),"retained record count mismatch")
    root=Path(path).resolve().parent.parent
    for r in records:
        artifact=root/(r.get("artifact") or {}).get("path","")
        verify_artifact(artifact,r,violations)
    return violations

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--manifest",required=True); args=ap.parse_args()
    v=verify_manifest(args.manifest)
    print(json.dumps({"schema_version":1,"violations":v},sort_keys=True))
    raise SystemExit(bool(v))

if __name__=="__main__": main()
