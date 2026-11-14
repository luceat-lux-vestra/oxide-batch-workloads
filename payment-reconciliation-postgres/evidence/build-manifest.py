#!/usr/bin/env python3
import argparse,hashlib,json,os,subprocess
from pathlib import Path

WORKLOAD="payment-reconciliation-postgres"
INCLUDES=["Cargo.toml","Cargo.lock","src","migrations","tests/recovery_matrix.rs","docker-compose.yml","ci/validate","evidence"]
CRATE_CHECKSUM="eb44a43551fbf5c70d11cc54d4c14ec8a40ebaf917a1d5ff1a158556e1fe093b"

def git(*args):
    return subprocess.check_output(["git",*args],text=True).strip()

def closure_entries(sha):
    out=git("ls-tree","-r",sha,"--",WORKLOAD)
    entries=[]
    for line in out.splitlines():
        meta,path=line.split("\t",1); mode,typ,oid=meta.split()
        if typ!="blob": continue
        rel=path[len(WORKLOAD)+1:]
        if any(rel==x or rel.startswith(x.rstrip("/")+"/") for x in INCLUDES):
            entries.append({"path":rel,"mode":mode,"git_blob_oid":oid})
    entries.sort(key=lambda x:x["path"])
    return entries

def closure_digest(entries):
    raw=b"".join(f'{x["path"]}\0{x["mode"]}\0{x["git_blob_oid"]}\n'.encode() for x in entries)
    return hashlib.sha256(raw).hexdigest()

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def input_identity(snapshot):
    raw=json.dumps(snapshot,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()

ap=argparse.ArgumentParser()
ap.add_argument("--validation-dir",required=True); ap.add_argument("--producer-sha",required=True)
ap.add_argument("--run-url",required=True); ap.add_argument("--rustc",required=True); ap.add_argument("--cargo",required=True); ap.add_argument("--postgres",required=True)
a=ap.parse_args()
vd=Path(a.validation_dir)
artifacts=sorted(p for p in vd.glob("*.json") if p.name!="evidence-manifest.json")
if len(artifacts)!=12: raise SystemExit(f"expected 12 evidence artifacts, got {len(artifacts)}")
records=[]
for p in artifacts:
    e=json.loads(p.read_text()); report=e["producer_report"]; snap=report["snapshot"]
    records.append({
      "scenario":e["scenario"],
      "artifact":{"path":f"validation/{p.name}","sha256":sha(p),"size_bytes":p.stat().st_size},
      "input":{"identity":{"kind":e["source_kind"],"sha256":input_identity(snap)},"reproduction":{
        "snapshot_id":snap["metadata"]["snapshot_id"],"scenario_case":snap["metadata"]["scenario_case"],"contract_version":"payment-reconciliation-v1"
      }},
      "parameters":{"campaign_issue":148,"validation_subject":"oxide-batch 0.6.0","expected_business_state":report["oracle"]["business_state"],"observations":e.get("observations") or {}},
      "failure_point":e.get("failure_point")
    })
entries=closure_entries(a.producer_sha)
entry={x["path"]:x for x in entries}
lock_oid=entry["Cargo.lock"]["git_blob_oid"]
producer_verifier="evidence/verify-retained-evidence.py"
canonical=vd/"verify-retained-evidence.py"
manifest={
 "schema_version":2,"workload":WORKLOAD,
 "producer":{"base_revision":a.producer_sha,"revision_role":"producer-checkout","run":{"kind":"github-actions-branch-evidence-producer","identity":a.run_url,"binding":"recorded-metadata"}},
 "semantic_closure":{"algorithm":"sha256-git-tree-entries-v1","includes":INCLUDES,"excluded_generated_paths":[r["artifact"]["path"] for r in records],"entries":entries,"digest_sha256":closure_digest(entries)},
 "validation_subject":{"lockfile":{"path":"Cargo.lock","git_blob_oid":lock_oid},"crates":[{"name":"oxide-batch","version":"0.6.0","source":"registry+https://github.com/rust-lang/crates.io-index","checksum":CRATE_CHECKSUM}]},
 "records":records,
 "verifier":{"canonical":{"path":"validation/verify-retained-evidence.py","sha256":sha(canonical),"result_model":"violations-v1"},"producer":{"path":producer_verifier,"git_blob_oid":entry[producer_verifier]["git_blob_oid"]}},
 "environment":{"observations":[
   {"name":"declared-rust-msrv","source":"producer Cargo.toml in semantic closure","trust":"recorded-metadata","value":"1.95"},
   {"name":"rustc-version","source":"producer workflow","trust":"recorded-metadata","value":a.rustc},
   {"name":"cargo-version","source":"producer workflow","trust":"recorded-metadata","value":a.cargo},
   {"name":"postgres-version","source":"workload PostgreSQL container","trust":"recorded-metadata","value":a.postgres},
   {"name":"github-actions-run","source":"producer workflow metadata","trust":"recorded-metadata","value":a.run_url}
 ],"limitations":[
   "Producer run identity is recorded metadata, not an externally attested trusted-producer binding.",
   "Crash/recovery records use deterministic frozen fixtures so recovery is isolated from mutable external state; separate business records retain the real payment-reference Merchant/provider snapshot.",
   "This supplemental campaign validates exact OxideBatch 0.6.0 behavior and is not retroactive completion evidence for the closed 0.6 release horizon."
 ]},
 "retention":{"committed_artifacts":{"directory":"validation","max_count":12,"max_total_bytes":262144,"supersession":"One canonical #148 supplemental business/recovery evidence set; replacement requires an atomic manifest, verifier and artifact update."},"wall_clock_freshness_merge_gate":False},
 "external_artifacts":[]
}
(vd/"evidence-manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
