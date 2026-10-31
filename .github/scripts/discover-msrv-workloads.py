#!/usr/bin/env python3
"""Emit the validated execution matrix for declared-MSRV entries only.

The full canonical discovery remains the aggregate authority. This narrower
projection is used only to decide which entries need an actual MSRV runner.
It is derived exclusively from the fail-closed validated registry and never
hardcodes an entry name.
"""

import json
import sys
from pathlib import Path
import importlib.util

MODULE_PATH = Path(__file__).with_name("validate-workload-registry.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("workload_registry_validator", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def discover(root: Path) -> dict:
    result = validator.validate_repository(root)
    include = result["workloads"] + result["fixtures"]
    return {"include": [entry for entry in include if entry["msrv"]["declared"]]}


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    try:
        matrix = discover(root)
    except validator.RegistryError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc
    print(json.dumps(matrix))


if __name__ == "__main__":
    main()
