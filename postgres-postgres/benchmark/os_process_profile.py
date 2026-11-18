#!/usr/bin/env python3
"""Produce retained campaign #266 OS/process telemetry evidence from the #203 sampler."""

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
import paired
import profile_manifest
import profile_smoke

ALLOCATOR = "system"
RUN_SCHEMA = "oxide-batch-os-process-profile-run/v1"
SUMMARY_SCHEMA = "oxide-batch-os-process-summary/v1"
SAMPLE_SCHEMA = "oxide-batch-workloads.endurance-sample"
SAMPLE_SCHEMA_VERSION = 1

# Reuse #203's frozen workload semantics rather than introducing a second sampler contract.
ROWS = 200_000
SEED = 20_260_904
CHUNK_SIZE = 1_000
READER = "paging"
FETCH_SIZE = 500
PAGE_SIZE = 750
SEQUENCE = 2
DEFAULT_SAMPLE_INTERVAL_MS = 50

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
FILESYSTEM_METRICS = (
    "total_bytes",
    "used_bytes",
    "available_bytes",
)


def fail(message: str) -> None:
    raise RuntimeError(message)


def build_binaries(target: str) -> tuple[Path, Path]:
    candidate = allocator_registry.candidate_by_id(
        profile_smoke.ALLOCATOR_REGISTRY, ALLOCATOR
    )
    if allocator_registry.target_state(candidate, target) != "qualified":
        fail(f"system allocator is not qualified for target {target}")

    profile_smoke.run_checked(
        [
            "cargo",
            "build",
            "--locked",
            "--release",
            "-p",
            "postgres-postgres",
            "--bin",
            "postgres-postgres",
            "--bin",
            "postgres-postgres-endurancectl",
        ]
    )
    workload = Path("target/release/postgres-postgres")
    controller = Path("target/release/postgres-postgres-endurancectl")
    if not workload.is_file() or not controller.is_file():
        fail("release build did not produce workload and endurance controller binaries")
    return workload.resolve(), controller.resolve()


def campaign_id(token: str) -> str:
    safe = token.replace("_", "-")
    value = f"lanef-{safe}"[:48]
    if not value or not value[0].isalnum():
        fail(f"unsafe generated campaign id: {value!r}")
    if any(not (char.isalnum() or char == "-") for char in value):
        fail(f"unsafe generated campaign id: {value!r}")
    return value


def validate_verifier_report(report: Any) -> dict[str, Any]:
    return paired.parse_verify_report(json.dumps(report), ROWS)


def _metric_stats(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"observed_count": 0, "min": None, "max": None}
    return {
        "observed_count": len(values),
        "min": min(values),
        "max": max(values),
    }


def summarize_samples(path: Path, expected_campaign_id: str) -> dict[str, Any]:
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
            if record.get("sequence") != SEQUENCE:
                fail(f"sample sequence mismatch at line {line_number}")
            timestamp = record.get("timestamp_unix_nanos_utc")
            if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp <= 0:
                fail(f"invalid sample timestamp at line {line_number}")
            if timestamp < previous_timestamp:
                fail(f"sample timestamps regress at line {line_number}")
            previous_timestamp = timestamp
            records.append(record)

    if not records:
        fail("samples.jsonl contains no records")

    by_role: dict[str, dict[str, Any]] = {}
    process_records = 0
    availability_reasons: collections.Counter[str] = collections.Counter()

    for record in records:
        process = record.get("process")
        if not isinstance(process, dict):
            continue
        process_records += 1
        role = process.get("role")
        if not isinstance(role, str) or not role:
            fail("process sample is missing a non-empty role")

        role_bucket = by_role.setdefault(
            role,
            {metric: [] for metric in PROCESS_METRICS},
        )
        for metric in PROCESS_METRICS:
            value = process.get(metric)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                fail(f"process metric {metric} must be a non-negative integer or null")
            role_bucket[metric].append(value)

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

    if process_records == 0:
        fail("OS/process evidence contains no process observations")

    role_summary = {
        role: {
            metric: _metric_stats(values)
            for metric, values in sorted(metrics.items())
        }
        for role, metrics in sorted(by_role.items())
    }

    def collect_group(group: str, metrics: tuple[str, ...]) -> dict[str, dict[str, int | None]]:
        result: dict[str, dict[str, int | None]] = {}
        for metric in metrics:
            values: list[int] = []
            for record in records:
                obj = record.get(group)
                if not isinstance(obj, dict):
                    fail(f"sample group {group} must be an object")
                value = obj.get(metric)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    fail(f"{group}.{metric} must be a non-negative integer")
                values.append(value)
            result[metric] = _metric_stats(values)
        return result

    first_timestamp = records[0]["timestamp_unix_nanos_utc"]
    last_timestamp = records[-1]["timestamp_unix_nanos_utc"]
    return {
        "schema": SUMMARY_SCHEMA,
        "claim": "bounded-resource-timeline-projection-only",
        "raw_sample_schema": SAMPLE_SCHEMA,
        "raw_sample_schema_version": SAMPLE_SCHEMA_VERSION,
        "campaign_id": expected_campaign_id,
        "sequence": SEQUENCE,
        "sample_count": len(records),
        "process_sample_count": process_records,
        "first_timestamp_unix_nanos_utc": first_timestamp,
        "last_timestamp_unix_nanos_utc": last_timestamp,
        "observed_span_nanos": last_timestamp - first_timestamp,
        "roles": role_summary,
        "availability_reasons": dict(sorted(availability_reasons.items())),
        "postgres": collect_group("postgres", POSTGRES_METRICS),
        "filesystem": collect_group("filesystem", FILESYSTEM_METRICS),
    }


