#!/usr/bin/env python3
"""#318 canonical 100-slot online execution controller.

Unlike the lane coordinators, this controller owns the actual primary-lifecycle
order. It writes a durable intent before each slot, executes normal and
fault-overlay slots in the exact deterministic plan order, and chains each
validated completion to the next intent. Existing lane evidence is never
retroactively promoted to canonical-order proof.
"""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

import campaign_day_fault_execution as fault_day
import campaign_day_online_traffic as normal_day
import campaign_hard_crash as hard_crash

pilot = normal_day.pilot

SCHEMA = "oxide-batch-workloads.payment-reference-day-online-canonical-execution"
SCHEMA_VERSION = 1
JOURNAL_SCHEMA = pilot.CANONICAL_ORDER_JOURNAL_SCHEMA
INTENT_SCHEMA = pilot.CANONICAL_ORDER_INTENT_SCHEMA
COMPLETION_SCHEMA = "oxide-batch-workloads.payment-reference-online-order-completion"
DECISION = "ONLINE_TRAFFIC_COMPLETE_CANONICAL_ORDER_VERIFIED"
ORDER_DIR = pilot.CANONICAL_ORDER_DIR
SUMMARY_FILE = "online-traffic.summary.json"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise pilot.Block(message)


def journal_identity(
    plan: dict[str, Any],
    source_sha: str,
    provenance_hashes: dict[str, str],
) -> dict[str, Any]:
    return pilot.canonical_order_identity(plan, source_sha, provenance_hashes)


def order_files(order_dir: Path, slot: int) -> tuple[Path, Path]:
    return pilot.canonical_order_files(order_dir, slot)


def slot_evidence_sha256(folder: Path) -> dict[str, str]:
    require(folder.is_dir() and not folder.is_symlink(), "slot evidence directory missing/unsafe")
    result: dict[str, str] = {}
    for path in sorted(folder.iterdir(), key=lambda value: value.name):
        require(path.is_file() and not path.is_symlink(), "slot evidence contains non-file/symlink")
        result[path.name] = pilot.sha(path.read_bytes())
    require("result.json" in result, "slot result missing from canonical evidence")
    return result


def expected_intent(
    item: dict[str, Any],
    previous_chain_sha256: str,
) -> dict[str, Any]:
    return pilot.canonical_order_intent(item, previous_chain_sha256)


def expected_completion(
    item: dict[str, Any],
    intent_sha256: str,
    evidence_sha256: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema": COMPLETION_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "ordinal": item["slot"],
        "slot": item["slot"],
        "kind": item["kind"],
        "intent_sha256": intent_sha256,
        "result_sha256": evidence_sha256["result.json"],
        "evidence_sha256": evidence_sha256,
    }


def retain_exact(path: Path, value: dict[str, Any]) -> str:
    require(not path.is_symlink(), "canonical online summary path must not be a symlink")
    raw = pilot.stable(value)
    if path.exists():
        require(path.is_file(), "canonical online summary path is not a file")
        require(path.read_bytes() == raw, "canonical online summary immutable conflict")
        return "replay"
    pilot.exclusive_write(path, value)
    return "created"


def ensure_order_journal(
    *,
    day: Path,
    plan: dict[str, Any],
    source_sha: str,
    provenance_hashes: dict[str, str],
) -> Path:
    require(
        not (day / "normal-traffic.summary.json").exists()
        and not (day / "fault-traffic.summary.json").exists(),
        "lane summaries cannot be retroactively promoted to canonical execution",
    )
    order_dir = day / ORDER_DIR
    expected = journal_identity(plan, source_sha, provenance_hashes)
    if order_dir.exists():
        require(order_dir.is_dir() and not order_dir.is_symlink(), "canonical order journal path unsafe")
        require(pilot.read_object(order_dir / "identity.json") == expected, "canonical order journal identity drift")
        return order_dir

    for item in plan["slots"]:
        require(
            not (day / f"slot-{item['slot']:03d}").exists(),
            "pre-existing slot evidence cannot be retroactively ordered",
        )
    order_dir.mkdir(mode=0o700, exist_ok=False)
    pilot.fsync_dir(day)
    pilot.exclusive_write(order_dir / "identity.json", expected)
    return order_dir


