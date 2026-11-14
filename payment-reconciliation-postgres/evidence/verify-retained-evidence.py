#!/usr/bin/env python3
import argparse, copy, hashlib, json
from pathlib import Path

CLASSIFICATION = "supplemental exact-artifact v0.6.0 business/reference-system evidence"

def add(v, ok, msg):
    if not ok:
        v.append(msg)

def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

def row_digest(rows):
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["source_row_id"]):
        h.update(compact(row))
        h.update(b"\n")
    return h.hexdigest()

def canonical_detail(key, internal, provider):
    ic=len(internal); pc=len(provider)
    if ic==0: cls="MISSING_INTERNAL"
    elif pc==0: cls="MISSING_PROVIDER"
    elif ic>1: cls="DUPLICATE_INTERNAL"
    elif pc>1: cls="DUPLICATE_PROVIDER"
    else:
        i=internal[0]; p=provider[0]
        if any(i[f] != p[f] for f in ("authorized_amount_minor","captured_amount_minor","refunded_amount_minor")):
            cls="AMOUNT_MISMATCH"
        elif i["currency"] != p["currency"] or i["normalized_status"] != p["normalized_status"]:
            cls="ATTRIBUTE_MISMATCH"
        else: cls="MATCH"
    i=internal[0] if ic==1 else None
    p=provider[0] if pc==1 else None
    paired=i is not None and p is not None
    return {
        "provider_payment_id": key,
        "classification": cls,
        "internal_row_count": ic,
        "provider_row_count": pc,
        "internal_currency": i["currency"] if i else None,
        "provider_currency": p["currency"] if p else None,
        "internal_status": i["normalized_status"] if i else None,
        "provider_status": p["normalized_status"] if p else None,
        "authorized_delta_minor": p["authorized_amount_minor"]-i["authorized_amount_minor"] if paired else None,
        "captured_delta_minor": p["captured_amount_minor"]-i["captured_amount_minor"] if paired else None,
        "refunded_delta_minor": p["refunded_amount_minor"]-i["refunded_amount_minor"] if paired else None,
    }

def oracle(snapshot):
    ib={}; pb={}
    for r in snapshot["internal"]: ib.setdefault(r["provider_payment_id"],[]).append(r)
    for r in snapshot["provider"]: pb.setdefault(r["provider_payment_id"],[]).append(r)
    details=[canonical_detail(k,ib.get(k,[]),pb.get(k,[])) for k in sorted(set(ib)|set(pb))]
    mismatch=sum(d["classification"]!="MATCH" for d in details)
    isum=sum(r["authorized_amount_minor"]+r["captured_amount_minor"]+r["refunded_amount_minor"] for r in snapshot["internal"])
    psum=sum(r["authorized_amount_minor"]+r["captured_amount_minor"]+r["refunded_amount_minor"] for r in snapshot["provider"])
    delta=psum-isum
    state="CLEAR" if mismatch==0 and delta==0 else "HELD"
    h=hashlib.sha256()
    h.update(compact(details)); h.update(b"\n"); h.update(str(delta).encode()); h.update(b"\n"); h.update(state.encode())
    return {
        "business_state":state,
        "mismatch_count":mismatch,
        "aggregate_delta_minor":delta,
        "result_digest":h.hexdigest(),
        "details":details,
    }

def normalize_actual(actual):
    value=copy.deepcopy(actual)
    value["details"]=sorted(value.get("details") or [], key=lambda d:d["provider_payment_id"])
    return value

def apply_recipe(base, derived, v, label):
    recipe=derived.get("mutation_recipe") or {}
    kind=recipe.get("kind")
    expected=derived["metadata"]["scenario_case"]
    add(v,kind==expected,f"{label}: mutation recipe kind must equal scenario_case")
    work={"internal":copy.deepcopy(base["internal"]),"provider":copy.deepcopy(base["provider"])}
    target=recipe.get("target_provider_payment_id")
    if kind=="MISSING_PROVIDER":
        work["provider"]=[r for r in work["provider"] if r["provider_payment_id"]!=target]
    elif kind=="MISSING_INTERNAL":
        work["internal"]=[r for r in work["internal"] if r["provider_payment_id"]!=target]
    elif kind=="DUPLICATE_PROVIDER":
        rows=[r for r in work["provider"] if r["provider_payment_id"]==target]
        add(v,len(rows)==1,f"{label}: duplicate target must be unique in base")
        if rows:
            dup=copy.deepcopy(rows[0]); dup["source_row_id"] += "#duplicate-1"; work["provider"].append(dup)
    elif kind=="AMOUNT_MISMATCH":
        delta=recipe.get("delta_minor")
        add(v,isinstance(delta,int) and delta>0,f"{label}: amount delta must be positive")
        rows=[r for r in work["provider"] if r["provider_payment_id"]==target]
        add(v,len(rows)==1,f"{label}: amount mismatch target must be unique in base")
        if rows and isinstance(delta,int): rows[0]["authorized_amount_minor"] += delta
    else:
        add(v,False,f"{label}: unsupported mutation recipe {kind!r}")
    add(v,sorted(work["internal"],key=lambda r:r["source_row_id"])==sorted(derived["internal"],key=lambda r:r["source_row_id"]),f"{label}: derived internal rows do not match recipe")
    add(v,sorted(work["provider"],key=lambda r:r["source_row_id"])==sorted(derived["provider"],key=lambda r:r["source_row_id"]),f"{label}: derived provider rows do not match recipe")

