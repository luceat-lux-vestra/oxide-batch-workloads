#!/usr/bin/env python3
"""Run one campaign #266 native timeline-attribution profile."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import allocator_registry
import paired
import profile_manifest
import profile_smoke

ALLOCATOR_REGISTRY = allocator_registry.load_registry()
ALLOCATOR = "system"
MAX_ROWS = 10_000
MAX_SIZE = 100_000
MAX_ESTIMATED_ITEM_LISTENER_RECORDS = 40_000
DEFAULT_ROWS = 2_000
DEFAULT_SEED = 266
DEFAULT_CHUNK_SIZE = 200
DEFAULT_FETCH_SIZE = 128
DEFAULT_PAGE_SIZE = 128
TIMELINE_SCHEMA = "oxide-batch-workload-timeline/v1"
TIMELINE_SUMMARY_SCHEMA = "oxide-batch-workload-timeline-summary/v1"
RUN_SCHEMA = "oxide-batch-timeline-profile-run/v1"
TIMELINE_DIR_ENV = "OXIDE_BATCH_TIMELINE_DIR"


def fail(message: str) -> None:
    raise RuntimeError(message)


def validate_bounds(args: argparse.Namespace) -> None:
    for name in ("rows", "chunk_size", "fetch_size", "page_size"):
        value = getattr(args, name)
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    if args.rows > MAX_ROWS:
        raise ValueError(f"rows must be <= {MAX_ROWS}")
    for name in ("chunk_size", "fetch_size", "page_size"):
        if getattr(args, name) > MAX_SIZE:
            raise ValueError(f"{name} must be <= {MAX_SIZE}")
    if args.seed < 0:
        raise ValueError("seed must be non-negative")
    if args.reader not in ("cursor", "paging"):
        raise ValueError(f"unknown reader selection: {args.reader}")

    chunks = math.ceil(args.rows / args.chunk_size)
    item_listener_upper_bound = 4 * args.rows + 2 * chunks
    if item_listener_upper_bound > MAX_ESTIMATED_ITEM_LISTENER_RECORDS:
        raise ValueError(
            "rows/chunk_size exceed the bounded timeline item-listener budget: "
            f"{item_listener_upper_bound} > {MAX_ESTIMATED_ITEM_LISTENER_RECORDS}"
        )


def qualified_system_allocator(target: str) -> None:
    candidate = allocator_registry.candidate_by_id(ALLOCATOR_REGISTRY, ALLOCATOR)
    if candidate["status"] != "active":
        raise ValueError("system allocator is not active in the allocator registry")
    state = allocator_registry.target_state(candidate, target)
    if state != "qualified":
        raise ValueError(f"system allocator is not qualified for target {target}: {state}")


def timeline_import_name(reader: str) -> str:
    value = f"timeline_profile_system_{reader}"
    if not paired.IMPORT_NAME.fullmatch(value):
        fail(f"unsafe generated timeline profile import name: {value!r}")
    return value


def workload_command(
    *,
    binary: Path,
    import_value: str,
    reader: str,
    chunk_size: int,
    fetch_size: int,
    page_size: int,
) -> list[str]:
    return [
        str(binary),
        "run",
        "--import-name",
        import_value,
        "--chunk-size",
        str(chunk_size),
        *paired.reader_args(reader, fetch_size, page_size),
    ]


def build_system_binary(target: str) -> Path:
    qualified_system_allocator(target)
    profile_smoke.run_checked(profile_smoke.cargo_build_args(ALLOCATOR))
    built = Path("target/release/postgres-postgres")
    if not built.is_file():
        fail(f"cargo build did not produce {built}")
    destination_root = Path("target/timeline-profile-binaries")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / "postgres-postgres-system"
    shutil.copy2(built, destination)
    destination.chmod(0o755)
    return destination.resolve()


def timeline_limitations() -> list[str]:
    return [
        "native timeline timestamps are phase-boundary attribution evidence only and are not benchmark timing",
        "listener timestamps include listener dispatch overhead",
        "item.read and item.write are database-facing operation boundaries, not isolated PostgreSQL wait measurements",
        "OxideBatch 0.6.0 exposes checkpoint durability through chunk.committed; no separate checkpoint duration is inferred",
        "the workload is deterministic and single-threaded, so queue/backpressure and multi-worker concurrency are not inferred",
        "hosted-runner evidence validates orchestration and attribution readability, not stable-host absolute performance",
        "OxideBatch 0.6.0 Phase 0 evidence must not be generalized to the post-M7 allocator decision",
    ]


def validate_timeline_evidence(events_path: Path, summary_path: Path) -> dict[str, Any]:
    if not events_path.is_file() or events_path.stat().st_size <= 0:
        fail("timeline recorder produced no non-empty events.jsonl")
    if not summary_path.is_file() or summary_path.stat().st_size <= 0:
        fail("timeline recorder produced no non-empty summary.json")

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("timeline summary.json is not valid JSON") from exc

    if summary.get("schema") != TIMELINE_SUMMARY_SCHEMA:
        fail(f"unexpected timeline summary schema: {summary.get('schema')!r}")
    if summary.get("claim") != "phase-boundary-attribution-only":
        fail(f"unexpected timeline claim: {summary.get('claim')!r}")
    if summary.get("clock") != "process-monotonic-std-instant":
        fail(f"unexpected timeline clock: {summary.get('clock')!r}")
    if summary.get("framework_telemetry_schema_version") != 1:
        fail("unexpected OxideBatch framework telemetry schema version")
    if summary.get("dropped_records") != 0:
        fail(f"authoritative timeline run dropped records: {summary.get('dropped_records')!r}")

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(events_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            fail(f"timeline events contains an empty line at {line_number}")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"timeline event line {line_number} is not valid JSON"
            ) from exc
        if not isinstance(record, dict):
            fail(f"timeline event line {line_number} must be an object")
        records.append(record)

    if not records:
        fail("timeline events contains no records")
    if summary.get("observed_records") != len(records):
        fail("timeline observed_records does not match retained events")
    if summary.get("retained_records") != len(records):
        fail("timeline retained_records does not match retained events")

    previous_elapsed = -1
    phase_transitions: set[tuple[str, str]] = set()
    for sequence, record in enumerate(records):
        if record.get("schema") != TIMELINE_SCHEMA:
            fail(f"timeline record {sequence} has unexpected schema")
        if record.get("sequence") != sequence:
            fail(
                f"timeline sequence is not contiguous at record {sequence}: "
                f"{record.get('sequence')!r}"
            )
        elapsed = record.get("elapsed_ns")
        if not isinstance(elapsed, int) or isinstance(elapsed, bool) or elapsed < 0:
            fail(f"timeline record {sequence} has invalid elapsed_ns")
        if elapsed < previous_elapsed:
            fail(f"timeline elapsed_ns regressed at record {sequence}")
        previous_elapsed = elapsed
        phase = record.get("phase")
        transition = record.get("transition")
        if not isinstance(phase, str) or not phase:
            fail(f"timeline record {sequence} has invalid phase")
        if not isinstance(transition, str) or not transition:
            fail(f"timeline record {sequence} has invalid transition")
        phase_transitions.add((phase, transition))

    required = {
        ("item.read", "before"),
        ("item.read", "after"),
        ("item.process", "before"),
        ("item.process", "after"),
        ("item.write", "before"),
        ("item.write", "after"),
        ("chunk.committed", "event"),
    }
    missing = sorted(required - phase_transitions)
    if missing:
        fail(f"timeline evidence is missing required phase boundaries: {missing}")

    return summary


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
    reader: str,
    rows: int,
    seed: int,
    chunk_size: int,
    fetch_size: int,
    page_size: int,
    dataset_identity: str,
    artifacts: list[dict[str, str]],
    verifier_report: dict[str, Any],
) -> dict[str, Any]:
    manifest = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": "timeline",
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
            "reader_mode": reader,
            "rows": rows,
            "seed": seed,
            "chunk_size": chunk_size,
            "fetch_size": fetch_size if reader == "cursor" else None,
            "page_size": page_size if reader == "paging" else None,
            "dataset_identity": dataset_identity,
        },
        "instrumentation": {
            "profiler_attached": False,
            "tools": [
                {
                    "name": "oxide-batch-native-timeline-recorder",
                    "version": TIMELINE_SCHEMA,
                    "role": "in-process framework lifecycle and item-listener phase-boundary recorder",
                }
            ],
        },
        "artifacts": artifacts,
        "correctness": {
            "verdict": "PASS",
            "verifier": "postgres-postgres verify",
            "expected_digest_sha256": verifier_report["expected_digest_sha256"],
            "actual_digest_sha256": verifier_report["actual_digest_sha256"],
        },
        "limitations": timeline_limitations(),
    }
    profile_manifest.validate_manifest(manifest)
    return manifest


def run_profile(args: argparse.Namespace) -> dict[str, Any]:
    validate_bounds(args)
    profile_smoke.prepare_output_root(args.output)

    env = os.environ.copy()
    env["RUST_LOG"] = "error"

    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    qualified_system_allocator(target)
    binary = build_system_binary(target)

    subject_sha = profile_smoke.workload_sha()
    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = profile_smoke.sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.6.0")
    profile_smoke.validate_allocator_lock(lock_path, target)

    token = profile_smoke.run_token()
    template_database = profile_smoke.database_name(token, "timeline_template")
    sample_database = profile_smoke.database_name(token, "timeline_system")
    profile_smoke.paired.compose_db("dropdb", template_database)
    profile_smoke.paired.compose_db("createdb", template_database, template="template0")
    template_url = paired.database_url(args.base_database_url, template_database)

    try:
        profile_smoke.run_checked(
            [str(binary), "migrate", "--database-url", template_url],
            env=env,
        )
        profile_smoke.run_checked(
            [
                str(binary),
                "seed",
                "--database-url",
                template_url,
                "--rows",
                str(args.rows),
                "--seed",
                str(args.seed),
            ],
            env=env,
        )

        paired.compose_db("dropdb", sample_database)
        paired.compose_db("createdb", sample_database, template=template_database)
        database_url = paired.database_url(args.base_database_url, sample_database)
        import_value = timeline_import_name(args.reader)

        timeline_root = args.output / "timeline"
        correctness_root = args.output / "correctness"
        binary_root = args.output / "binary"
        timeline_root.mkdir(parents=True, exist_ok=True)
        correctness_root.mkdir(parents=True, exist_ok=True)
        binary_root.mkdir(parents=True, exist_ok=True)

        retained_binary = binary_root / "postgres-postgres-system"
        shutil.copy2(binary, retained_binary)
        retained_binary.chmod(0o755)

        command = workload_command(
            binary=binary,
            import_value=import_value,
            reader=args.reader,
            chunk_size=args.chunk_size,
            fetch_size=args.fetch_size,
            page_size=args.page_size,
        )
        run_env = env.copy()
        run_env["DATABASE_URL"] = database_url
        run_env[TIMELINE_DIR_ENV] = str(timeline_root.resolve())

        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=run_env,
        )
        stdout_path = timeline_root / "workload.stdout.txt"
        stderr_path = timeline_root / "workload.stderr.txt"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            fail(
                f"timeline workload exited {completed.returncode}: "
                f"{completed.stderr[-2000:]}"
            )

        events_path = timeline_root / "events.jsonl"
        summary_path = timeline_root / "summary.json"
        timeline_summary = validate_timeline_evidence(events_path, summary_path)

        verifier_report, verifier_stdout = profile_smoke.run_verifier(
            binary=binary,
            database_url=database_url,
            import_value=import_value,
            rows=args.rows,
            env=env,
        )
        verifier_path = correctness_root / "verifier.json"
        verifier_path.write_text(verifier_stdout, encoding="utf-8")

        run_metadata = {
            "schema": RUN_SCHEMA,
            "allocator": ALLOCATOR,
            "reader_mode": args.reader,
            "rows": args.rows,
            "seed": args.seed,
            "chunk_size": args.chunk_size,
            "fetch_size": args.fetch_size if args.reader == "cursor" else None,
            "page_size": args.page_size if args.reader == "paging" else None,
            "timeline_dir_env": TIMELINE_DIR_ENV,
            "timeline_schema": TIMELINE_SCHEMA,
            "timeline_summary_schema": TIMELINE_SUMMARY_SCHEMA,
            "claim": "phase-boundary-attribution-only",
            "workload_exit_status": completed.returncode,
            "binary_sha256": profile_smoke.sha256_file(retained_binary),
            "observed_records": timeline_summary["observed_records"],
            "retained_records": timeline_summary["retained_records"],
            "dropped_records": timeline_summary["dropped_records"],
            "command": command,
        }
        run_path = timeline_root / "run.json"
        profile_smoke.write_json(run_path, run_metadata)

        artifacts = [
            profile_smoke.artifact_record(args.output, events_path),
            {
                **profile_smoke.artifact_record(args.output, summary_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, run_path),
                "kind": "projection",
            },
            profile_smoke.artifact_record(args.output, stdout_path),
            profile_smoke.artifact_record(args.output, stderr_path),
            profile_smoke.artifact_record(args.output, verifier_path),
            {
                **profile_smoke.artifact_record(args.output, retained_binary),
                "kind": "binary",
            },
        ]

        manifest = build_manifest(
            run_id=f"timeline-{token}-system-{args.reader}",
            subject_sha=subject_sha,
            cargo_lock_sha256=cargo_lock_sha256,
            rustc_version=rustc_version,
            cargo_version=cargo_version,
            target=target,
            oxide_subject=oxide_subject,
            environment=environment,
            reader=args.reader,
            rows=args.rows,
            seed=args.seed,
            chunk_size=args.chunk_size,
            fetch_size=args.fetch_size,
            page_size=args.page_size,
            dataset_identity=verifier_report["source_digest"],
            artifacts=artifacts,
            verifier_report=verifier_report,
        )
        profile_smoke.write_json(args.output / "manifest.json", manifest)
        return {
            "valid": True,
            "manifest": str(args.output / "manifest.json"),
            "lane": "timeline",
            "records": timeline_summary["retained_records"],
            "correctness": "PASS",
        }
    finally:
        paired.compose_db("dropdb", sample_database)
        paired.compose_db("dropdb", template_database)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--reader", choices=("cursor", "paging"), default="paging")
    parser.add_argument("--fetch-size", type=int, default=DEFAULT_FETCH_SIZE)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    args = parser.parse_args()

    result = run_profile(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
