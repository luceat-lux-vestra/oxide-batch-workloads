#!/usr/bin/env python3
"""Run one campaign #266 CPU-attribution profile with Linux perf."""

from __future__ import annotations

import argparse
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

ALLOCATOR_REGISTRY = allocator_registry.load_registry()
MAX_ROWS = 1_000_000
MAX_SIZE = 1_000_000
MAX_FREQUENCY_HZ = 1_000
DEFAULT_ROWS = 100_000
DEFAULT_SEED = 266
DEFAULT_CHUNK_SIZE = 1_000
DEFAULT_FETCH_SIZE = 500
DEFAULT_PAGE_SIZE = 750
DEFAULT_FREQUENCY_HZ = 99


def fail(message: str) -> None:
    raise RuntimeError(message)


def validate_bounds(args: argparse.Namespace) -> None:
    for name in ("rows", "chunk_size", "fetch_size", "page_size", "frequency_hz"):
        value = getattr(args, name)
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    if args.rows > MAX_ROWS:
        raise ValueError(f"rows must be <= {MAX_ROWS}")
    for name in ("chunk_size", "fetch_size", "page_size"):
        if getattr(args, name) > MAX_SIZE:
            raise ValueError(f"{name} must be <= {MAX_SIZE}")
    if args.frequency_hz > MAX_FREQUENCY_HZ:
        raise ValueError(f"frequency_hz must be <= {MAX_FREQUENCY_HZ}")
    if args.seed < 0:
        raise ValueError("seed must be non-negative")
    if args.reader not in ("cursor", "paging"):
        raise ValueError(f"unknown reader selection: {args.reader}")


def qualified_allocator(allocator: str, target: str) -> dict[str, Any]:
    candidate = allocator_registry.candidate_by_id(ALLOCATOR_REGISTRY, allocator)
    if candidate["status"] != "active":
        raise ValueError(f"allocator is not active: {allocator}")
    state = allocator_registry.target_state(candidate, target)
    if state != "qualified":
        raise ValueError(
            f"allocator {allocator} is not qualified for target {target}: {state}"
        )
    return candidate


def cpu_import_name(allocator: str, reader: str) -> str:
    value = f"cpu_profile_{allocator}_{reader}"
    if not paired.IMPORT_NAME.fullmatch(value):
        fail(f"unsafe generated CPU profile import name: {value!r}")
    return value


def build_variant(allocator: str, target: str) -> Path:
    qualified_allocator(allocator, target)
    profile_smoke.run_checked(profile_smoke.cargo_build_args(allocator))
    built = Path("target/release/postgres-postgres")
    if not built.is_file():
        fail(f"cargo build did not produce {built}")
    destination_root = Path("target/cpu-profile-binaries")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / f"postgres-postgres-{allocator}"
    shutil.copy2(built, destination)
    destination.chmod(0o755)
    return destination.resolve()


def find_perf() -> tuple[str, str]:
    perf = shutil.which("perf")
    if perf is None:
        fail("Linux perf is unavailable on this host")
    completed = profile_smoke.run_checked([perf, "--version"])
    version = completed.stdout.strip() or completed.stderr.strip()
    if not version:
        fail("perf --version returned no version text")
    return perf, version


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


def perf_record_command(
    *,
    perf: str,
    output: Path,
    frequency_hz: int,
    workload: list[str],
) -> list[str]:
    return [
        perf,
        "record",
        "--quiet",
        "--freq",
        str(frequency_hz),
        "--call-graph",
        "dwarf",
        "--output",
        str(output),
        "--",
        *workload,
    ]


def perf_projection_commands(perf: str, profile: Path) -> tuple[list[str], list[str]]:
    return (
        [perf, "report", "--stdio", "--input", str(profile)],
        [perf, "script", "--input", str(profile)],
    )


def cpu_limitations() -> list[str]:
    return [
        "CPU profiler runtime is attribution evidence only and must not be compared with benchmark-lane timing",
        "on-CPU sampling can underrepresent database, network, lock, sleep, and other off-CPU wait time",
        "hosted-runner attribution validates orchestration and profile readability, not stable-host absolute performance",
        "OxideBatch 0.6.0 Phase 0 evidence must not be generalized to the post-M7 allocator decision",
    ]


