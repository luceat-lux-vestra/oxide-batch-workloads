#!/usr/bin/env python3
"""Run campaign #323 repeated allocator memory/quiescence evidence on a qualified ARM64 host."""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import allocator_registry
import allocator_telemetry_profile
import paired
import profile_manifest
import profile_smoke

SCHEMA = "oxide-batch-allocator-memory-quiescence/v1"
ALLOCATOR_SCHEMA = "oxide-batch-allocator-memory-quiescence-allocator/v1"
SAMPLE_SCHEMA = "oxide-batch-workloads.endurance-sample"
SAMPLE_SCHEMA_VERSION = 1

ROWS = 200_000
SEED = 20_260_904
CHUNK_SIZE = 1_000
DEFAULT_ITERATIONS = 10
MAX_ITERATIONS = 22
DEFAULT_SAMPLE_INTERVAL_MS = 50
DEFAULT_POST_TERMINAL_QUIESCENCE_MS = 1_000
MAX_POST_TERMINAL_QUIESCENCE_MS = 600_000
CANONICAL_TARGET = "aarch64-unknown-linux-gnu"

PROCESS_METRICS = (
    "rss_bytes",
    "pss_bytes",
    "smaps_rollup_rss_bytes",
    "private_clean_bytes",
    "private_dirty_bytes",
    "shared_clean_bytes",
    "shared_dirty_bytes",
    "swap_bytes",
    "minor_faults",
    "major_faults",
    "cpu_time_ticks",
    "voluntary_context_switches",
    "nonvoluntary_context_switches",
    "thread_count",
    "fd_count",
)
POSTGRES_METRICS = (
    "total_connections",
    "active_connections",
    "database_size_bytes",
)
METADATA_METRICS = (
    "job_instances",
    "job_executions",
    "step_executions",
    "job_instance_table_bytes",
    "job_execution_table_bytes",
    "step_execution_table_bytes",
)
FILESYSTEM_METRICS = (
    "total_bytes",
    "used_bytes",
    "available_bytes",
)


def fail(message: str) -> None:
    raise RuntimeError(message)


def validate_geometry(args: argparse.Namespace) -> None:
    if args.iterations < 2 or args.iterations > MAX_ITERATIONS:
        raise ValueError(f"iterations must be between 2 and {MAX_ITERATIONS}")
    if args.iterations % 2:
        raise ValueError("iterations must be even so cursor/paging counts remain balanced")
    if args.sample_interval_ms < 10 or args.sample_interval_ms > 60_000:
        raise ValueError("sample_interval_ms must be between 10 and 60000")
    if (
        args.post_terminal_quiescence_ms <= 0
        or args.post_terminal_quiescence_ms > MAX_POST_TERMINAL_QUIESCENCE_MS
    ):
        raise ValueError(
            "post_terminal_quiescence_ms must be between 1 and "
            f"{MAX_POST_TERMINAL_QUIESCENCE_MS}"
        )


def candidates_for_target(target: str) -> tuple[dict[str, Any], ...]:
    if target != CANONICAL_TARGET:
        fail(
            "campaign #323 memory/quiescence evidence requires exact target "
            f"{CANONICAL_TARGET}, got {target}"
        )
    candidates = allocator_registry.qualified_active_candidates(
        profile_smoke.ALLOCATOR_REGISTRY, target
    )
    if not candidates:
        fail(f"no active allocators are qualified for target {target}")
    return candidates


def selected_feature(candidate: dict[str, Any]) -> str | None:
    telemetry = candidate["telemetry"]
    if telemetry["status"] == "wired":
        feature = allocator_registry.telemetry_feature(candidate)
        if not feature:
            fail(f"wired allocator {candidate['identity']} is missing telemetry feature")
        return feature
    return candidate["cargo_feature"]


def build_args(candidate: dict[str, Any]) -> list[str]:
    command = [
        "cargo",
        "build",
        "--locked",
        "--release",
        "-p",
        "postgres-postgres",
        "--no-default-features",
        "--bin",
        "postgres-postgres",
        "--bin",
        "postgres-postgres-endurancectl",
    ]
    feature = selected_feature(candidate)
    if feature:
        command.extend(["--features", feature])
    return command


