#!/usr/bin/env python3
"""#318 day-wide coordinator for non-fault Merchant lifecycles.

This is deliberately not a terminal-day controller. It never installs fault
plans, executes fault-overlay slots, runs recovery/reconciliation/settlement,
or advances the campaign frontier. It only coordinates the normal online lane
using the one-slot write-ahead executor and re-verifies completed slot journals
before any resume.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
SCHEMA = "oxide-batch-workloads.payment-reference-day-online-normal-execution"
SCHEMA_VERSION = 1
DECISION = "NORMAL_TRAFFIC_COMPLETE_FAULT_OVERLAYS_PENDING"


def _load_pilot():
    spec = importlib.util.spec_from_file_location(
        "payment_reference_online_pilot_for_day",
        HERE / "campaign-online-pilot.py",
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load one-slot online pilot")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pilot = _load_pilot()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise pilot.Block(message)


def partition(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    slots = plan.get("slots")
    counts = plan.get("traffic_counts")
    require(isinstance(slots, list), "day traffic slots missing")
    require(isinstance(counts, dict), "day traffic counts missing")
    require(
        type(plan.get("primary_lifecycles")) is int
        and plan["primary_lifecycles"] == len(slots),
        "day primary lifecycle count mismatch",
    )
    require(
        [row.get("slot") for row in slots] == list(range(1, len(slots) + 1)),
        "day slot sequence is not contiguous",
    )

    normal = [row for row in slots if row.get("kind") in pilot.NORMAL]
    faults = [row for row in slots if row.get("kind") == "fault_overlay"]
    require(len(normal) + len(faults) == len(slots), "unknown online traffic class")

    expected_normal = sum(counts.get(kind, -1) for kind in pilot.NORMAL)
    expected_faults = counts.get("fault_overlay")
    require(expected_normal >= 0 and type(expected_faults) is int and expected_faults >= 0,
            "traffic count contract missing normal/fault geometry")
    require(len(normal) == expected_normal, "normal traffic slot count mismatch")
    require(len(faults) == expected_faults, "fault-overlay slot count mismatch")
    require(expected_normal + expected_faults == plan["primary_lifecycles"],
            "normal/fault partition does not cover the day")
    require(all(row.get("fault_class") is None for row in normal),
            "normal slot unexpectedly owns a fault class")
    require(all(isinstance(row.get("fault_class"), str) and row["fault_class"] for row in faults),
            "fault-overlay slot missing fault class")
    return normal, faults


def _summary(
    *,
    plan: dict[str, Any],
    source_sha: str,
    provenance_hashes: dict[str, str],
    normal: list[dict[str, Any]],
    faults: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_sha,
        "day_plan_sha256": pilot.sha(pilot.stable(plan)),
        "provenance_sha256": provenance_hashes,
        "normal_slot_count": len(normal),
        "normal_slots": [row["slot"] for row in normal],
        "normal_request_count": sum(len(pilot.slot_requests(row)) for row in normal),
        "fault_slot_count": len(faults),
        "pending_fault_slots": [
            {"slot": row["slot"], "fault_class": row["fault_class"]}
            for row in faults
        ],
        "independent_financial_verification_required": True,
        "day_completion_authorized": False,
    }


def retain_exact(path: Path, value: dict[str, Any]) -> str:
    require(not path.is_symlink(), "normal traffic summary path must not be a symlink")
    raw = pilot.stable(value)
    if path.exists():
        require(path.is_file(), "normal traffic summary path is not a file")
        require(path.read_bytes() == raw, "normal traffic summary immutable conflict")
        return "replay"
    pilot.exclusive_write(path, value)
    return "created"


RunSlot = Callable[..., dict[str, Any]]
ValidateSlot = Callable[..., dict[str, Any]]


def execute_normal_day(
    *,
    plan: dict[str, Any],
    evidence_root: Path,
    source_sha: str,
    provenance_hashes: dict[str, str],
    run_slot: RunSlot = pilot.run_one,
    validate_slot: ValidateSlot = pilot.validate_completed_slot,
) -> dict[str, Any]:
    normal, faults = partition(plan)
    require(not evidence_root.is_symlink(), "operator evidence root must not be a symlink")
    root = evidence_root.resolve()
    require(root.exists() and root.is_dir(), "operator evidence root must exist")

    executed_now: list[int] = []
    reused: list[int] = []
    day = pilot.day_path(root, plan)

    for item in normal:
        slot = item["slot"]
        slot_dir = day / f"slot-{slot:03}"
        if slot_dir.exists():
            validate_slot(
                plan=plan,
                slot=slot,
                evidence_root=root,
                source_sha=source_sha,
                provenance_hashes=provenance_hashes,
            )
            reused.append(slot)
            continue

        run_slot(
            plan=plan,
            slot=slot,
            evidence_root=root,
            source_sha=source_sha,
            provenance_hashes=provenance_hashes,
        )
        validate_slot(
            plan=plan,
            slot=slot,
            evidence_root=root,
            source_sha=source_sha,
            provenance_hashes=provenance_hashes,
        )
        executed_now.append(slot)

    require(day.is_dir() and not day.is_symlink(), "normal traffic day evidence missing after execution")
    summary = _summary(
        plan=plan,
        source_sha=source_sha,
        provenance_hashes=provenance_hashes,
        normal=normal,
        faults=faults,
    )
    retention = retain_exact(day / "normal-traffic.summary.json", summary)
    return {
        "decision": DECISION,
        "normal_slot_count": len(normal),
        "fault_slot_count": len(faults),
        "executed_this_invocation": executed_now,
        "reused_completed_slots": reused,
        "summary_retention": retention,
        "day_completion_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="#318 day-wide normal online traffic coordinator; fault overlays remain external"
    )
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--execute-normal-day", action="store_true")
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--provenance-dir", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    args = parser.parse_args()

    try:
        plan = pilot.traffic.build(args.campaign_id, args.day)
        normal, faults = partition(plan)
        if not args.execute_normal_day:
            print(json.dumps({
                "decision": "PLAN_ONLY",
                "normal_slots": [row["slot"] for row in normal],
                "pending_fault_slots": [
                    {"slot": row["slot"], "fault_class": row["fault_class"]}
                    for row in faults
                ],
                "no_http_mutations": True,
                "day_completion_authorized": False,
            }, sort_keys=True))
            return 0

        require(args.provenance_dir is not None and args.evidence_root is not None,
                "execution requires provenance and evidence root")
        require(args.expected_source_sha is not None,
                "execution requires exact expected source SHA")
        provenance_hashes = pilot.qualified_source(
            args.expected_source_sha,
            args.provenance_dir,
        )
        pilot.attest_live_topology(args.provenance_dir)
        result = execute_normal_day(
            plan=plan,
            evidence_root=args.evidence_root,
            source_sha=args.expected_source_sha,
            provenance_hashes=provenance_hashes,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
        OverflowError,
    ) as exc:
        print(
            json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
