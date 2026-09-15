#!/usr/bin/env python3
"""Canonical retained-evidence verifier for postgres-postgres Track F (#98).

The campaign extends the pre-existing bounded-resource evidence with three
external-process observation records.  The older canonical verifier remains
a dependency, but this wrapper pins its exact SHA-256 before importing it so a
future edit cannot silently weaken previously accepted evidence.

Output contract: {"schema_version": 1, "violations": [...]}; nonzero exit when
violations is non-empty.
"""

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
CAMPAIGN_ISSUE = 98
PRODUCER_SHA = "3df532e66cc28a1ed350901f06cf80b9de6c92f5"
PRODUCER_RUN_ID = 34394363154
UPSTREAM_GAP = "luceat-lux-vestra/oxide-batch#269"
LEGACY_VERIFIER = "verify-retained-evidence.py"
LEGACY_VERIFIER_SHA256 = "810131c062905a20d806b3038278881d508477e0b47c1ee7d3408c1c600afcd0"

LEGACY_SCENARIOS = {
    "cursor_bounded_resource_run",
    "paging_bounded_resource_run",
}
TRACK_F_SCENARIOS = {
    "track_f_process_interop",
    "track_f_process_interop_closure",
    "track_f_process_identity",
}
REQUIRED_SCENARIOS = LEGACY_SCENARIOS | TRACK_F_SCENARIOS


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def field(record: dict, *parts: str):
    value = record
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def add(violations: list[str], condition: bool, message: str) -> None:
    if not condition:
        violations.append(message)