def campaign_id(token: str, allocator: str) -> str:
    safe = f"allocmem-{token}-{allocator}".replace("_", "-")
    value = safe[:48]
    if not value or not value[0].isalnum():
        fail(f"unsafe generated campaign id: {value!r}")
    if any(not (char.isalnum() or char == "-") for char in value):
        fail(f"unsafe generated campaign id: {value!r}")
    return value


def _metric_stats(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"observed_count": 0, "min": None, "max": None}
    return {"observed_count": len(values), "min": min(values), "max": max(values)}


def _require_non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        fail(f"{field} must be a non-negative integer")
    return value


def load_samples(
    path: Path,
    *,
    expected_campaign_id: str,
    expected_sequences: set[int],
) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size <= 0:
        fail("samples.jsonl is missing or empty")

    records: list[dict[str, Any]] = []
    previous_timestamp = -1
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"samples.jsonl line {line_number} is not valid JSON"
                ) from exc
            if not isinstance(record, dict):
                fail(f"samples.jsonl line {line_number} must be an object")
            if record.get("schema") != SAMPLE_SCHEMA:
                fail(f"unexpected sample schema at line {line_number}")
            if record.get("schema_version") != SAMPLE_SCHEMA_VERSION:
                fail(f"unexpected sample schema version at line {line_number}")
            if record.get("campaign_id") != expected_campaign_id:
                fail(f"sample campaign id mismatch at line {line_number}")
            sequence = record.get("sequence")
            if sequence not in expected_sequences:
                fail(f"unexpected sample sequence at line {line_number}: {sequence!r}")
            timestamp = record.get("timestamp_unix_nanos_utc")
            if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp <= 0:
                fail(f"invalid sample timestamp at line {line_number}")
            if timestamp < previous_timestamp:
                fail(f"sample timestamps regress at line {line_number}")
            previous_timestamp = timestamp
            records.append(record)

    if not records:
        fail("samples.jsonl contains no records")
    observed_sequences = {record["sequence"] for record in records}
    if observed_sequences != expected_sequences:
        fail(
            "sample sequence coverage mismatch: "
            f"expected {sorted(expected_sequences)}, observed {sorted(observed_sequences)}"
        )
    return records


def _group_stats(
    records: list[dict[str, Any]],
    group: str,
    metrics: tuple[str, ...],
) -> dict[str, dict[str, int | None]]:
    result: dict[str, dict[str, int | None]] = {}
    for metric in metrics:
        values: list[int] = []
        for record in records:
            obj = record.get(group)
            if not isinstance(obj, dict):
                fail(f"sample group {group} must be an object")
            values.append(_require_non_negative_int(obj.get(metric), f"{group}.{metric}"))
        result[metric] = _metric_stats(values)
    return result