def require_no_future_evidence(
    *,
    day: Path,
    order_dir: Path,
    plan: dict[str, Any],
    start_slot: int,
) -> None:
    for item in plan["slots"][start_slot - 1:]:
        intent, complete = order_files(order_dir, item["slot"])
        require(not intent.exists() and not complete.exists(), "later canonical order evidence exists before missing prefix")
        require(
            not (day / f"slot-{item['slot']:03d}").exists(),
            "slot evidence exists without canonical order intent",
        )


def validate_slot(
    *,
    plan: dict[str, Any],
    item: dict[str, Any],
    evidence_root: Path,
    source_sha: str,
    provenance_hashes: dict[str, str],
    validate_normal: Callable[..., dict[str, Any]],
    validate_fault: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if item["kind"] in pilot.NORMAL:
        return validate_normal(
            plan=plan,
            slot=item["slot"],
            evidence_root=evidence_root,
            source_sha=source_sha,
            provenance_hashes=provenance_hashes,
        )
    require(item["kind"] == "fault_overlay", "canonical controller encountered unknown traffic kind")
    return validate_fault(
        plan=plan,
        item=item,
        evidence_root=evidence_root,
        source_sha=source_sha,
        provenance_hashes=provenance_hashes,
    )


def validate_completion(
    *,
    day: Path,
    order_dir: Path,
    plan: dict[str, Any],
    item: dict[str, Any],
    previous_chain_sha256: str | None,
    evidence_root: Path,
    source_sha: str,
    provenance_hashes: dict[str, str],
    validate_normal: Callable[..., dict[str, Any]],
    validate_fault: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    intent_path, complete_path = order_files(order_dir, item["slot"])
    require(intent_path.is_file() and not intent_path.is_symlink(), "canonical slot intent missing/unsafe")
    require(complete_path.is_file() and not complete_path.is_symlink(), "canonical slot completion missing/unsafe")
    expected = expected_intent(item, previous_chain_sha256)
    require(pilot.read_object(intent_path) == expected, "canonical slot intent drift")
    result = validate_slot(
        plan=plan,
        item=item,
        evidence_root=evidence_root,
        source_sha=source_sha,
        provenance_hashes=provenance_hashes,
        validate_normal=validate_normal,
        validate_fault=validate_fault,
    )
    evidence = slot_evidence_sha256(day / f"slot-{item['slot']:03d}")
    completion = expected_completion(item, pilot.sha(intent_path.read_bytes()), evidence)
    require(pilot.read_object(complete_path) == completion, "canonical slot completion drift")
    return result, pilot.sha(complete_path.read_bytes())


def summary(
    *,
    plan: dict[str, Any],
    source_sha: str,
    provenance_hashes: dict[str, str],
    journal_identity_sha256: str,
    completion_hashes: list[dict[str, Any]],
    fault_results: list[dict[str, Any]],
) -> dict[str, Any]:
    normal, faults = normal_day.partition(plan)
    require(len(completion_hashes) == plan["primary_lifecycles"], "canonical completion count mismatch")
    require(
        [row["slot"] for row in completion_hashes] == [item["slot"] for item in plan["slots"]],
        "canonical completion order mismatch",
    )
    require(len(fault_results) == len(faults), "canonical fault result count mismatch")
    require(
        all(type(result.get("recovery_reconciliation_required")) is bool for result in fault_results),
        "canonical fault recovery flag missing",
    )
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_sha,
        "day_plan_sha256": pilot.sha(pilot.stable(plan)),
        "fault_catalog_sha256": plan["fault_catalog_sha256"],
        "provenance_sha256": provenance_hashes,
        "order_journal_identity_sha256": journal_identity_sha256,
        "primary_lifecycle_count": plan["primary_lifecycles"],
        "normal_slot_count": len(normal),
        "fault_slot_count": len(faults),
        "slot_order": [item["slot"] for item in plan["slots"]],
        "slot_completion_sha256": completion_hashes,
        "order_chain_head_sha256": completion_hashes[-1]["sha256"],
        "combined_normal_fault_ordering_authorized": True,
        "online_completion_authorized": True,
        "scheduled_hard_crash": hard_crash.scheduled_spec(plan),
        "recovery_reconciliation_required": any(
            result.get("recovery_reconciliation_required") is True
            for result in fault_results
        ),
        "independent_financial_verification_required": True,
        "day_completion_authorized": False,
    }


def execute_canonical_day(
    *,
    plan: dict[str, Any],
    evidence_root: Path,
    source_sha: str,
    provenance_hashes: dict[str, str],
    fault_proxy_base: str,
    fault_control_timeout_seconds: float,
    provider_control_timeout_seconds: float,
    ensure_setup: Callable[..., Path] = pilot.ensure_day_setup,
    run_normal: Callable[..., dict[str, Any]] = pilot.run_one,
    validate_normal: Callable[..., dict[str, Any]] = pilot.validate_completed_slot,
    run_fault: Callable[..., dict[str, Any]] = fault_day.execute_fault_slot,
    validate_fault: Callable[..., dict[str, Any]] = fault_day.validate_completed_fault_slot,
) -> dict[str, Any]:
    normal_day.partition(plan)
    require(not evidence_root.is_symlink(), "operator evidence root must not be a symlink")
    root = evidence_root.resolve()
    require(root.exists() and root.is_dir(), "operator evidence root must exist")
    fault_proxy_base = fault_day.validate_proxy_base(fault_proxy_base)
    require(
        isinstance(fault_control_timeout_seconds, (int, float))
        and not isinstance(fault_control_timeout_seconds, bool)
        and fault_control_timeout_seconds > 0,
        "fault control timeout must be positive",
    )
    require(
        isinstance(provider_control_timeout_seconds, (int, float))
        and not isinstance(provider_control_timeout_seconds, bool)
        and provider_control_timeout_seconds > 0,
        "provider control timeout must be positive",
    )

    executed: list[int] = []
    reused: list[int] = []
    fault_results: list[dict[str, Any]] = []
    completion_hashes: list[dict[str, Any]] = []

    with pilot.online_controller_lock_path(root, plan).open("a+b") as controller_lock:
        fcntl.flock(controller_lock, fcntl.LOCK_EX)
        day = ensure_setup(
            plan=plan,
            evidence_root=root,
            source_sha=source_sha,
            provenance_hashes=provenance_hashes,
        )
        require(
            pilot.read_object(day / "identity.json") == pilot.day_identity(plan, source_sha, provenance_hashes),
            "canonical day source/provenance/plan drift",
        )
        order_dir = ensure_order_journal(
            day=day,
            plan=plan,
            source_sha=source_sha,
            provenance_hashes=provenance_hashes,
        )

        journal_identity_sha256 = pilot.sha((order_dir / "identity.json").read_bytes())
        previous_chain_sha256: str = journal_identity_sha256
        for item in plan["slots"]:
            intent_path, complete_path = order_files(order_dir, item["slot"])
            if intent_path.exists():
                require(
                    complete_path.exists(),
                    "canonical slot has durable intent without completion; replay is forbidden",
                )
                result, previous_chain_sha256 = validate_completion(
                    day=day,
                    order_dir=order_dir,
                    plan=plan,
                    item=item,
                    previous_chain_sha256=previous_chain_sha256,
                    evidence_root=root,
                    source_sha=source_sha,
                    provenance_hashes=provenance_hashes,
                    validate_normal=validate_normal,
                    validate_fault=validate_fault,
                )
                reused.append(item["slot"])
            else:
                require(not complete_path.exists(), "canonical completion exists without intent")
                require_no_future_evidence(
                    day=day,
                    order_dir=order_dir,
                    plan=plan,
                    start_slot=item["slot"],
                )
                intent = expected_intent(item, previous_chain_sha256)
                pilot.exclusive_write(intent_path, intent)

                if item["kind"] in pilot.NORMAL:
                    run_normal(
                        plan=plan,
                        slot=item["slot"],
                        evidence_root=root,
                        source_sha=source_sha,
                        provenance_hashes=provenance_hashes,
                    )
                else:
                    require(item["kind"] == "fault_overlay", "canonical controller encountered unknown traffic kind")
                    with (day / ".lock").open("a+b") as day_lock:
                        fcntl.flock(day_lock, fcntl.LOCK_EX)
                        run_fault(
                            plan=plan,
                            item=item,
                            evidence_root=root,
                            source_sha=source_sha,
                            provenance_hashes=provenance_hashes,
                            fault_proxy_base=fault_proxy_base,
                            fault_control_timeout_seconds=fault_control_timeout_seconds,
                            provider_control_timeout_seconds=provider_control_timeout_seconds,
                        )

                result = validate_slot(
                    plan=plan,
                    item=item,
                    evidence_root=root,
                    source_sha=source_sha,
                    provenance_hashes=provenance_hashes,
                    validate_normal=validate_normal,
                    validate_fault=validate_fault,
                )
                evidence = slot_evidence_sha256(day / f"slot-{item['slot']:03d}")
                pilot.exclusive_write(
                    complete_path,
                    expected_completion(item, pilot.sha(intent_path.read_bytes()), evidence),
                )
                result, previous_chain_sha256 = validate_completion(
                    day=day,
                    order_dir=order_dir,
                    plan=plan,
                    item=item,
                    previous_chain_sha256=(
                        journal_identity_sha256 if item["slot"] == 1 else completion_hashes[-1]["sha256"]
                    ),
                    evidence_root=root,
                    source_sha=source_sha,
                    provenance_hashes=provenance_hashes,
                    validate_normal=validate_normal,
                    validate_fault=validate_fault,
                )
                executed.append(item["slot"])

            completion_hashes.append({
                "slot": item["slot"],
                "sha256": previous_chain_sha256,
            })
            if item["kind"] == "fault_overlay":
                fault_results.append(result)

        expected_order_files = {order_dir / "identity.json"}
        for item in plan["slots"]:
            expected_order_files.update(order_files(order_dir, item["slot"]))
        require(set(order_dir.iterdir()) == expected_order_files, "canonical order journal contains unexpected files")

        value = summary(
            plan=plan,
            source_sha=source_sha,
            provenance_hashes=provenance_hashes,
            journal_identity_sha256=journal_identity_sha256,
            completion_hashes=completion_hashes,
            fault_results=fault_results,
        )
        retention = retain_exact(day / SUMMARY_FILE, value)

    return {
        "decision": DECISION,
        "primary_lifecycle_count": plan["primary_lifecycles"],
        "executed_this_invocation": executed,
        "reused_completed_slots": reused,
        "summary_retention": retention,
        "combined_normal_fault_ordering_authorized": True,
        "online_completion_authorized": True,
        "day_completion_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 canonical normal+fault online day controller")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--execute-canonical-day", action="store_true")
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--provenance-dir", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--fault-proxy-base-url")
    parser.add_argument("--fault-control-timeout-seconds", type=float)
    parser.add_argument("--provider-control-timeout-seconds", type=float)
    args = parser.parse_args()

    try:
        plan = pilot.traffic.build(args.campaign_id, args.day)
        normal_day.partition(plan)
        if not args.execute_canonical_day:
            print(json.dumps({
                "decision": "PLAN_ONLY",
                "slot_order": [
                    {
                        "slot": item["slot"],
                        "kind": item["kind"],
                        "fault_class": item["fault_class"],
                    }
                    for item in plan["slots"]
                ],
                "primary_lifecycle_count": plan["primary_lifecycles"],
                "no_http_mutations": True,
                "day_completion_authorized": False,
            }, sort_keys=True))
            return 0

        require(
            args.expected_source_sha is not None
            and args.provenance_dir is not None
            and args.evidence_root is not None,
            "execution requires exact source, provenance and evidence root",
        )
        require(
            args.fault_proxy_base_url is not None
            and args.fault_control_timeout_seconds is not None
            and args.provider_control_timeout_seconds is not None,
            "execution requires explicit fault-control endpoint and timeouts",
        )
        provenance_hashes = pilot.qualified_source(args.expected_source_sha, args.provenance_dir)
        pilot.attest_live_topology(args.provenance_dir)
        result = execute_canonical_day(
            plan=plan,
            evidence_root=args.evidence_root,
            source_sha=args.expected_source_sha,
            provenance_hashes=provenance_hashes,
            fault_proxy_base=args.fault_proxy_base_url,
            fault_control_timeout_seconds=args.fault_control_timeout_seconds,
            provider_control_timeout_seconds=args.provider_control_timeout_seconds,
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