def load_legacy_verifier(validation_dir: Path):
    path = validation_dir / LEGACY_VERIFIER
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read pinned legacy verifier {path}: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != LEGACY_VERIFIER_SHA256:
        raise ValueError(
            "pinned legacy verifier identity mismatch: "
            f"expected {LEGACY_VERIFIER_SHA256}, got {actual}"
        )
    spec = importlib.util.spec_from_file_location("postgres_postgres_legacy_evidence_verifier", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load pinned legacy verifier {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_legacy_subset(manifest: dict, manifest_path: Path) -> list[str]:
    records = manifest.get("records")
    if not isinstance(records, list):
        return ["manifest records must be an array"]
    legacy_records = [
        item
        for item in records
        if isinstance(item, dict) and item.get("scenario") in LEGACY_SCENARIOS
    ]
    validation_dir = manifest_path.parent
    legacy = load_legacy_verifier(validation_dir)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".legacy-manifest.json",
            dir=validation_dir,
            delete=False,
        ) as handle:
            json.dump({"records": legacy_records}, handle)
            temporary = Path(handle.name)
        return list(legacy.verify(temporary))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_track_artifact(
    root: Path, scenario: str, manifest_record: dict, violations: list[str]
) -> dict | None:
    artifact = manifest_record.get("artifact")
    path_value = artifact.get("path") if isinstance(artifact, dict) else None
    if not isinstance(path_value, str):
        violations.append(f"{scenario}: artifact path is missing")
        return None
    try:
        return load_json(root / path_value)
    except ValueError as exc:
        violations.append(f"{scenario}: {exc}")
        return None


def verify_manifest_binding(
    manifest: dict, record_by_scenario: dict[str, dict], violations: list[str]
) -> None:
    producer_sha = field(manifest, "producer", "base_revision")
    add(
        violations,
        producer_sha == PRODUCER_SHA,
        f"producer.base_revision must be the exact Track F producer checkout {PRODUCER_SHA}",
    )
    run_identity = field(manifest, "producer", "run", "identity")
    add(
        violations,
        isinstance(run_identity, str) and f"/actions/runs/{PRODUCER_RUN_ID}" in run_identity,
        f"producer.run.identity must bind GitHub Actions run {PRODUCER_RUN_ID}",
    )

    expected_ref = (
        "https://github.com/luceat-lux-vestra/oxide-batch-workloads/commit/" + PRODUCER_SHA
    )
    expected_targets = {
        "track_f_process_interop": "process_interop",
        "track_f_process_interop_closure": "process_interop_closure",
        "track_f_process_identity": "process_interop_identity",
    }
    for scenario, target in expected_targets.items():
        record = record_by_scenario[scenario]
        add(
            violations,
            field(record, "input", "identity", "reference") == expected_ref,
            f"{scenario}: input identity must bind the exact producer checkout",
        )
        add(
            violations,
            field(record, "input", "reproduction", "test_target") == target,
            f"{scenario}: reproduction.test_target must be {target}",
        )
        add(
            violations,
            field(record, "parameters", "campaign_issue") == CAMPAIGN_ISSUE,
            f"{scenario}: parameters.campaign_issue must be {CAMPAIGN_ISSUE}",
        )
        add(
            violations,
            field(record, "parameters", "github_actions_run_id") == PRODUCER_RUN_ID,
            f"{scenario}: parameters.github_actions_run_id must be {PRODUCER_RUN_ID}",
        )


def verify_process_interop(value: dict, violations: list[str]) -> None:
    prefix = "track_f_process_interop"
    add(
        violations,
        value.get("schema") == "oxide-batch-workloads.process-interop-observation"
        and value.get("schema_version") == 1,
        f"{prefix}: schema identity mismatch",
    )
    add(violations, value.get("campaign_issue") == CAMPAIGN_ISSUE, f"{prefix}: campaign_issue mismatch")
    add(violations, value.get("producer_checkout") == PRODUCER_SHA, f"{prefix}: producer_checkout mismatch")
    add(
        violations,
        value.get("validation_subject")
        == {"crate": "oxide-batch", "source": "crates.io", "version": "0.6.0"},
        f"{prefix}: validation subject must be exact published oxide-batch 0.6.0 from crates.io",
    )

    validation = value.get("validation_rejection") or {}
    add(
        violations,
        validation.get("process_exit_code") == 2 and validation.get("durable_execution_created") is False,
        f"{prefix}: validation rejection must be process-visible and create no durable execution",
    )
    infrastructure = value.get("infrastructure_failure") or {}
    add(
        violations,
        isinstance(infrastructure.get("process_exit_code"), int)
        and infrastructure.get("process_exit_code") != 0
        and infrastructure.get("durable_execution_observable_in_target_repository") is False,
        f"{prefix}: pre-repository infrastructure failure must be nonzero with no target durable execution",
    )
    typed = value.get("typed_failure") or {}
    add(
        violations,
        typed.get("process_exit_code") == 1
        and typed.get("durable_status") == "FAILED"
        and typed.get("durable_commits") == 2
        and typed.get("durable_position") == 200
        and typed.get("plain_restart_supported") is True,
        f"{prefix}: typed failure/restart primitives do not match the observed released contract",
    )

    duplicate = value.get("active_duplicate_lost_response_retry") or {}
    primary_pid = duplicate.get("primary_pid")
    different_pid = duplicate.get("different_identity_pid")
    add(
        violations,
        duplicate.get("duplicate_process_exit_code") == 1
        and duplicate.get("job_instances_after_retry") == 1
        and duplicate.get("job_executions_after_retry") == 1
        and duplicate.get("durable_business_rows_after_retry") == 200
        and duplicate.get("different_identity_completed") is True
        and positive_int(primary_pid)
        and positive_int(different_pid)
        and primary_pid != different_pid,
        f"{prefix}: duplicate/lost-response retry or distinct-identity primitive relationship mismatch",
    )
    completed = value.get("completed_redelivery") or {}
    add(
        violations,
        completed.get("process_exit_code") == 1 and completed.get("new_execution_created") is False,
        f"{prefix}: completed redelivery must fail closed without a new execution",
    )

    sigkill = value.get("sigkill") or {}
    add(
        violations,
        sigkill.get("signal") == 9
        and sigkill.get("durable_status_before_recovery") == "STARTED"
        and sigkill.get("durable_commits_before_recovery") == 2
        and sigkill.get("durable_position_before_recovery") == 200
        and sigkill.get("final_exit_code") == "COMPLETED"
        and sigkill.get("final_status") == "COMPLETED"
        and sigkill.get("clean_recovered_content_digest_equal") is True
        and positive_int(sigkill.get("old_pid"))
        and positive_int(sigkill.get("new_process_pid"))
        and sigkill.get("old_pid") != sigkill.get("new_process_pid"),
        f"{prefix}: SIGKILL recovery primitives are inconsistent",
    )
    sigterm = value.get("sigterm") or {}
    add(
        violations,
        sigterm.get("signal") == 15
        and sigterm.get("graceful_signal_contract_observed") is False
        and sigterm.get("durable_status_before_recovery") == "STARTED"
        and sigterm.get("durable_commits_before_recovery") == 2
        and sigterm.get("durable_position_before_recovery") == 200
        and sigterm.get("final_exit_code") == "COMPLETED"
        and sigterm.get("final_status") == "COMPLETED"
        and positive_int(sigterm.get("old_pid"))
        and positive_int(sigterm.get("new_process_pid"))
        and sigterm.get("old_pid") != sigterm.get("new_process_pid"),
        f"{prefix}: SIGTERM observation/recovery primitives are inconsistent",
    )

    control = value.get("verifier_negative_control") or {}
    add(
        violations,
        control.get("deliberate_corruption_detected") is True
        and control.get("restored_state_verified") is True,
        f"{prefix}: verifier negative control did not prove corruption detection and restoration",
    )
    gaps = value.get("capability_gaps") or {}
    add(
        violations,
        isinstance(gaps.get("graceful_sigterm"), str)
        and gaps["graceful_sigterm"].startswith("UNSUPPORTED/GAP:"),
        f"{prefix}: graceful SIGTERM must remain classified UNSUPPORTED/GAP",
    )
    add(
        violations,
        isinstance(gaps.get("stop_cancel_process_contract"), str)
        and gaps["stop_cancel_process_contract"].startswith("UNSUPPORTED/GAP:"),
        f"{prefix}: stop/cancel process contract must remain classified UNSUPPORTED/GAP",
    )
    for key in ("dedicated_external_correlation_identity", "machine_readable_launch_result"):
        add(
            violations,
            isinstance(gaps.get(key), str) and gaps[key].startswith("NOT_PROVEN:"),
            f"{prefix}: {key} must remain classified NOT_PROVEN",
        )


def verify_process_closure(value: dict, violations: list[str]) -> None:
    prefix = "track_f_process_interop_closure"
    add(
        violations,
        value.get("schema") == "oxide-batch-workloads.process-interop-closure-observation"
        and value.get("schema_version") == 1,
        f"{prefix}: schema identity mismatch",
    )
    add(violations, value.get("campaign_issue") == CAMPAIGN_ISSUE, f"{prefix}: campaign_issue mismatch")
    add(violations, value.get("producer_checkout") == PRODUCER_SHA, f"{prefix}: producer_checkout mismatch")

    sigterm = value.get("sigterm") or {}
    continuation = value.get("continuation") or {}
    recovery = value.get("public_recovery") or {}
    add(
        violations,
        sigterm.get("signal") == 15
        and sigterm.get("numeric_exit_code") is None
        and sigterm.get("launcher_is_direct_parent") is True
        and sigterm.get("durable_status_before_recovery") == "STARTED"
        and sigterm.get("committed_chunks_before_recovery") == 1
        and sigterm.get("checkpoint_position_before_recovery") == 100
        and positive_int(sigterm.get("shutdown_latency_micros"))
        and positive_int(value.get("launcher_pid"))
        and positive_int(sigterm.get("workload_pid")),
        f"{prefix}: SIGTERM ownership/shutdown primitives are inconsistent",
    )
    add(
        violations,
        recovery.get("process_exit_code") == 0
        and recovery.get("recovered_status") == "FAILED"
        and recovery.get("active_job_executions_after_recovery") == 0
        and recovery.get("active_step_executions_after_recovery") == 1
        and recovery.get("step_lifecycle_closure") == "UNSUPPORTED/GAP"
        and recovery.get("upstream_issue") == UPSTREAM_GAP,
        f"{prefix}: public recovery must preserve the observed stale-StepExecution GAP",
    )
    add(
        violations,
        continuation.get("process_exit_code") == 0
        and continuation.get("status") == "COMPLETED"
        and continuation.get("attempt") == "2"
        and continuation.get("active_job_executions_final") == 0
        and continuation.get("active_step_executions_final") == 1
        and positive_int(continuation.get("workload_pid"))
        and continuation.get("workload_pid") != sigterm.get("workload_pid"),
        f"{prefix}: continuation must be a genuinely new completed process while stale step remains observable",
    )

    history = value.get("step_execution_history")
    history_ok = (
        isinstance(history, list)
        and len(history) == 2
        and isinstance(history[0], list)
        and isinstance(history[1], list)
        and len(history[0]) == 4
        and len(history[1]) == 4
        and history[0][1:] == ["STARTED", 1, 100]
        and history[1][1:] == ["COMPLETED", 4, 350]
        and history[0][0] == recovery.get("recovered_job_execution_id")
        and history[1][0] == continuation.get("job_execution_id")
        and history[0][0] != history[1][0]
    )
    add(violations, history_ok, f"{prefix}: StepExecution history/recovery lineage mismatch")

    gaps = value.get("capability_gaps") or {}
    add(
        violations,
        isinstance(gaps.get("recovery_step_lifecycle_closure"), str)
        and gaps["recovery_step_lifecycle_closure"].startswith("UNSUPPORTED/GAP:")
        and "oxide-batch#269" in gaps["recovery_step_lifecycle_closure"],
        f"{prefix}: recovery lifecycle closure finding must remain UNSUPPORTED/GAP and bind upstream #269",
    )


def verify_process_identity(value: dict, violations: list[str]) -> None:
    prefix = "track_f_process_identity"
    add(
        violations,
        value.get("schema") == "oxide-batch-workloads.process-interop-identity-observation"
        and value.get("schema_version") == 1,
        f"{prefix}: schema identity mismatch",
    )
    add(violations, value.get("campaign_issue") == CAMPAIGN_ISSUE, f"{prefix}: campaign_issue mismatch")
    add(violations, value.get("producer_checkout") == PRODUCER_SHA, f"{prefix}: producer_checkout mismatch")
    initial = value.get("initial_execution") or {}
    continuation = value.get("recovery_continuation") or {}
    add(
        violations,
        initial.get("attempt") == "1"
        and initial.get("termination_signal") == 9
        and initial.get("durable_status_before_kill") == "STARTED"
        and initial.get("durable_status_after_kill") == "STARTED"
        and positive_int(initial.get("workload_pid"))
        and positive_int(value.get("launcher", {}).get("pid")),
        f"{prefix}: initial external execution identity primitives are inconsistent",
    )
    add(
        violations,
        continuation.get("attempt") == "2"
        and continuation.get("process_exit_code") == 0
        and continuation.get("durable_status") == "COMPLETED"
        and positive_int(continuation.get("workload_pid"))
        and continuation.get("workload_pid") != initial.get("workload_pid")
        and continuation.get("job_instance_id") == initial.get("job_instance_id")
        and continuation.get("job_execution_id") != initial.get("job_execution_id"),
        f"{prefix}: recovery must preserve JobInstance identity while using a new process and JobExecution",
    )


def verify(manifest_path: Path) -> list[str]:
    violations: list[str] = []
    manifest = load_json(manifest_path)
    records = manifest.get("records")
    if not isinstance(records, list):
        return ["manifest records must be an array"]
    record_by_scenario = {
        item.get("scenario"): item
        for item in records
        if isinstance(item, dict) and isinstance(item.get("scenario"), str)
    }
    if set(record_by_scenario) != REQUIRED_SCENARIOS:
        return [
            "postgres-postgres Track F evidence must contain exactly: "
            + ", ".join(sorted(REQUIRED_SCENARIOS))
        ]

    violations.extend(verify_legacy_subset(manifest, manifest_path))
    verify_manifest_binding(manifest, record_by_scenario, violations)

    root = manifest_path.parent.parent
    loaded = {
        scenario: load_track_artifact(root, scenario, record_by_scenario[scenario], violations)
        for scenario in TRACK_F_SCENARIOS
    }
    if any(value is None for value in loaded.values()):
        return violations
    verify_process_interop(loaded["track_f_process_interop"], violations)
    verify_process_closure(loaded["track_f_process_interop_closure"], violations)
    verify_process_identity(loaded["track_f_process_identity"], violations)
    return violations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        violations = verify(args.manifest)
    except ValueError as exc:
        violations = [str(exc)]
    print(json.dumps({"schema_version": SCHEMA_VERSION, "violations": violations}, sort_keys=True))
    raise SystemExit(0 if not violations else 1)


if __name__ == "__main__":
    main()
