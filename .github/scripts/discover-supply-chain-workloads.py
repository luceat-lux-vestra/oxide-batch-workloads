#!/usr/bin/env python3
"""Emit the canonical, validated supply-chain fan-out matrix as JSON.

Reuses the exact same fail-closed registry validation as
discover-workloads.py (`validate-workload-registry.py`), but this is a
distinct, narrower projection: it selects only `workloads.json`'s
`workloads` array -- real OxideBatch validation subjects -- and never
`fixtures`. Supply-chain policy scanning is a production-graph control;
`fixtures` are bounded CI-orchestration proofs (see
.github/WORKLOAD_CONTRACT.md) and must never become an equivalent-weight
supply-chain scanning subject, even though they participate in the shared
ci/msrv fan-out. This module never infers workloads by scanning directories
and never hardcodes a workload name.
"""

import json
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-workload-registry.py")
sys.path.insert(0, str(MODULE_PATH.parent))
import importlib.util

SPEC = importlib.util.spec_from_file_location("workload_registry_validator", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def discover(root: Path) -> dict:
    result = validator.validate_repository(root)
    return {"include": result["workloads"]}


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    try:
        matrix = discover(root)
    except validator.RegistryError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc
    if not matrix["include"]:
        print("::error::supply-chain discovery produced zero real workloads")
        raise SystemExit(1)
    print(json.dumps(matrix))


if __name__ == "__main__":
    main()
