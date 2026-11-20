#!/usr/bin/env python3
"""Canonical #318 deterministic fault catalog and pure plan renderer."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

CATALOG_PATH = Path(__file__).with_name("fault-catalog-v1.json")
SCHEMA = "oxide-batch-workloads.payment-reference-campaign-fault-catalog"
SCHEMA_VERSION = 1
PLACEHOLDER = "{provider_payment_id}"
PAYMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class FaultCatalogError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FaultCatalogError(message)


def _load(path: Path = CATALOG_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), "fault catalog must be a JSON object")
    require(
        set(value) == {"schema", "schema_version", "fault_plan_version", "faults"},
        "fault catalog fields mismatch",
    )
    require(value["schema"] == SCHEMA and value["schema_version"] == SCHEMA_VERSION, "fault catalog schema mismatch")
    require(value["fault_plan_version"] == 1, "fault plan version must remain v1")
    faults = value["faults"]
    require(isinstance(faults, list) and len(faults) == 5, "fault catalog must contain exactly five cases")
    seen = set()
    for index, fault in enumerate(faults):
        require(isinstance(fault, dict), f"fault[{index}] must be an object")
        require(
            set(fault) == {
                "fault_class", "scenario_case", "scenario_version", "seed",
                "expected_provider_truth", "expected_canonical_outcome",
                "caller_outcome", "reconciliation", "steps",
            },
            f"fault[{index}] fields mismatch",
        )
        fault_class = fault["fault_class"]
        require(isinstance(fault_class, str) and fault_class.strip(), f"fault[{index}] class must be non-blank")
        require(fault_class not in seen, f"duplicate fault class: {fault_class}")
        seen.add(fault_class)
        for field in ("scenario_case", "expected_provider_truth", "expected_canonical_outcome", "caller_outcome"):
            require(isinstance(fault[field], str) and fault[field].strip(), f"{fault_class}: {field} must be non-blank")
        require(type(fault["scenario_version"]) is int and fault["scenario_version"] > 0, f"{fault_class}: bad scenario_version")
        require(type(fault["seed"]) is int and fault["seed"] >= 0, f"{fault_class}: bad seed")
        reconciliation = fault["reconciliation"]
        require(isinstance(reconciliation, dict), f"{fault_class}: reconciliation must be an object")
        require(
            set(reconciliation) == {
                "classification", "requires_read_before_mutation_retry",
                "mutation_retry_policy", "requirements",
            },
            f"{fault_class}: reconciliation fields mismatch",
        )
        require(isinstance(reconciliation["classification"], str) and reconciliation["classification"], f"{fault_class}: missing reconciliation classification")
        require(type(reconciliation["requires_read_before_mutation_retry"]) is bool, f"{fault_class}: read-before-retry flag must be bool")
        require(isinstance(reconciliation["mutation_retry_policy"], str) and reconciliation["mutation_retry_policy"], f"{fault_class}: missing mutation retry policy")
        requirements = reconciliation["requirements"]
        require(
            isinstance(requirements, list)
            and requirements
            and all(isinstance(item, str) and item for item in requirements)
            and len(requirements) == len(set(requirements)),
            f"{fault_class}: reconciliation requirements must be unique non-blank strings",
        )
        require(isinstance(fault["steps"], list) and fault["steps"], f"{fault_class}: steps must be non-empty")
    return value


def catalog_sha256(path: Path = CATALOG_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classes() -> tuple[str, ...]:
    return tuple(fault["fault_class"] for fault in _load()["faults"])


def fault(fault_class: str) -> dict[str, Any]:
    for value in _load()["faults"]:
        if value["fault_class"] == fault_class:
            return copy.deepcopy(value)
    raise FaultCatalogError(f"unknown fault class: {fault_class}")


def _render(value: Any, payment_id: str | None) -> Any:
    if isinstance(value, str):
        if PLACEHOLDER not in value:
            return value
        require(isinstance(payment_id, str) and payment_id.strip(), "provider payment id is required for this fault class")
        require(PAYMENT_ID.fullmatch(payment_id) is not None, "provider payment id must be a safe path segment")
        return value.replace(PLACEHOLDER, payment_id)
    if isinstance(value, list):
        return [_render(item, payment_id) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, payment_id) for key, item in value.items()}
    return copy.deepcopy(value)


def render_plan(fault_class: str, logical_business_time: str, payment_id: str | None = None) -> dict[str, Any]:
    require(isinstance(logical_business_time, str) and logical_business_time.strip(), "logical business time must be non-blank")
    catalog = _load()
    entry = fault(fault_class)
    steps = _render(entry["steps"], payment_id)
    rendered = json.dumps(steps, sort_keys=True)
    require(PLACEHOLDER not in rendered, "unresolved provider payment id placeholder")
    return {
        "fault_plan_version": catalog["fault_plan_version"],
        "scenario_version": entry["scenario_version"],
        "seed": entry["seed"],
        "logical_business_time": logical_business_time,
        "scenario_case": entry["scenario_case"],
        "expected_provider_truth": entry["expected_provider_truth"],
        "expected_canonical_outcome": entry["expected_canonical_outcome"],
        "steps": steps,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#318 canonical deterministic fault catalog")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("classes")
    p = sub.add_parser("render")
    p.add_argument("--fault-class", required=True)
    p.add_argument("--logical-business-time", required=True)
    p.add_argument("--payment-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            output = {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "fault_classes": list(classes()),
                "catalog_sha256": catalog_sha256(),
            }
        elif args.command == "classes":
            output = list(classes())
        else:
            output = render_plan(args.fault_class, args.logical_business_time, args.payment_id)
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0
    except (FaultCatalogError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"decision": "BLOCK", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
