#!/usr/bin/env python3
"""#318 independent per-day payment-reconciliation proof.

This module binds one frozen payment-reconciliation-postgres snapshot/result to
an exact logical day, replays the retained v0.7.0 reconciliation oracle, and
returns a compact subject for the day frontier. Its producer CLI invokes only
the workload's read-only `verify` command against already-completed state; it
does not snapshot, run, recover, finalize, or refresh Merchant/provider state.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

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
    require(
        isinstance(campaign_id, str) and core.CAMPAIGN_ID.fullmatch(campaign_id) is not None,
        "reconciliation campaign id is unsafe",
    )
    require(type(sequence) is int and sequence > 0, "reconciliation day sequence invalid")
    # payment-reconciliation-postgres rejects ':' in snapshot identifiers.
    return f"{campaign_id}-day-{sequence:06}-reconciliation"


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
        "oxide_batch_version", "workload_contract", "workload_binary_sha256",
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
    require(artifact["workload_contract"] == WORKLOAD_CONTRACT,
            "reconciliation workload contract mismatch")
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

def build_artifact(
    *,
    plan: dict[str, Any],
    source_revision: str,
    workload_binary_sha256: str,
    producer_report: dict[str, Any],
    crash_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    artifact = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_revision,
        "day_plan_sha256": plan_digest(plan),
        "oxide_batch_version": OXIDE_BATCH_VERSION,
        "workload_contract": WORKLOAD_CONTRACT,
        "workload_binary_sha256": workload_binary_sha256,
        "workload_semantics_sha256": file_sha256(RECONCILIATION_VERIFIER),
        "producer_report": object_(producer_report, "reconciliation producer report"),
        "scheduled_hard_crash": scheduled_hard_crash(plan),
        "crash_evidence": crash_evidence,
    }
    validate(
        artifact,
        plan=plan,
        source_revision=source_revision,
        workload_binary_sha256=workload_binary_sha256,
    )
    return artifact


def load_campaign(campaign_dir: Path) -> tuple[str, str, str]:
    require(campaign_dir.is_dir() and not campaign_dir.is_symlink(), "unsafe campaign directory")
    record = object_(
        json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8")),
        "campaign",
    )
    require(
        record.get("schema") == "oxide-batch-workloads.payment-reference-campaign"
        and record.get("schema_version") == 1,
        "campaign schema mismatch",
    )
    require(record.get("contract") == core.contract()["contract"], "campaign contract identity mismatch")
    require(record.get("contract_sha256") == core.sha256(core.CONTRACT_PATH), "campaign contract digest mismatch")
    identity = object_(record.get("identity"), "campaign identity")
    campaign_id = identity.get("campaign_id")
    source_revision = identity.get("source_revision")
    binaries = object_(identity.get("batch_binaries"), "campaign batch binaries")
    binary = object_(binaries.get("payment-reconciliation-postgres"), "payment reconciliation binary identity")
    binary_sha = binary.get("sha256")
    require(
        campaign_id == campaign_dir.name
        and isinstance(campaign_id, str)
        and core.CAMPAIGN_ID.fullmatch(campaign_id) is not None,
        "campaign directory identity mismatch",
    )
    require(
        isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", source_revision) is not None,
        "campaign source revision invalid",
    )
    require(
        isinstance(binary_sha, str) and HEX64.fullmatch(binary_sha) is not None,
        "campaign reconciliation binary SHA invalid",
    )
    return campaign_id, source_revision, binary_sha


def verify_workload_binary(path: Path, expected_sha256: str) -> Path:
    require(path.is_file() and not path.is_symlink(), "reconciliation workload binary missing/unsafe")
    require(os.access(path, os.X_OK), "reconciliation workload binary is not executable")
    require(file_sha256(path) == expected_sha256, "reconciliation workload binary SHA mismatch")
    return path.resolve()


Runner = Callable[..., Any]


def load_producer_report(
    *,
    workload_binary: Path,
    database_url: str,
    snapshot_id: str,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    require(isinstance(database_url, str) and database_url.strip(), "reconciliation database URL missing")
    require(
        isinstance(snapshot_id, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+", snapshot_id) is not None,
        "reconciliation snapshot id is not workload-safe",
    )
    child_env = dict(os.environ)
    child_env["DATABASE_URL"] = database_url
    result = runner(
        [
            str(workload_binary),
            "verify",
            "--snapshot-id",
            snapshot_id,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=child_env,
    )
    require(result.returncode == 0, "reconciliation workload verify command failed")
    require(len(result.stdout.encode("utf-8")) <= 8_000_000, "reconciliation producer report is oversized")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReconciliationEvidenceError("reconciliation producer report is not valid JSON") from exc
    return object_(report, "reconciliation producer report")


def retain_immutable(path: Path, value: Any) -> str:
    require(not path.is_symlink(), "reconciliation output path must not be a symlink")
    raw = core.canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        require(path.is_file() and not path.is_symlink(), "existing reconciliation output is unsafe")
        require(path.read_bytes() == raw, "immutable reconciliation verification conflict")
        return "replay"
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return "created"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="#318 read-only producer for the retained day reconciliation proof"
    )
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--workload-binary", type=Path, required=True)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("PAYMENT_RECONCILIATION_DATABASE_URL", ""),
    )
    parser.add_argument("--crash-evidence", type=Path)
    args = parser.parse_args()
    try:
        require(type(args.day) is int and args.day > 0, "day must be positive")
        campaign_id, source_revision, binary_sha = load_campaign(args.campaign_dir)
        plan = day_cardinality.plan_for(campaign_id, args.day)
        workload_binary = verify_workload_binary(args.workload_binary, binary_sha)
        snapshot_id = expected_snapshot_id(campaign_id, args.day)

        crash = None
        if args.crash_evidence is not None:
            require(
                args.crash_evidence.is_file() and not args.crash_evidence.is_symlink(),
                "reconciliation crash evidence path unsafe/missing",
            )
            crash = object_(
                json.loads(args.crash_evidence.read_text(encoding="utf-8")),
                "reconciliation crash evidence",
            )

        report = load_producer_report(
            workload_binary=workload_binary,
            database_url=args.database_url,
            snapshot_id=snapshot_id,
        )
        artifact = build_artifact(
            plan=plan,
            source_revision=source_revision,
            workload_binary_sha256=binary_sha,
            producer_report=report,
            crash_evidence=crash,
        )
        output = (
            args.campaign_dir
            / "days"
            / f"day-{args.day:06}.reconciliation.verification.json"
        )
        retention = retain_immutable(output, artifact)
        subject = validate(
            artifact,
            plan=plan,
            source_revision=source_revision,
            workload_binary_sha256=binary_sha,
        )
        print(json.dumps({
            "decision": DECISION,
            "verification_path": str(output),
            "retention": retention,
            "snapshot_id": subject["snapshot_id"],
            "run_id": subject["run_id"],
            "published_count": subject["published_count"],
        }, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