def summarize_sequence(records: list[dict[str, Any]], sequence: int) -> dict[str, Any]:
    selected = [record for record in records if record["sequence"] == sequence]
    if not selected:
        fail(f"sequence {sequence} has no resource samples")

    kinds = [record.get("sample_kind") for record in selected]
    required = ("terminal-success", "quiescence-start", "quiescence-end")
    positions: dict[str, int] = {}
    for kind in required:
        try:
            positions[kind] = kinds.index(kind)
        except ValueError as exc:
            raise RuntimeError(f"sequence {sequence} is missing {kind}") from exc
    if not (
        positions["terminal-success"]
        < positions["quiescence-start"]
        < positions["quiescence-end"]
    ):
        fail(f"sequence {sequence} quiescence boundaries are out of order")

    quiescence_records = selected[
        positions["quiescence-start"] : positions["quiescence-end"] + 1
    ]
    if any(record.get("process") is not None for record in quiescence_records):
        fail(f"sequence {sequence} quiescence samples must not retain a process")

    by_role: dict[str, dict[str, list[int]]] = {}
    availability_reasons: collections.Counter[str] = collections.Counter()
    workload_pids: set[int] = set()
    for record in selected:
        process = record.get("process")
        if process is None:
            continue
        if not isinstance(process, dict):
            fail(f"sequence {sequence} process must be object or null")
        role = process.get("role")
        if not isinstance(role, str) or not role:
            fail(f"sequence {sequence} process role must be non-empty")
        pid = _require_non_negative_int(process.get("pid"), "process.pid")
        if role == "clean-workload":
            workload_pids.add(pid)
        bucket = by_role.setdefault(role, {metric: [] for metric in PROCESS_METRICS})
        for metric in PROCESS_METRICS:
            value = process.get(metric)
            if value is None:
                continue
            bucket[metric].append(_require_non_negative_int(value, f"process.{metric}"))
        for field in (
            "unavailable_reason",
            "smaps_rollup_unavailable_reason",
            "fd_count_unavailable_reason",
        ):
            reason = process.get(field)
            if reason is not None:
                if not isinstance(reason, str) or not reason:
                    fail(f"{field} must be null or a non-empty string")
                availability_reasons[f"{field}:{reason}"] += 1

    if not workload_pids:
        fail(f"sequence {sequence} contains no clean-workload process observation")

    role_summary = {
        role: {metric: _metric_stats(values) for metric, values in sorted(metrics.items())}
        for role, metrics in sorted(by_role.items())
    }

    quiescence_end = selected[positions["quiescence-end"]]
    return {
        "sequence": sequence,
        "reader": "cursor" if sequence % 2 else "paging",
        "sample_count": len(selected),
        "quiescence_periodic_count": sum(
            1 for record in quiescence_records if record.get("sample_kind") == "quiescence-periodic"
        ),
        "workload_process_ids": sorted(workload_pids),
        "roles": role_summary,
        "availability_reasons": dict(sorted(availability_reasons.items())),
        "postgres": _group_stats(selected, "postgres", POSTGRES_METRICS),
        "oxide_batch": _group_stats(selected, "oxide_batch", METADATA_METRICS),
        "filesystem": _group_stats(selected, "filesystem", FILESYSTEM_METRICS),
        "quiescence_end": {
            "postgres": quiescence_end["postgres"],
            "oxide_batch": quiescence_end["oxide_batch"],
            "filesystem": quiescence_end["filesystem"],
        },
    }


def summarize_samples(
    path: Path,
    *,
    expected_campaign_id: str,
    iterations: int,
) -> dict[str, Any]:
    expected_sequences = set(range(1, iterations + 1))
    records = load_samples(
        path,
        expected_campaign_id=expected_campaign_id,
        expected_sequences=expected_sequences,
    )
    return {
        "schema": ALLOCATOR_SCHEMA,
        "campaign_id": expected_campaign_id,
        "sample_schema": SAMPLE_SCHEMA,
        "sample_schema_version": SAMPLE_SCHEMA_VERSION,
        "sample_count": len(records),
        "sequences": [
            summarize_sequence(records, sequence)
            for sequence in range(1, iterations + 1)
        ],
    }


def validate_iteration(
    iteration_path: Path,
    *,
    sequence: int,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    if not iteration_path.is_file():
        fail(f"missing retained iteration evidence: {iteration_path}")
    iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
    if iteration.get("sequence") != sequence or iteration.get("terminal") != "success":
        fail(f"sequence {sequence} retained iteration is not terminal success")
    execution = iteration.get("execution")
    if not isinstance(execution, dict) or execution.get("kind") != "clean":
        fail(f"sequence {sequence} must remain a clean execution")
    result = execution.get("result")
    if not isinstance(result, dict):
        fail(f"sequence {sequence} clean execution result must be an object")
    expected_reader = "cursor" if sequence % 2 else "paging"
    if result.get("reader") != expected_reader:
        fail(f"sequence {sequence} reader mismatch")
    if result.get("chunk_size") != CHUNK_SIZE:
        fail(f"sequence {sequence} chunk size mismatch")
    if expected_reader == "cursor":
        if result.get("fetch_size") != 500 or result.get("page_size") is not None:
            fail(f"sequence {sequence} cursor geometry mismatch")
    else:
        if result.get("page_size") != 750 or result.get("fetch_size") is not None:
            fail(f"sequence {sequence} paging geometry mismatch")
    if result.get("cleanup_deleted_rows") != ROWS:
        fail(f"sequence {sequence} cleanup row count mismatch")
    run = result.get("run")
    verification = result.get("verification")
    if not isinstance(run, dict) or run.get("success") is not True:
        fail(f"sequence {sequence} workload child was not successful")
    if not isinstance(verification, dict) or verification.get("success") is not True:
        fail(f"sequence {sequence} verifier child was not successful")
    verifier = paired.parse_verify_report(
        json.dumps(result.get("verification_report")), ROWS
    )
    pid = _require_non_negative_int(run.get("pid"), "iteration.execution.result.run.pid")
    return iteration, verifier, pid


def redact_secret(text: str, *secrets: str) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED_DATABASE_URL]")
    return result


