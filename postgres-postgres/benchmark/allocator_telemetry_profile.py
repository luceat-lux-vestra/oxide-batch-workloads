#!/usr/bin/env python3
"""Produce retained campaign #266 allocator-native telemetry evidence."""

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
WIRED_ALLOCATORS = tuple(
    candidate["identity"]
    for candidate in allocator_registry.wired_telemetry_candidates(ALLOCATOR_REGISTRY)
)
DEFAULT_ROWS = 2_000
DEFAULT_SEED = 266
DEFAULT_CHUNK_SIZE = 200
DEFAULT_FETCH_SIZE = 128
DEFAULT_PAGE_SIZE = 128
MAX_ROWS = 10_000
MAX_SIZE = 100_000
TELEMETRY_SCHEMA = "oxide-batch-allocator-native-telemetry/v1"
RUN_SCHEMA = "oxide-batch-allocator-telemetry-profile-run/v1"
TELEMETRY_DIR_ENV = "OXIDE_BATCH_ALLOCATOR_TELEMETRY_DIR"


def fail(message: str) -> None:
    raise RuntimeError(message)


def telemetry_candidate(allocator: str, target: str | None = None) -> dict[str, Any]:
    candidate = allocator_registry.candidate_by_id(ALLOCATOR_REGISTRY, allocator)
    telemetry = candidate["telemetry"]
    if candidate["status"] != "active" or telemetry["status"] != "wired":
        raise ValueError(f"allocator-native telemetry is not wired for {allocator}")
    if target is not None:
        state = allocator_registry.target_state(candidate, target)
        if state != "qualified":
            raise ValueError(
                f"allocator-native telemetry is not qualified for {allocator} on {target}: {state}"
            )
    return candidate


def telemetry_build_args(allocator: str) -> list[str]:
    candidate = telemetry_candidate(allocator)
    feature = allocator_registry.telemetry_feature(candidate)
    if not feature:
        raise ValueError(f"allocator-native telemetry feature is missing for {allocator}")
    return [
        "cargo",
        "build",
        "--locked",
        "--release",
        "-p",
        "postgres-postgres",
        "--no-default-features",
        "--features",
        feature,
    ]


def build_telemetry_binary(allocator: str, target: str) -> tuple[Path, dict[str, Any]]:
    candidate = telemetry_candidate(allocator, target)
    profile_smoke.run_checked(telemetry_build_args(allocator))
    built = Path("target/release/postgres-postgres")
    if not built.is_file():
        fail(f"cargo build did not produce {built}")
    destination_root = Path("target/allocator-telemetry-binaries")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / f"postgres-postgres-{allocator}-telemetry"
    shutil.copy2(built, destination)
    destination.chmod(0o755)
    return destination.resolve(), candidate


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


def telemetry_import_name(allocator: str, reader: str) -> str:
    value = f"allocator_telemetry_{allocator}_{reader}"
    if not paired.IMPORT_NAME.fullmatch(value):
        fail(f"unsafe generated allocator telemetry import name: {value!r}")
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


def _require_non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        fail(f"{field} must be a non-negative integer")
    return value


