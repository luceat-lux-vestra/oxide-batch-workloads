#!/usr/bin/env python3
import argparse,json
from pathlib import Path
ap=argparse.ArgumentParser()
ap.add_argument("--scenario",required=True); ap.add_argument("--source-kind",required=True)
ap.add_argument("--report",required=True); ap.add_argument("--output",required=True)
a=ap.parse_args()
report=json.loads(Path(a.report).read_text())
if report.get("violations") != []:
    raise SystemExit("producer verifier is not clean")
value={
 "schema_version":1,"campaign_issue":148,"classification":report["classification"],
 "scenario":a.scenario,"source_kind":a.source_kind,"failure_point":None,
 "observations":{"producer_path":"payment-reference-live-snapshot"},
 "producer_report":report
}
Path(a.output).write_text(json.dumps(value,indent=2,sort_keys=False)+"\n")
