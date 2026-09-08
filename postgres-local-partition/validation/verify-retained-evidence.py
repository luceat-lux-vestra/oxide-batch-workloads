#!/usr/bin/env python3
import argparse, json, math, statistics
from pathlib import Path

W=[1,2,4,8,16,32,64]
RUN="34183558594"
AID="10040083979"
SHA="d5ed675859780b3403b3afdf6bc0393c998f77bb"
SRC="4c40f627ed306157b0a1734ff7d03f5d94e45eb28b76d7158d5ec27c65a81518"
DST="8d8d62ad0aa2d84578a63ab01cf3c44af6a13618d7e37c759f186dc5bb469ff0"
RAW="05ad4cdb6afbf0c546dc5ec9a544b104c0cc0b768c2835e2d8799fb63d7dd141"
ZIP="4132b27cffe2f4bfbfcdb33605af3e2519fb32388e65eb01304d0aac5c93e46b"
CRATE="eb44a43551fbf5c70d11cc54d4c14ec8a40ebaf917a1d5ff1a158556e1fe093b"
COLS=["kind","ordinal","workers","durable_seconds","rows_per_second","worker_execution_count","peak_active_workers","active_workers_after_join"]

def eq(a,b): return math.isclose(float(a),float(b),rel_tol=1e-12,abs_tol=1e-9)
def add(v,ok,msg):
    if not ok:v.append(msg)
def load(p,v):
    try:return json.loads(Path(p).read_text())
    except Exception as e:v.append(f"cannot read {p}: {e}");return {}

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--manifest",required=True);a=ap.parse_args()
    v=[];m=load(a.manifest,v);rs=m.get("records") or []
    add(v,len(rs)==1,"manifest must retain exactly one record")
    r=rs[0] if rs else {}
    add(v,r.get("scenario")=="canonical_dense_single_host_scaling","unexpected scenario")
    e=load((r.get("artifact") or {}).get("path",""),v)
    add(v,e.get("schema_version")==1 and e.get("campaign")==93,"wrong evidence identity")
    add(v,e.get("classification")=="observational scaling evidence; no numeric regression threshold","classification drift")
    c=e.get("configuration") or {}
    add(v,c=={"measured_runs":7,"partitions":1024,"rows":262144,"sample_isolation":"fresh PostgreSQL clone from one migrated/seeded deterministic template per sample","seed":20260908,"timed_metric":"durable job execution created_at -> ended_at","warmups":1,"worker_points":W},"configuration drift")
    o=e.get("ownership") or {}
    add(v,o.get("status")=="UNKNOWN" and o.get("optimization_issue_allowed") is False,"ownership overclaim")
    p=e.get("producer") or {}
    add(v,p.get("run_id")==RUN and p.get("run_attempt")=="1" and p.get("artifact_id")==AID,"producer identity drift")
    add(v,p.get("raw_report_sha256")==RAW and p.get("artifact_zip_sha256")==ZIP,"producer digest drift")
    pr=e.get("provenance") or {};sub=pr.get("oxide_batch_subject") or {}
    add(v,pr.get("workload_sha")==SHA and pr.get("logical_cpu_count")==4,"producer provenance drift")
    add(v,sub=={"checksum":CRATE,"name":"oxide-batch","source":"registry+https://github.com/rust-lang/crates.io-index","version":"0.6.0"},"OxideBatch subject drift")
    cor=e.get("correctness") or {}
    add(v,cor.get("source_digest_sha256")==SRC and cor.get("destination_digest_sha256")==DST,"correctness digest drift")
    ext=m.get("external_artifacts") or []
    add(v,len(ext)==1 and ext[0].get("sha256")==ZIP,"raw external artifact drift")
    if ext:
        add(v,RUN in ext[0].get("reference","") and AID in ext[0].get("reference",""),"raw artifact reference drift")
        add(v,"2026-10-08T03:46:39Z" in ext[0].get("retention_guarantee",""),"raw artifact expiry missing")
    add(v,e.get("sample_columns")==COLS,"sample schema drift")
    rows=e.get("samples") or [];add(v,len(rows)==56,"expected 56 samples")
    samples=[]
    for i,row in enumerate(rows):
        if not isinstance(row,list) or len(row)!=len(COLS):v.append(f"sample {i} shape");continue
        s=dict(zip(COLS,row));samples.append(s);w=s["workers"]
        add(v,w in W,f"sample {i} workers")
        add(v,s["worker_execution_count"]==1024 and s["peak_active_workers"]==w and s["active_workers_after_join"]==0,f"sample {i} worker invariant")
        if isinstance(s["durable_seconds"],(int,float)) and s["durable_seconds"]>0:
            add(v,eq(s["rows_per_second"],262144/s["durable_seconds"]),f"sample {i} throughput derivation")
        else:v.append(f"sample {i} durable timing")
    warm=[s for s in samples if s["kind"]=="warmup"];meas=[s for s in samples if s["kind"]=="measured"]
    add(v,len(warm)==7 and [s["workers"] for s in warm]==W and all(s["ordinal"]==0 for s in warm),"warmup schedule")
    add(v,len(meas)==49,"measured sample count")
    rounds={};byw={w:[] for w in W}
    for s in meas:rounds.setdefault(s["ordinal"],{})[s["workers"]]=s;byw[s["workers"]].append(s)
    for n in range(7):
        add(v,set(rounds.get(n,{}))==set(W),f"round {n} coverage")
        add(v,[s["workers"] for s in meas if s["ordinal"]==n]==W[n:]+W[:n],f"round {n} order")
    calc={}
    if all(set(rounds.get(n,{}))==set(W) for n in range(7)):
        paired={w:[] for w in W}
        for n in range(7):
            base=rounds[n][1]["durable_seconds"]
            for w in W:paired[w].append(base/rounds[n][w]["durable_seconds"])
        for w in W:
            sp=statistics.median(paired[w])
            calc[str(w)]={"durable_seconds_median":statistics.median([s["durable_seconds"] for s in byw[w]]),"rows_per_second_median":statistics.median([s["rows_per_second"] for s in byw[w]]),"paired_speedup_median":sp,"scaling_efficiency_median":sp/w}
    got=(e.get("summary") or {}).get("per_worker") or {};add(v,set(got)=={str(w) for w in W},"summary worker set")
    for w,fields in calc.items():
        for k,x in fields.items():add(v,k in got.get(w,{}) and eq(got[w][k],x),f"summary {w}.{k}")
    print(json.dumps({"schema_version":1,"violations":v},sort_keys=True));raise SystemExit(bool(v))

if __name__=="__main__":main()