def build_manifest(
    *,
    run_id: str,
    subject_sha: str,
    cargo_lock_sha256: str,
    rustc_version: str,
    cargo_version: str,
    target: str,
    oxide_subject: dict[str, Any],
    environment: dict[str, Any],
    artifacts: list[dict[str, str]],
    verifier_report: dict[str, Any],
) -> dict[str, Any]:
    manifest = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": "os-process",
        "run": {"id": run_id, "repetition": 1},
        "subject": {
            "workload_sha": subject_sha,
            "cargo_lock_sha256": cargo_lock_sha256,
            "rustc": rustc_version,
            "cargo": cargo_version,
            "target": target,
            "build_profile": "release",
            "oxide_batch": {
                "name": oxide_subject["name"],
                "version": oxide_subject["version"],
                "source": oxide_subject["source"],
                "checksum": oxide_subject["checksum"],
            },
        },
        "allocator": profile_smoke.allocator_record(ALLOCATOR),
        "environment": environment,
        "workload": {
            "reader_mode": READER,
            "rows": ROWS,
            "seed": SEED,
            "chunk_size": CHUNK_SIZE,
            "fetch_size": None,
            "page_size": PAGE_SIZE,
            "dataset_identity": verifier_report["source_digest"],
        },
        "instrumentation": {
            "profiler_attached": False,
            "tools": [
                {
                    "name": "oxide-batch-workloads-endurance-resource-sampler",
                    "version": f"{SAMPLE_SCHEMA}/v{SAMPLE_SCHEMA_VERSION}",
                    "role": (
                        "Linux/Android procfs process metrics plus PostgreSQL "
                        "connection/database and filesystem observations"
                    ),
                }
            ],
        },
        "artifacts": artifacts,
        "correctness": {
            "verdict": "PASS",
            "verifier": "postgres-postgres verify via #203 recorded iteration",
            "expected_digest_sha256": verifier_report["expected_digest_sha256"],
            "actual_digest_sha256": verifier_report["actual_digest_sha256"],
        },
        "limitations": [
            "bounded single-iteration Lane F rehearsal; this artifact is not the long-running/quiescence completion evidence",
            "raw samples are authoritative; summary/run JSON files are projections only",
            "process exit races may yield null metrics with explicit unavailable reasons instead of fabricated zeroes",
            "hosted-runner resource observations are orchestration evidence, not stable-host benchmark or leak evidence",
            "no benchmark timing claim is carried by this telemetry lane",
            "0.7.0 observations must not be generalized to later OxideBatch releases",
        ],
    }
    profile_manifest.validate_manifest(manifest)
    return manifest