def build_manifest(
    *,
    allocator: str,
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
    perf_version: str,
) -> dict[str, Any]:
    manifest = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": "cpu",
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
        "allocator": profile_smoke.allocator_record(allocator),
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
            "profiler_attached": True,
            "tools": [
                {
                    "name": "perf",
                    "version": perf_version,
                    "role": "CPU sampling profiler",
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
        "limitations": cpu_limitations(),
    }
    profile_manifest.validate_manifest(manifest)
    return manifest


def run_profile(args: argparse.Namespace) -> dict[str, Any]:
    validate_bounds(args)
    profile_smoke.prepare_output_root(args.output)

    env = os.environ.copy()
    env["RUST_LOG"] = "error"

    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    qualified_allocator(args.allocator, target)
    binary = build_variant(args.allocator, target)
    perf, perf_version = find_perf()

    subject_sha = profile_smoke.workload_sha()
    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = profile_smoke.sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.6.0")
    profile_smoke.validate_allocator_lock(lock_path, target)

    token = profile_smoke.run_token()
    template_database = profile_smoke.database_name(token, "cpu_template")
    sample_database = profile_smoke.database_name(token, f"cpu_{args.allocator}")
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
        import_value = cpu_import_name(args.allocator, args.reader)

        cpu_root = args.output / "cpu"
        correctness_root = args.output / "correctness"
        cpu_root.mkdir(parents=True, exist_ok=True)
        correctness_root.mkdir(parents=True, exist_ok=True)

        perf_data = cpu_root / "perf.data"
        perf_stderr = cpu_root / "perf.stderr.txt"
        workload = workload_command(
            binary=binary,
            import_value=import_value,
            reader=args.reader,
            chunk_size=args.chunk_size,
            fetch_size=args.fetch_size,
            page_size=args.page_size,
        )

        profiled_env = env.copy()
        # Do not expose the credential-bearing DATABASE_URL in perf's recorded
        # child command line. clap reads it from the environment instead.
        profiled_env["DATABASE_URL"] = database_url
        completed = subprocess.run(
            perf_record_command(
                perf=perf,
                output=perf_data,
                frequency_hz=args.frequency_hz,
                workload=workload,
            ),
            check=False,
            text=True,
            capture_output=True,
            env=profiled_env,
        )
        perf_stderr.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            fail(
                f"perf record exited {completed.returncode}: "
                f"{completed.stderr[-2000:]}"
            )
        if not perf_data.is_file() or perf_data.stat().st_size <= 0:
            fail("perf record produced no non-empty perf.data")

        report_command, script_command = perf_projection_commands(perf, perf_data)
        report = subprocess.run(
            report_command,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        if report.returncode != 0:
            fail(f"perf report exited {report.returncode}: {report.stderr[-2000:]}")
        script = subprocess.run(
            script_command,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        if script.returncode != 0:
            fail(f"perf script exited {script.returncode}: {script.stderr[-2000:]}")

        report_path = cpu_root / "perf-report.txt"
        script_path = cpu_root / "perf-script.txt"
        report_path.write_text(report.stdout, encoding="utf-8")
        script_path.write_text(script.stdout, encoding="utf-8")
        if not report.stdout.strip():
            fail("perf report projection was empty")
        if not script.stdout.strip():
            fail("perf script projection was empty")

        verifier_report, verifier_stdout = profile_smoke.run_verifier(
            binary=binary,
            database_url=database_url,
            import_value=import_value,
            rows=args.rows,
            env=env,
        )
        verifier_path = correctness_root / "verifier.json"
        verifier_path.write_text(verifier_stdout, encoding="utf-8")

        profiler_metadata = {
            "schema": "oxide-batch-cpu-profile-run/v1",
            "profiler": {"name": "perf", "version": perf_version},
            "sample_frequency_hz": args.frequency_hz,
            "call_graph": "dwarf",
            "allocator": args.allocator,
            "reader_mode": args.reader,
            "rows": args.rows,
            "seed": args.seed,
            "child_exit_status": completed.returncode,
            "timing_claim": "attribution-only",
        }
        metadata_path = cpu_root / "run.json"
        profile_smoke.write_json(metadata_path, profiler_metadata)

        artifacts = [
            profile_smoke.artifact_record(args.output, perf_data),
            profile_smoke.artifact_record(args.output, perf_stderr),
            {
                **profile_smoke.artifact_record(args.output, report_path),
                "kind": "projection",
            },
            profile_smoke.artifact_record(args.output, script_path),
            profile_smoke.artifact_record(args.output, metadata_path),
            profile_smoke.artifact_record(args.output, verifier_path),
        ]
        manifest = build_manifest(
            allocator=args.allocator,
            run_id=f"cpu-profile-{token}-{args.allocator}-{args.reader}",
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
            perf_version=perf_version,
        )
        profile_smoke.write_json(args.output / "manifest.json", manifest)
        return manifest
    finally:
        paired.compose_db("dropdb", sample_database)
        paired.compose_db("dropdb", template_database)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allocator",
        required=True,
        choices=tuple(
            candidate["identity"]
            for candidate in allocator_registry.active_candidates(ALLOCATOR_REGISTRY)
        ),
    )
    parser.add_argument("--reader", choices=("cursor", "paging"), default="paging")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--fetch-size", type=int, default=DEFAULT_FETCH_SIZE)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--frequency-hz", type=int, default=DEFAULT_FREQUENCY_HZ)
    args = parser.parse_args()

    manifest = run_profile(args)
    print(
        json.dumps(
            {
                "valid": True,
                "lane": manifest["lane"],
                "allocator": manifest["allocator"]["identity"],
                "reader": manifest["workload"]["reader_mode"],
                "profiler": manifest["instrumentation"]["tools"][0]["version"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