def validate_native_snapshot(path: Path, allocator: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        fail("allocator telemetry producer emitted no non-empty snapshot.json")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("allocator snapshot.json is not valid JSON") from exc
    if not isinstance(snapshot, dict):
        fail("allocator snapshot must be an object")
    if snapshot.get("schema") != TELEMETRY_SCHEMA:
        fail(f"unexpected allocator telemetry schema: {snapshot.get('schema')!r}")
    if snapshot.get("allocator") != allocator:
        fail(
            f"allocator snapshot identity mismatch: expected {allocator!r}, "
            f"found {snapshot.get('allocator')!r}"
        )
    if snapshot.get("capture") != "post-successful-workload-run":
        fail(f"unexpected allocator capture boundary: {snapshot.get('capture')!r}")
    native_api = snapshot.get("native_api")
    if not isinstance(native_api, str) or not native_api.strip():
        fail("allocator snapshot native_api must be a non-empty string")
    payload = snapshot.get("native_payload")
    if not isinstance(payload, dict) or not payload:
        fail("allocator snapshot native_payload must be a non-empty object")
    limitations = snapshot.get("limitations")
    if (
        not isinstance(limitations, list)
        or not limitations
        or any(not isinstance(item, str) or not item.strip() for item in limitations)
    ):
        fail("allocator snapshot limitations must be a non-empty string array")

    if allocator == "jemalloc":
        required = {
            "stats.allocated",
            "stats.active",
            "stats.metadata",
            "stats.resident",
            "stats.mapped",
            "stats.retained",
        }
        missing = sorted(required - payload.keys())
        if missing:
            fail(f"jemalloc snapshot is missing native counters: {missing}")
        for key in required:
            _require_non_negative_int(payload[key], f"native_payload.{key}")
    elif allocator == "snmalloc":
        current = _require_non_negative_int(
            payload.get("current_memory_usage"),
            "native_payload.current_memory_usage",
        )
        peak = _require_non_negative_int(
            payload.get("peak_memory_usage"),
            "native_payload.peak_memory_usage",
        )
        if peak < current:
            fail("snmalloc peak_memory_usage must be >= current_memory_usage")
    elif allocator == "mimalloc":
        if "mi_stats_get_json" not in native_api:
            fail("mimalloc snapshot must identify mi_stats_get_json native API")
    else:
        fail(f"unexpected wired allocator telemetry identity: {allocator}")

    return snapshot


def limitations() -> list[str]:
    return [
        "allocator-native telemetry is a post-successful-workload point snapshot and is not benchmark timing",
        "allocator-specific payloads retain native semantics and are intentionally not normalized into one fragmentation metric",
        "snapshot capture and serialization add allocator/runtime overhead after workload completion",
        "hosted-runner evidence validates telemetry orchestration and native API readability, not stable-host absolute performance",
        "this bounded run is not a long-running or post-quiescence retention observation",
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
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    manifest = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": "allocator",
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
            "profiler_attached": False,
            "tools": [
                {
                    "name": "allocator-native-telemetry",
                    "version": TELEMETRY_SCHEMA,
                    "role": f"post-successful-workload native snapshot via {snapshot['native_api']}",
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
        "limitations": limitations(),
    }
    profile_manifest.validate_manifest(manifest)
    return manifest


def run_profile(args: argparse.Namespace) -> dict[str, Any]:
    validate_bounds(args)
    profile_smoke.prepare_output_root(args.output)

    env = os.environ.copy()
    env["RUST_LOG"] = "error"

    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    binary, candidate = build_telemetry_binary(args.allocator, target)

    subject_sha = profile_smoke.workload_sha()
    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = profile_smoke.sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.6.0")
    profile_smoke.validate_allocator_lock(lock_path, target)

    token = profile_smoke.run_token()
    template_database = profile_smoke.database_name(token, "allocator_template")
    sample_database = profile_smoke.database_name(token, f"allocator_{args.allocator}")
    paired.compose_db("dropdb", template_database)
    paired.compose_db("createdb", template_database, template="template0")
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
        import_value = telemetry_import_name(args.allocator, args.reader)

        allocator_root = args.output / "allocator"
        correctness_root = args.output / "correctness"
        binary_root = args.output / "binary"
        provenance_root = args.output / "provenance"
        for path in (allocator_root, correctness_root, binary_root, provenance_root):
            path.mkdir(parents=True, exist_ok=True)

        retained_binary = binary_root / f"postgres-postgres-{args.allocator}-telemetry"
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
        run_env[TELEMETRY_DIR_ENV] = str(allocator_root.resolve())

        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=run_env,
        )
        stdout_path = allocator_root / "workload.stdout.txt"
        stderr_path = allocator_root / "workload.stderr.txt"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            fail(
                f"allocator telemetry workload exited {completed.returncode}: "
                f"{completed.stderr[-2000:]}"
            )

        snapshot_path = allocator_root / "snapshot.json"
        snapshot = validate_native_snapshot(snapshot_path, args.allocator)

        verifier_report, verifier_stdout = profile_smoke.run_verifier(
            binary=binary,
            database_url=database_url,
            import_value=import_value,
            rows=args.rows,
            env=env,
        )
        verifier_path = correctness_root / "verifier.json"
        verifier_path.write_text(verifier_stdout, encoding="utf-8")

        retained_lock = provenance_root / "Cargo.lock"
        retained_registry = provenance_root / "allocator_registry.json"
        shutil.copy2(lock_path, retained_lock)
        shutil.copy2(Path("benchmark/allocator_registry.json"), retained_registry)

        allocator_provenance_path = provenance_root / "allocator.json"
        profile_smoke.write_json(allocator_provenance_path, candidate)
        toolchain_path = provenance_root / "toolchain.json"
        profile_smoke.write_json(
            toolchain_path,
            {
                "rustc": rustc_version,
                "cargo": cargo_version,
                "target": target,
                "build_command": telemetry_build_args(args.allocator),
            },
        )

        run_metadata = {
            "schema": RUN_SCHEMA,
            "claim": "allocator-native-post-success-snapshot-only",
            "allocator": args.allocator,
            "telemetry_feature": allocator_registry.telemetry_feature(candidate),
            "capabilities": candidate["telemetry"]["capabilities"],
            "reader_mode": args.reader,
            "rows": args.rows,
            "seed": args.seed,
            "chunk_size": args.chunk_size,
            "fetch_size": args.fetch_size if args.reader == "cursor" else None,
            "page_size": args.page_size if args.reader == "paging" else None,
            "telemetry_dir_env": TELEMETRY_DIR_ENV,
            "telemetry_schema": TELEMETRY_SCHEMA,
            "native_api": snapshot["native_api"],
            "workload_exit_status": completed.returncode,
            "binary_sha256": profile_smoke.sha256_file(retained_binary),
            "snapshot_sha256": profile_smoke.sha256_file(snapshot_path),
            "command": command,
        }
        run_path = allocator_root / "run.json"
        profile_smoke.write_json(run_path, run_metadata)

        artifacts = [
            profile_smoke.artifact_record(args.output, snapshot_path),
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
            {
                **profile_smoke.artifact_record(args.output, retained_lock),
                "kind": "provenance",
            },
            {
                **profile_smoke.artifact_record(args.output, retained_registry),
                "kind": "provenance",
            },
            {
                **profile_smoke.artifact_record(args.output, allocator_provenance_path),
                "kind": "provenance",
            },
            {
                **profile_smoke.artifact_record(args.output, toolchain_path),
                "kind": "provenance",
            },
        ]

        manifest = build_manifest(
            allocator=args.allocator,
            run_id=f"allocator-{token}-{args.allocator}-{args.reader}",
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
            snapshot=snapshot,
        )
        profile_smoke.write_json(args.output / "manifest.json", manifest)
        return {
            "valid": True,
            "manifest": str(args.output / "manifest.json"),
            "lane": "allocator",
            "allocator": args.allocator,
            "correctness": "PASS",
        }
    finally:
        paired.compose_db("dropdb", sample_database)
        paired.compose_db("dropdb", template_database)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allocator", choices=WIRED_ALLOCATORS, required=True)
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