def run_controller(
    controller: Path,
    *,
    env: dict[str, str],
    evidence_root: Path,
    selected_campaign: str,
    sample_interval_ms: int,
    post_terminal_quiescence_ms: int,
) -> tuple[int, subprocess.CompletedProcess[str]]:
    process = subprocess.Popen(
        [
            str(controller),
            "run-next",
            "--evidence-root",
            str(evidence_root),
            "--campaign-id",
            selected_campaign,
            "--sample-interval-ms",
            str(sample_interval_ms),
            "--post-terminal-quiescence-ms",
            str(post_terminal_quiescence_ms),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = process.communicate()
    completed = subprocess.CompletedProcess(
        args=process.args,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return process.pid, completed


def build_and_retain_binaries(
    candidate: dict[str, Any],
    *,
    target: str,
    allocator_root: Path,
) -> tuple[Path, Path]:
    identity = candidate["identity"]
    state = allocator_registry.target_state(candidate, target)
    if state != "qualified":
        fail(f"allocator {identity} is not qualified for target {target}: {state}")
    profile_smoke.run_checked(build_args(candidate))
    built_workload = Path("target/release/postgres-postgres")
    built_controller = Path("target/release/postgres-postgres-endurancectl")
    if not built_workload.is_file() or not built_controller.is_file():
        fail(f"allocator {identity} build did not produce both required binaries")

    binary_root = allocator_root / "binary"
    binary_root.mkdir(parents=True, exist_ok=True)
    workload = binary_root / "postgres-postgres"
    controller = binary_root / "postgres-postgres-endurancectl"
    shutil.copy2(built_workload, workload)
    shutil.copy2(built_controller, controller)
    workload.chmod(0o755)
    controller.chmod(0o755)
    return workload.resolve(), controller.resolve()


def run_allocator(
    *,
    candidate: dict[str, Any],
    args: argparse.Namespace,
    target: str,
    token: str,
    base_env: dict[str, str],
) -> dict[str, Any]:
    identity = candidate["identity"]
    allocator_root = args.output / identity
    allocator_root.mkdir(parents=True, exist_ok=True)
    workload, controller = build_and_retain_binaries(
        candidate, target=target, allocator_root=allocator_root
    )

    selected_campaign = campaign_id(token, identity)
    database = profile_smoke.database_name(token, f"allocmem_{identity}")
    paired.compose_db("dropdb", database)
    paired.compose_db("createdb", database, template="template0")
    database_url = paired.database_url(args.base_database_url, database)

    env = base_env.copy()
    env["DATABASE_URL"] = database_url
    env["RUST_LOG"] = "error"

    evidence_root = allocator_root / "memory" / "endurance"
    logs_root = allocator_root / "controller"
    native_root = allocator_root / "allocator-native"
    correctness_root = allocator_root / "correctness"
    projection_root = allocator_root / "projection"
    provenance_root = allocator_root / "provenance"
    for root in (
        evidence_root,
        logs_root,
        native_root,
        correctness_root,
        projection_root,
        provenance_root,
    ):
        root.mkdir(parents=True, exist_ok=True)

    telemetry_status = candidate["telemetry"]["status"]
    source_digests: set[str] = set()
    controller_pids: list[int] = []
    workload_pids: list[int] = []
    snapshots: list[dict[str, Any]] = []

    try:
        profile_smoke.run_checked([str(workload), "migrate"], env=env)
        profile_smoke.run_checked(
            [str(workload), "seed", "--rows", str(ROWS), "--seed", str(SEED)],
            env=env,
        )

        for sequence in range(1, args.iterations + 1):
            run_env = env.copy()
            snapshot_path: Path | None = None
            if telemetry_status == "wired":
                sequence_native_root = native_root / f"{sequence:06}"
                sequence_native_root.mkdir(parents=True, exist_ok=True)
                run_env[allocator_telemetry_profile.TELEMETRY_DIR_ENV] = str(
                    sequence_native_root.resolve()
                )
                snapshot_path = sequence_native_root / "snapshot.json"
            else:
                run_env.pop(allocator_telemetry_profile.TELEMETRY_DIR_ENV, None)

            controller_pid, completed = run_controller(
                controller,
                env=run_env,
                evidence_root=evidence_root,
                selected_campaign=selected_campaign,
                sample_interval_ms=args.sample_interval_ms,
                post_terminal_quiescence_ms=args.post_terminal_quiescence_ms,
            )
            controller_pids.append(controller_pid)
            stdout_path = logs_root / f"{sequence:06}.stdout.json"
            stderr_path = logs_root / f"{sequence:06}.stderr.txt"
            safe_stdout = redact_secret(
                completed.stdout, database_url, args.base_database_url
            )
            safe_stderr = redact_secret(
                completed.stderr, database_url, args.base_database_url
            )
            stdout_path.write_text(safe_stdout, encoding="utf-8")
            stderr_path.write_text(safe_stderr, encoding="utf-8")
            if completed.returncode != 0:
                fail(
                    f"allocator {identity} sequence {sequence} controller exited "
                    f"{completed.returncode}: {safe_stderr[-2000:]}"
                )

            try:
                report = json.loads(safe_stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"allocator {identity} sequence {sequence} controller stdout is not JSON"
                ) from exc
            if not isinstance(report, dict):
                fail(f"allocator {identity} sequence {sequence} report must be an object")
            if report.get("selected_sequence") != sequence:
                fail(f"allocator {identity} sequence {sequence} selection mismatch")
            plan = report.get("selected_plan")
            if not isinstance(plan, dict) or plan.get("failure") != "clean":
                fail(f"allocator {identity} sequence {sequence} must select a clean plan")

            iteration_path = (
                evidence_root
                / selected_campaign
                / "iterations"
                / f"{sequence:06}.json"
            )
            _, verifier, workload_pid = validate_iteration(
                iteration_path, sequence=sequence
            )
            workload_pids.append(workload_pid)
            source_digest = verifier.get("source_digest")
            if not isinstance(source_digest, str) or not source_digest:
                fail(f"allocator {identity} sequence {sequence} verifier lacks source digest")
            source_digests.add(source_digest)
            profile_smoke.write_json(
                correctness_root / f"{sequence:06}.json", verifier
            )

            if telemetry_status == "wired":
                assert snapshot_path is not None
                snapshot = allocator_telemetry_profile.validate_native_snapshot(
                    snapshot_path, identity
                )
                snapshots.append(
                    {
                        "sequence": sequence,
                        "path": str(snapshot_path.relative_to(allocator_root)),
                        "sha256": profile_smoke.sha256_file(snapshot_path),
                        "native_api": snapshot["native_api"],
                    }
                )

        if len(source_digests) != 1:
            fail(
                f"allocator {identity} observed multiple dataset identities: "
                f"{sorted(source_digests)}"
            )

        samples_path = evidence_root / selected_campaign / "samples.jsonl"
        projection = summarize_samples(
            samples_path,
            expected_campaign_id=selected_campaign,
            iterations=args.iterations,
        )
        projection.update(
            {
                "allocator": identity,
                "controller_invocations": args.iterations,
                "controller_process_ids": controller_pids,
                "controller_process_id_distinct_count": len(set(controller_pids)),
                "workload_process_ids": workload_pids,
                "workload_process_id_distinct_count": len(set(workload_pids)),
                "telemetry_status": telemetry_status,
                "native_snapshots": snapshots,
            }
        )
        projection_path = projection_root / "summary.json"
        profile_smoke.write_json(projection_path, projection)

        retained_lock = provenance_root / "Cargo.lock"
        retained_registry = provenance_root / "allocator_registry.json"
        retained_candidate = provenance_root / "allocator.json"
        shutil.copy2(Path("Cargo.lock"), retained_lock)
        shutil.copy2(allocator_registry.DEFAULT_PATH, retained_registry)
        profile_smoke.write_json(retained_candidate, candidate)

        return {
            "schema": ALLOCATOR_SCHEMA,
            "allocator": identity,
            "campaign_id": selected_campaign,
            "correctness": "PASS",
            "iterations": args.iterations,
            "cursor_iterations": args.iterations // 2,
            "paging_iterations": args.iterations // 2,
            "dataset_identity": next(iter(source_digests)),
            "telemetry": {
                "status": telemetry_status,
                "capabilities": candidate["telemetry"]["capabilities"],
                "reason": candidate["telemetry"].get("reason"),
                "native_snapshot_count": len(snapshots),
            },
            "projection": str(projection_path.relative_to(args.output)),
            "projection_sha256": profile_smoke.sha256_file(projection_path),
            "samples": str(samples_path.relative_to(args.output)),
            "samples_sha256": profile_smoke.sha256_file(samples_path),
            "workload_binary_sha256": profile_smoke.sha256_file(workload),
            "controller_binary_sha256": profile_smoke.sha256_file(controller),
        }
    finally:
        paired.compose_db("dropdb", database)


def run_campaign(args: argparse.Namespace) -> dict[str, Any]:
    validate_geometry(args)
    profile_smoke.prepare_output_root(args.output)

    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    candidates = candidates_for_target(target)
    profile_smoke.validate_allocator_lock(Path("Cargo.lock"), target)
    oxide_subject = paired.lock_package(Path("Cargo.lock"), "oxide-batch", "0.7.0")
    subject_sha = profile_smoke.workload_sha()
    token = profile_smoke.run_token()
    base_env = os.environ.copy()

    allocator_results = [
        run_allocator(
            candidate=candidate,
            args=args,
            target=target,
            token=token,
            base_env=base_env,
        )
        for candidate in candidates
    ]
    dataset_identities = {
        result["dataset_identity"] for result in allocator_results
    }
    if len(dataset_identities) != 1:
        fail(
            "allocator campaign did not retain one deterministic dataset identity: "
            f"{sorted(dataset_identities)}"
        )

    summary = {
        "schema": SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "claim": "same-host-repeated-memory-quiescence-observation",
        "subject": {
            "workload_sha": subject_sha,
            "cargo_lock_sha256": profile_smoke.sha256_file(Path("Cargo.lock")),
            "rustc": rustc_version,
            "cargo": cargo_version,
            "target": target,
            "oxide_batch": oxide_subject,
        },
        "environment": environment,
        "geometry": {
            "rows": ROWS,
            "seed": SEED,
            "chunk_size": CHUNK_SIZE,
            "cursor_fetch_size": 500,
            "paging_page_size": 750,
            "iterations_per_allocator": args.iterations,
            "sample_interval_ms": args.sample_interval_ms,
            "post_terminal_quiescence_ms": args.post_terminal_quiescence_ms,
        },
        "dataset_identity": next(iter(dataset_identities)),
        "allocators": allocator_results,
        "limitations": [
            "this is memory/quiescence evidence, not benchmark timing",
            "process-local allocator state ends when each workload child exits",
            "PostgreSQL, OxideBatch metadata, and filesystem state persist across the explicit idle interval",
            "allocator-native payloads retain native semantics and are not normalized into a synthetic fragmentation score",
            "system and tcmalloc remain explicitly not-wired for allocator-native numeric telemetry in the current registry",
            "rising RSS alone is not a leak verdict",
            "OxideBatch 0.7.0 observations must not be generalized to later releases",
        ],
    }
    profile_smoke.write_json(args.output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument(
        "--sample-interval-ms",
        type=int,
        default=DEFAULT_SAMPLE_INTERVAL_MS,
    )
    parser.add_argument(
        "--post-terminal-quiescence-ms",
        type=int,
        default=DEFAULT_POST_TERMINAL_QUIESCENCE_MS,
    )
    args = parser.parse_args()
    summary = run_campaign(args)
    print(
        json.dumps(
            {
                "valid": True,
                "schema": summary["schema"],
                "campaign": summary["campaign"],
                "allocators": [
                    result["allocator"] for result in summary["allocators"]
                ],
                "dataset_identity": summary["dataset_identity"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
