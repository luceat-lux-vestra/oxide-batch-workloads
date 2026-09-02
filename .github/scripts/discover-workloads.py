#!/usr/bin/env python3
"""Emit the canonical, validated workload fan-out matrix as JSON.

This is the only place the central CI workflow learns which workloads exist
and what their MSRV/provenance policy is. It never inspects a workload's
business logic: everything it emits comes straight from the already
fail-closed-validated `workloads.json` registry entries.
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
    entries = validator.validate_repository(root)
    return {"include": entries}


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    try:
        matrix = discover(root)
    except validator.RegistryError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc
    if not matrix["include"]:
        print("::error::canonical workload discovery produced zero workloads")
        raise SystemExit(1)
    print(json.dumps(matrix))


if __name__ == "__main__":
    main()
