#!/usr/bin/env python3
"""#318 independent per-day payment-reconciliation proof.

This verifier binds one frozen payment-reconciliation-postgres snapshot/result to
an exact logical day, replays the retained v0.7.0 reconciliation oracle, and
returns a compact subject for the day frontier. It does not run the workload or
refresh Merchant/provider state.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any

import campaign_core as core
import campaign_day_cardinality as day_cardinality

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
RECONCILIATION_VERIFIER = (
    REPO_ROOT / "payment-reconciliation-postgres" / "evidence" / "verify-retained-evidence.py"
)

SCHEMA = "oxide-batch-workloads.payment-reference-day-reconciliation-verification"
SCHEMA_VERSION = 1
DECISION = "RECONCILIATION_VERIFIED_NOT_DAY_ACCEPTED"
CRASH_SCHEMA = "oxide-batch-workloads.payment-reference-reconciliation-crash-execution"
WORKLOAD_CONTRACT = "payment-reconciliation-v1"
POLICY_VERSION = "payment-reconciliation-policy-v1"
OXIDE_BATCH_VERSION = "0.7.0"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")

RECONCILIATION_CRASHES = {
    "reconciliation-during-write": ("during-write", "recover-run-finalize"),
    "reconciliation-after-business-commit": ("after-business-commit", "recover-run-finalize"),
    "reconciliation-finalize-after-commit": ("after-commit", "finalize-replay"),
}


class ReconciliationEvidenceError(core.EvidenceError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReconciliationEvidenceError(message)


def object_(value: Any, name: str) -> dict[str, Any]:
    require(type(value) is dict, f"{name}: expected object")
    return value


def array(value: Any, name: str) -> list[Any]:
    require(type(value) is list, f"{name}: expected array")
    return value


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


qualification = _module("payment_reconciliation_retained_verifier", RECONCILIATION_VERIFIER)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_digest(plan: dict[str, Any]) -> str:
    return json_sha256(plan)


def expected_snapshot_id(campaign_id: str, sequence: int) -> str:
    return f"{campaign_id}:day-{sequence:06}:reconciliation"


def scheduled_hard_crash(plan: dict[str, Any]) -> str | None:
    sequence = plan.get("logical_day_sequence")
    require(type(sequence) is int and sequence > 0, "reconciliation plan logical day sequence invalid")
    day = core.plan_day(sequence)
    require(day.get("logical_day_sequence") == sequence, "reconciliation core day identity mismatch")
    scheduled = day.get("scheduled_hard_crash")
    return scheduled if scheduled in RECONCILIATION_CRASHES else None


def _validate_crash(
    crash: Any,
    *,
    plan: dict[str, Any],
    snapshot_id: str,
) -> dict[str, Any] | None:
    scheduled = scheduled_hard_crash(plan)
    if scheduled is None:
        require(crash is None, "reconciliation crash evidence supplied on a day without a scheduled reconciliation crash")
        return None

    require(crash is not None, "scheduled reconciliation crash evidence required")
    crash = object_(crash, "reconciliation crash evidence")
    expected_fields = {
        "schema", "schema_version", "campaign_id", "logical_day_sequence",
        "snapshot_id", "boundary", "semantic_marker_observed", "signal",
        "killed_pid", "process_gone_after_kill", "recovery_action",
        "new_process_pid", "post_recovery_processing_state",
        "final_publication_count",
    }
    require(set(crash) == expected_fields, "reconciliation crash evidence fields mismatch")
    require(crash["schema"] == CRASH_SCHEMA and crash["schema_version"] == 1,
            "reconciliation crash evidence schema mismatch")
    require(
        crash["campaign_id"] == plan["campaign_id"]
        and crash["logical_day_sequence"] == plan["logical_day_sequence"]
        and crash["snapshot_id"] == snapshot_id,
        "reconciliation crash campaign/day/snapshot identity mismatch",
    )
    boundary, recovery_action = RECONCILIATION_CRASHES[scheduled]
    require(crash["boundary"] == boundary, "reconciliation crash boundary mismatch")
    require(crash["recovery_action"] == recovery_action, "reconciliation crash recovery action mismatch")
    require(crash["semantic_marker_observed"] is True and crash["signal"] == 9,
            "reconciliation crash marker/SIGKILL proof missing")
    require(type(crash["killed_pid"]) is int and crash["killed_pid"] > 1,
            "reconciliation killed PID invalid")
    require(crash["process_gone_after_kill"] is True,
            "reconciliation killed process was not proven gone")
    require(type(crash["new_process_pid"]) is int and crash["new_process_pid"] > 1
            and crash["new_process_pid"] != crash["killed_pid"],
            "reconciliation continuation must use a genuinely new process")
    require(crash["post_recovery_processing_state"] == "PROCESSED",
            "reconciliation post-recovery processing state must be PROCESSED")
    require(crash["final_publication_count"] == 1,
            "reconciliation CLEAR recovery must retain exactly one publication")
    return {
        "boundary": boundary,
        "signal": 9,
        "recovery_action": recovery_action,
        "final_publication_count": 1,
    }


def _snapshot(report: dict[str, Any]) -> dict[str, Any]:
    snap = object_(report.get("snapshot"), "reconciliation retained snapshot")
    require(set(snap) == {"metadata", "internal", "provider", "mutation_recipe"},
            "reconciliation snapshot fields mismatch")
    return snap


def validate(
    artifact: Any,
    *,
    plan: dict[str, Any],
    source_revision: str,
    workload_binary_sha256: str,
) -> dict[str, Any]:
    artifact = object_(artifact, "reconciliation artifact")
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "day_plan_sha256",
        "oxide_batch_version", "workload_binary_sha256",
        "workload_semantics_sha256", "producer_report",
        "scheduled_hard_crash", "crash_evidence",
    }
    require(set(artifact) == expected_fields, "reconciliation artifact fields mismatch")
    require(
        artifact["schema"] == SCHEMA
        and artifact["schema_version"] == SCHEMA_VERSION
        and artifact["decision"] == DECISION,
        "reconciliation artifact schema/decision mismatch",
    )
    require(
        artifact["campaign_id"] == plan.get("campaign_id")
        and artifact["logical_day_sequence"] == plan.get("logical_day_sequence")
        and artifact["source_revision"] == source_revision,
        "reconciliation artifact campaign/day/source mismatch",
    )
    require(artifact["day_plan_sha256"] == plan_digest(plan),
            "reconciliation day plan digest mismatch")
    require(artifact["oxide_batch_version"] == OXIDE_BATCH_VERSION,
            "reconciliation OxideBatch version mismatch")
    require(
        isinstance(workload_binary_sha256, str)
        and HEX64.fullmatch(workload_binary_sha256) is not None
        and artifact["workload_binary_sha256"] == workload_binary_sha256,
        "reconciliation workload binary identity mismatch",
    )
    require(
        artifact["workload_semantics_sha256"] == file_sha256(RECONCILIATION_VERIFIER),
        "reconciliation retained-verifier source identity mismatch",
    )

    report = object_(artifact["producer_report"], "reconciliation producer report")
    require(
        set(report) == {
            "schema_version", "campaign_issue", "classification", "snapshot",
            "base_snapshot", "oracle", "actual", "violations",
        },
        "reconciliation producer report fields mismatch",
    )
    require(
        report["schema_version"] == 1
        and report["campaign_issue"] == 148
        and report["classification"] == qualification.CLASSIFICATION
        and report["violations"] == [],
        "reconciliation producer report identity/violations mismatch",
    )
    require(report["base_snapshot"] is None,
            "campaign reconciliation must use the root live CLEAR snapshot, not a derived scenario")

    snapshot = _snapshot(report)
    metadata = object_(snapshot["metadata"], "reconciliation snapshot metadata")
    require(
        set(metadata) == {
            "snapshot_id", "business_date", "provider_scope", "scenario_case",
            "base_snapshot_id", "internal_snapshot_digest",
            "provider_snapshot_digest", "policy_version",
        },
        "reconciliation snapshot metadata fields mismatch",
    )
    sequence = plan["logical_day_sequence"]
    expected_day = core.plan_day(sequence)
    merchant_setup = array(plan.get("merchant_setup"), "reconciliation merchant setup")
    require(bool(merchant_setup), "reconciliation merchant setup missing")
    merchant_id = object_(merchant_setup[0], "reconciliation merchant setup[0]").get("body", {}).get("merchantId")
    require(isinstance(merchant_id, str) and merchant_id,
            "reconciliation Merchant scope identity missing")
    snapshot_id = expected_snapshot_id(plan["campaign_id"], sequence)
    require(
        metadata["snapshot_id"] == snapshot_id
        and metadata["business_date"] == expected_day["logical_business_date"]
        and metadata["provider_scope"] == merchant_id
        and metadata["scenario_case"] == "CLEAR"
        and metadata["base_snapshot_id"] is None
        and metadata["policy_version"] == POLICY_VERSION,
        "reconciliation snapshot identity/contract mismatch",
    )
    require(snapshot["mutation_recipe"] == {},
            "campaign reconciliation root snapshot must not contain a mutation recipe")

    internal = array(snapshot["internal"], "reconciliation internal snapshot")
    provider = array(snapshot["provider"], "reconciliation provider snapshot")
    require(bool(internal) and bool(provider),
            "reconciliation campaign snapshot must contain stable payment rows")
    try:
        internal_digest = qualification.row_digest(internal)
        provider_digest = qualification.row_digest(provider)
        expected_oracle = qualification.oracle(snapshot)
    except Exception as exc:
        raise ReconciliationEvidenceError(f"reconciliation independent oracle failed: {exc}") from exc
    require(metadata["internal_snapshot_digest"] == internal_digest,
            "reconciliation internal snapshot digest mismatch")
    require(metadata["provider_snapshot_digest"] == provider_digest,
            "reconciliation provider snapshot digest mismatch")
    require(report["oracle"] == expected_oracle,
            "reconciliation retained oracle does not match independent recomputation")
    require(
        expected_oracle["business_state"] == "CLEAR"
        and expected_oracle["mismatch_count"] == 0
        and expected_oracle["aggregate_delta_minor"] == 0,
        "reconciliation campaign snapshot must independently converge CLEAR",
    )

    internal_ids = [row.get("provider_payment_id") for row in internal if type(row) is dict]
    provider_ids = [row.get("provider_payment_id") for row in provider if type(row) is dict]
    require(len(set(internal_ids)) == len(internal_ids),
            "reconciliation internal provider identity duplicated")
    require(len(set(provider_ids)) == len(provider_ids),
            "reconciliation provider identity duplicated")
    require(set(internal_ids) == set(provider_ids),
            "reconciliation CLEAR snapshot internal/provider membership differs")

    actual = qualification.normalize_actual(object_(report["actual"], "reconciliation actual result"))
    require(
        set(actual) == {
            "run_id", "processing_state", "business_state", "mismatch_count",
            "aggregate_delta_minor", "result_digest", "details", "published_count",
        },
        "reconciliation actual result fields mismatch",
    )
    require(isinstance(actual["run_id"], str) and actual["run_id"],
            "reconciliation run identity missing")
    require(actual["processing_state"] == "PROCESSED",
            "reconciliation processing must be PROCESSED")
    require(actual["business_state"] == "CLEAR",
            "reconciliation finalized business state must be CLEAR")
    require(actual["mismatch_count"] == 0 and actual["aggregate_delta_minor"] == 0,
            "reconciliation finalized mismatch/delta must be zero")
    require(actual["result_digest"] == expected_oracle["result_digest"],
            "reconciliation result digest mismatch")
    require(actual["details"] == expected_oracle["details"],
            "reconciliation detail rows differ from independent oracle")
    require(actual["published_count"] == 1,
            "reconciliation CLEAR result must retain exactly one publication")

    expected_crash = scheduled_hard_crash(plan)
    require(artifact["scheduled_hard_crash"] == expected_crash,
            "reconciliation scheduled-crash identity mismatch")
    crash_subject = _validate_crash(
        artifact["crash_evidence"],
        plan=plan,
        snapshot_id=snapshot_id,
    )

    return {
        "snapshot_id": snapshot_id,
        "run_id": actual["run_id"],
        "business_state": "CLEAR",
        "mismatch_count": 0,
        "aggregate_delta_minor": 0,
        "result_digest": actual["result_digest"],
        "internal_row_count": len(internal),
        "provider_row_count": len(provider),
        "internal_snapshot_digest": internal_digest,
        "provider_snapshot_digest": provider_digest,
        "published_count": 1,
        "scheduled_hard_crash": expected_crash,
        "crash_subject": crash_subject,
    }