def run_profile(args: argparse.Namespace) -> dict[str, Any]:
    if args.sample_interval_ms < 10 or args.sample_interval_ms > 60_000:
        raise ValueError("sample_interval_ms must be between 10 and 60000")

    profile_smoke.prepare_output_root(args.output)
    env = os.environ.copy()
    env["RUST_LOG"] = "error"

    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    workload_binary, controller_binary = build_binaries(target)

    subject_sha = profile_smoke.workload_sha()
    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = profile_smoke.sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.7.0")
    profile_smoke.validate_allocator_lock(lock_path, target)

    token = profile_smoke.run_token()
    database = profile_smoke.database_name(token, "os_process")
    selected_campaign = campaign_id(token)
    paired.compose_db("dropdb", database)
    paired.compose_db("createdb", database, template="template0")
    database_url = paired.database_url(args.base_database_url, database)

    memory_root = args.output / "memory"
    evidence_root = memory_root / "endurance"
    projection_root = memory_root / "projection"
    correctness_root = args.output / "correctness"
    binary_root = args.output / "binary"
    provenance_root = args.output / "provenance"
    for path in (
        evidence_root,
        projection_root,
        correctness_root,
        binary_root,
        provenance_root,
    ):
        path.mkdir(parents=True, exist_ok=True)

    retained_workload = binary_root / "postgres-postgres-system"
    retained_controller = binary_root / "postgres-postgres-endurancectl-system"
    shutil.copy2(workload_binary, retained_workload)
    shutil.copy2(controller_binary, retained_controller)
    retained_workload.chmod(0o755)
    retained_controller.chmod(0o755)

    try:
        profile_smoke.run_checked(
            [str(workload_binary), "migrate", "--database-url", database_url],
            env=env,
        )
        profile_smoke.run_checked(
            [
                str(workload_binary),
                "seed",
                "--database-url",
                database_url,
                "--rows",
                str(ROWS),
                "--seed",
                str(SEED),
            ],
            env=env,
        )

        command = [
            str(controller_binary),
            "run-sampled",
            "--database-url",
            database_url,
            "--evidence-root",
            str(evidence_root),
            "--campaign-id",
            selected_campaign,
            "--sequence",
            str(SEQUENCE),
            "--sample-interval-ms",
            str(args.sample_interval_ms),
        ]
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        stdout_path = memory_root / "controller.stdout.json"
        stderr_path = memory_root / "controller.stderr.txt"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            fail(
                f"sampled endurance controller exited {completed.returncode}: "
                f"{completed.stderr[-2000:]}"
            )

        try:
            controller_report = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("sampled endurance controller stdout is not JSON") from exc
        if not isinstance(controller_report, dict):
            fail("sampled endurance controller report must be an object")

        campaign_root = evidence_root / selected_campaign
        campaign_path = campaign_root / "campaign.json"
        events_path = campaign_root / "events.jsonl"
        samples_path = campaign_root / "samples.jsonl"
        iteration_path = campaign_root / "iterations" / f"{SEQUENCE:06}.json"
        for path in (campaign_path, events_path, samples_path, iteration_path):
            if not path.is_file() or path.stat().st_size <= 0:
                fail(f"required raw endurance evidence is missing or empty: {path}")

        iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
        if iteration.get("terminal") != "success":
            fail("retained endurance iteration is not terminal success")
        execution = iteration.get("execution")
        if not isinstance(execution, dict) or execution.get("kind") != "clean":
            fail("Lane F retained run must be a clean endurance execution")
        result = execution.get("result")
        if not isinstance(result, dict):
            fail("clean endurance execution result must be an object")
        if result.get("reader") != READER:
            fail(f"unexpected retained reader: {result.get('reader')!r}")
        if result.get("chunk_size") != CHUNK_SIZE:
            fail("retained chunk size does not match #203 canonical contract")
        if result.get("page_size") != PAGE_SIZE or result.get("fetch_size") is not None:
            fail("retained paging geometry does not match #203 canonical contract")
        if result.get("cleanup_deleted_rows") != ROWS:
            fail("clean endurance iteration did not clean exactly the verified business rows")
        run_result = result.get("run")
        verification_result = result.get("verification")
        if not isinstance(run_result, dict) or run_result.get("success") is not True:
            fail("retained workload child was not successful")
        if (
            not isinstance(verification_result, dict)
            or verification_result.get("success") is not True
        ):
            fail("retained verification child was not successful")

        verifier_report = validate_verifier_report(result.get("verification_report"))
        verifier_path = correctness_root / "verifier.json"
        profile_smoke.write_json(verifier_path, verifier_report)

        summary = summarize_samples(samples_path, selected_campaign)
        summary_path = projection_root / "summary.json"
        profile_smoke.write_json(summary_path, summary)

        run_metadata = {
            "schema": RUN_SCHEMA,
            "claim": "bounded-resource-timeline-only",
            "campaign_id": selected_campaign,
            "sequence": SEQUENCE,
            "sample_interval_ms": args.sample_interval_ms,
            "sample_schema": SAMPLE_SCHEMA,
            "sample_schema_version": SAMPLE_SCHEMA_VERSION,
            "sample_count": summary["sample_count"],
            "process_sample_count": summary["process_sample_count"],
            "workload_binary_sha256": profile_smoke.sha256_file(retained_workload),
            "controller_binary_sha256": profile_smoke.sha256_file(retained_controller),
            "command": command,
        }
        run_path = projection_root / "run.json"
        profile_smoke.write_json(run_path, run_metadata)

        retained_lock = provenance_root / "Cargo.lock"
        shutil.copy2(lock_path, retained_lock)
        registry_path = allocator_registry.DEFAULT_PATH
        retained_registry = provenance_root / "allocator_registry.json"
        shutil.copy2(registry_path, retained_registry)
        selected_allocator = allocator_registry.candidate_by_id(
            profile_smoke.ALLOCATOR_REGISTRY, ALLOCATOR
        )
        allocator_path = provenance_root / "allocator.json"
        profile_smoke.write_json(allocator_path, selected_allocator)
        toolchain_path = provenance_root / "toolchain.json"
        profile_smoke.write_json(
            toolchain_path,
            {
                "rustc": rustc_version,
                "cargo": cargo_version,
                "target": target,
            },
        )

        raw_paths = [
            campaign_path,
            events_path,
            samples_path,
            iteration_path,
            stdout_path,
            stderr_path,
            verifier_path,
        ]
        artifacts = [profile_smoke.artifact_record(args.output, path) for path in raw_paths]
        artifacts.extend(
            [
                {
                    **profile_smoke.artifact_record(args.output, summary_path),
                    "kind": "projection",
                },
                {
                    **profile_smoke.artifact_record(args.output, run_path),
                    "kind": "projection",
                },
                {
                    **profile_smoke.artifact_record(args.output, retained_workload),
                    "kind": "binary",
                },
                {
                    **profile_smoke.artifact_record(args.output, retained_controller),
                    "kind": "binary",
                },
                {
                    **profile_smoke.artifact_record(args.output, retained_lock),
                    "kind": "provenance",
                },
                {
                    **profile_smoke.artifact_record(args.output, retained_registry),
                    "kind": "provenance",
                },
                {
                    **profile_smoke.artifact_record(args.output, allocator_path),
                    "kind": "provenance",
                },
                {
                    **profile_smoke.artifact_record(args.output, toolchain_path),
                    "kind": "provenance",
                },
            ]
        )

        manifest = build_manifest(
            run_id=f"os-process-{token}-system-paging",
            subject_sha=subject_sha,
            cargo_lock_sha256=cargo_lock_sha256,
            rustc_version=rustc_version,
            cargo_version=cargo_version,
            target=target,
            oxide_subject=oxide_subject,
            environment=environment,
            artifacts=artifacts,
            verifier_report=verifier_report,
        )
        profile_smoke.write_json(args.output / "manifest.json", manifest)
        return {
            "valid": True,
            "manifest": str(args.output / "manifest.json"),
            "lane": "os-process",
            "samples": summary["sample_count"],
            "process_samples": summary["process_sample_count"],
            "correctness": "PASS",
        }
    finally:
        paired.compose_db("dropdb", database)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--sample-interval-ms",
        type=int,
        default=DEFAULT_SAMPLE_INTERVAL_MS,
    )
    args = parser.parse_args()

    result = run_profile(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