def verify_artifact(path, record, v):
    try: e=json.loads(path.read_text())
    except Exception as exc:
        v.append(f"{path}: cannot parse: {exc}"); return
    label=record.get("scenario","unknown")
    add(v,e.get("schema_version")==1,f"{label}: schema_version")
    add(v,e.get("campaign_issue")==148,f"{label}: campaign issue")
    add(v,e.get("classification")==CLASSIFICATION,f"{label}: evidence classification")
    add(v,e.get("scenario")==label,f"{label}: scenario mismatch")
    add(v,e.get("failure_point")==record.get("failure_point"),f"{label}: failure_point manifest mismatch")
    report=e.get("producer_report") or {}
    add(v,report.get("schema_version")==1 and report.get("campaign_issue")==148,f"{label}: producer report identity")
    add(v,report.get("violations")==[],f"{label}: producer verifier reported violations")
    snap=report.get("snapshot") or {}; meta=snap.get("metadata") or {}
    internal=snap.get("internal") or []; provider=snap.get("provider") or []
    add(v,row_digest(internal)==meta.get("internal_snapshot_digest"),f"{label}: internal snapshot digest mismatch")
    add(v,row_digest(provider)==meta.get("provider_snapshot_digest"),f"{label}: provider snapshot digest mismatch")
    expected=oracle(snap)
    add(v,report.get("oracle")==expected,f"{label}: retained oracle does not match independent recomputation")
    actual=normalize_actual(report.get("actual") or {})
    add(v,actual.get("processing_state")=="PROCESSED",f"{label}: processing must be PROCESSED")
    add(v,actual.get("business_state")==expected["business_state"],f"{label}: business state mismatch")
    add(v,actual.get("mismatch_count")==expected["mismatch_count"],f"{label}: mismatch count")
    add(v,actual.get("aggregate_delta_minor")==expected["aggregate_delta_minor"],f"{label}: aggregate delta")
    add(v,actual.get("result_digest")==expected["result_digest"],f"{label}: result digest")
    add(v,actual.get("details")==expected["details"],f"{label}: detail mismatch")
    add(v,actual.get("published_count")==(1 if expected["business_state"]=="CLEAR" else 0),f"{label}: publication cardinality")
    base=report.get("base_snapshot")
    if meta.get("base_snapshot_id") is not None:
        add(v,isinstance(base,dict),f"{label}: derived scenario must retain base snapshot")
        if isinstance(base,dict):
            add(v,base.get("metadata",{}).get("snapshot_id")==meta.get("base_snapshot_id"),f"{label}: base snapshot identity")
            add(v,row_digest(base.get("internal") or [])==base.get("metadata",{}).get("internal_snapshot_digest"),f"{label}: base internal digest")
            add(v,row_digest(base.get("provider") or [])==base.get("metadata",{}).get("provider_snapshot_digest"),f"{label}: base provider digest")
            apply_recipe(base,snap,v,label)
    else:
        add(v,base is None,f"{label}: base snapshot must be null for root scenario")
    o=e.get("observations") or {}
    fp=e.get("failure_point")
    if fp:
        add(v,fp.get("signal")==9,f"{label}: hard crash must record SIGKILL")
    expectations={
      "hard-crash-during-write":{"detail_count_after_kill":2,"plain_restart_rejected":True,"explicit_public_recovery":True,"new_process_continuation":True,"final_publication_count":1},
      "hard-crash-after-chunk-commit":{"detail_count_after_kill":4,"plain_restart_rejected":True,"explicit_public_recovery":True,"new_process_continuation":True,"final_publication_count":1},
      "hard-crash-after-processing-commit":{"publication_count_after_kill":0,"business_state_after_kill":None,"separate_process_finalization":True,"final_publication_count":1},
      "hard-crash-before-finalize-commit":{"publication_count_after_kill":0,"business_state_after_kill":None,"finalize_replay":True,"final_publication_count":1},
      "hard-crash-after-publication-commit":{"publication_count_after_kill":1,"business_state_after_kill":"CLEAR","finalize_replay":True,"final_publication_count":1},
      "clear-finalize-replay":{"finalize_replays":2,"final_publication_count":1},
      "held-finalize-replay":{"finalize_replays":2,"final_publication_count":0},
    }
    if label in expectations:
        for k,x in expectations[label].items(): add(v,o.get(k)==x,f"{label}: observation {k}")
    if label.startswith("business-"):
        case={"business-clear":"CLEAR","business-missing-provider":"MISSING_PROVIDER","business-missing-internal":"MISSING_INTERNAL","business-duplicate-provider":"DUPLICATE_PROVIDER","business-amount-mismatch":"AMOUNT_MISMATCH"}[label]
        add(v,meta.get("scenario_case")==case,f"{label}: scenario_case")

def verify_manifest(manifest_path):
    v=[]
    try:m=json.loads(Path(manifest_path).read_text())
    except Exception as exc:return [f"cannot read manifest: {exc}"]
    records=m.get("records") or []
    add(v,len(records)==12,"expected exactly 12 retained campaign records")
    names={r.get("scenario") for r in records}
    expected={
      "business-clear","business-missing-provider","business-missing-internal","business-duplicate-provider","business-amount-mismatch",
      "hard-crash-during-write","hard-crash-after-chunk-commit","hard-crash-after-processing-commit",
      "hard-crash-before-finalize-commit","hard-crash-after-publication-commit","clear-finalize-replay","held-finalize-replay"
    }
    add(v,names==expected,"retained scenario set mismatch")
    for r in records:
        p=Path((r.get("artifact") or {}).get("path",""))
        verify_artifact(p,r,v)
    return v

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--manifest",required=True); a=ap.parse_args()
    violations=verify_manifest(a.manifest)
    print(json.dumps({"schema_version":1,"violations":violations},sort_keys=True))
    raise SystemExit(bool(violations))

if __name__=="__main__": main()
