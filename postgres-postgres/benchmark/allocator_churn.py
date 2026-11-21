#!/usr/bin/env python3
"""Run campaign #323 same-process real-workload allocator churn on qualified ARM64."""

from __future__ import annotations

import argparse
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

SCHEMA = "oxide-batch-allocator-same-process-churn-campaign/v1"
RUNNER_SCHEMA = "oxide-batch-allocator-same-process-churn/v1"
CANONICAL_TARGET = "aarch64-unknown-linux-gnu"
ROWS = 1_000_000
SEED = 20_260_904
CHUNK_SIZE = 1_000
FETCH_SIZE = 500
PAGE_SIZE = 750
DEFAULT_CYCLES = 20
MAX_CYCLES = 22
DEFAULT_IDLE_MS = 1_000
MAX_IDLE_MS = 600_000
PHASES = ("pre_run", "post_run", "post_cleanup", "post_idle")
NATIVE_PHASES = ("post-run", "post-cleanup", "post-idle")
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


def fail(message: str) -> None:
    raise RuntimeError(message)


def validate_geometry(args: argparse.Namespace) -> None:
    if args.cycles < 2 or args.cycles > MAX_CYCLES:
        raise ValueError(f"cycles must be between 2 and {MAX_CYCLES}")
    if args.cycles % 2:
        raise ValueError("cycles must be even so cursor/paging counts remain balanced")
    if args.idle_ms <= 0 or args.idle_ms > MAX_IDLE_MS:
        raise ValueError(f"idle_ms must be between 1 and {MAX_IDLE_MS}")


def candidates_for_target(target: str) -> tuple[dict[str, Any], ...]:
    if target != CANONICAL_TARGET:
        fail(
            "campaign #323 same-process allocator churn requires exact target "
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
        "postgres-postgres-allocator-churn",
    ]
    feature = selected_feature(candidate)
    if feature:
        command.extend(["--features", feature])
    return command


def campaign_id(token: str, allocator: str) -> str:
    safe = f"allocchurn-{token}-{allocator}".replace("_", "-")
    value = safe[:48]
    if not value or not value[0].isalnum():
        fail(f"unsafe generated campaign id: {value!r}")
    if any(not (char.isalnum() or char == "-") for char in value):
        fail(f"unsafe generated campaign id: {value!r}")
    return value


def require_uint(value: Any, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        fail(f"{field} must be an integer >= {minimum}")
    return value


def validate_process_sample(sample: Any, *, phase: str, pid: int) -> dict[str, Any]:
    if not isinstance(sample, dict):
        fail(f"{phase} sample must be an object")
    expected_phase = phase.replace("_", "-")
    if sample.get("phase") != expected_phase:
        fail(f"{phase} sample phase mismatch: {sample.get('phase')!r}")
    process = sample.get("process")
    if not isinstance(process, dict):
        fail(f"{phase}.process must be an object")
    if require_uint(process.get("pid"), f"{phase}.process.pid", positive=True) != pid:
        fail(f"{phase} process pid changed within same-process churn")
    if process.get("role") != "same-process-churn":
        fail(f"{phase} process role mismatch")
    for metric in PROCESS_METRICS:
        value = process.get(metric)
        if value is not None:
            require_uint(value, f"{phase}.process.{metric}")
    for required in ("rss_bytes", "pss_bytes", "smaps_rollup_rss_bytes"):
        require_uint(process.get(required), f"{phase}.process.{required}", positive=True)
    return process


def validate_runner_report(
    report: Any,
    *,
    allocator: str,
    cycles: int,
    idle_ms: int,
    source_rows: int,
    native_enabled: bool,
) -> dict[str, Any]:
    if not isinstance(report, dict):
        fail("same-process churn runner report must be an object")
    if report.get("schema") != RUNNER_SCHEMA:
        fail(f"unexpected runner schema: {report.get('schema')!r}")
    if report.get("allocator") != allocator:
        fail(f"runner allocator mismatch: {report.get('allocator')!r} != {allocator!r}")
    pid = require_uint(report.get("process_id"), "process_id", positive=True)
    if report.get("cycles") != cycles:
        fail("runner cycle count mismatch")
    if report.get("cursor_cycles") != cycles // 2 or report.get("paging_cycles") != cycles // 2:
        fail("runner reader counts are not balanced")
    if report.get("idle_ms") != idle_ms:
        fail("runner idle_ms mismatch")
    if report.get("source_rows") != source_rows:
        fail("runner source row count mismatch")
    if report.get("chunk_size") != CHUNK_SIZE:
        fail("runner chunk size drift")
    if report.get("cursor_fetch_size") != FETCH_SIZE:
        fail("runner cursor fetch size drift")
    if report.get("paging_page_size") != PAGE_SIZE:
        fail("runner paging page size drift")
    if report.get("native_telemetry_enabled") is not native_enabled:
        fail("runner native telemetry mode mismatch")

    cycle_reports = report.get("cycle_reports")
    if not isinstance(cycle_reports, list) or len(cycle_reports) != cycles:
        fail("runner cycle_reports coverage mismatch")

    phase_processes: dict[str, list[dict[str, Any]]] = {phase: [] for phase in PHASES}
    for expected_sequence, cycle in enumerate(cycle_reports, start=1):
        if not isinstance(cycle, dict):
            fail(f"cycle {expected_sequence} report must be an object")
        if cycle.get("sequence") != expected_sequence:
            fail(f"cycle sequence mismatch at {expected_sequence}")
        expected_reader = "cursor" if expected_sequence % 2 else "paging"
        if cycle.get("reader") != expected_reader:
            fail(f"cycle {expected_sequence} reader mismatch")
        if cycle.get("projection_rows") != source_rows:
            fail(f"cycle {expected_sequence} projection row count mismatch")
        if cycle.get("cleanup_deleted_rows") != source_rows:
            fail(f"cycle {expected_sequence} cleanup row count mismatch")
        for phase in PHASES:
            phase_processes[phase].append(
                validate_process_sample(cycle.get(phase), phase=phase, pid=pid)
            )
        native = cycle.get("native")
        if not isinstance(native, dict):
            fail(f"cycle {expected_sequence} native section must be an object")
        for field in ("post_run", "post_cleanup", "post_idle"):
            value = native.get(field)
            if native_enabled and (not isinstance(value, str) or not value):
                fail(f"cycle {expected_sequence} native {field} path missing")
            if not native_enabled and value is not None:
                fail(f"cycle {expected_sequence} unexpectedly emitted native {field}")

    return {"pid": pid, "phase_processes": phase_processes}


def metric_stats(processes: list[dict[str, Any]], metric: str) -> dict[str, int | None]:
    values = [process[metric] for process in processes if isinstance(process.get(metric), int)]
    if not values:
        return {
            "observed_count": 0,
            "first": None,
            "last": None,
            "delta_first_to_last": None,
            "min": None,
            "max": None,
        }
    return {
        "observed_count": len(values),
        "first": values[0],
        "last": values[-1],
        "delta_first_to_last": values[-1] - values[0],
        "min": min(values),
        "max": max(values),
    }


def summarize_phases(phase_processes: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        phase: {metric: metric_stats(processes, metric) for metric in PROCESS_METRICS}
        for phase, processes in phase_processes.items()
    }


def build_and_retain_binaries(
    candidate: dict[str, Any], *, target: str, allocator_root: Path
) -> tuple[Path, Path]:
    identity = candidate["identity"]
    state = allocator_registry.target_state(candidate, target)
    if state != "qualified":
        fail(f"allocator {identity} is not qualified for target {target}: {state}")
    profile_smoke.run_checked(build_args(candidate))
    built_workload = Path("target/release/postgres-postgres")
    built_runner = Path("target/release/postgres-postgres-allocator-churn")
    if not built_workload.is_file() or not built_runner.is_file():
        fail(f"allocator {identity} build did not produce required binaries")

    binary_root = allocator_root / "binary"
    binary_root.mkdir(parents=True, exist_ok=True)
    workload = binary_root / "postgres-postgres"
    runner = binary_root / "postgres-postgres-allocator-churn"
    shutil.copy2(built_workload, workload)
    shutil.copy2(built_runner, runner)
    workload.chmod(0o755)
    runner.chmod(0o755)
    return workload.resolve(), runner.resolve()


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
    workload, runner = build_and_retain_binaries(
        candidate, target=target, allocator_root=allocator_root
    )

    database = profile_smoke.database_name(token, f"allocchurn_{identity}")
    paired.compose_db("dropdb", database)
    paired.compose_db("createdb", database, template="template0")
    database_url = paired.database_url(args.base_database_url, database)
    env = base_env.copy()
    env["DATABASE_URL"] = database_url
    env["RUST_LOG"] = "error"

    logs_root = allocator_root / "logs"
    native_root = allocator_root / "allocator-native"
    provenance_root = allocator_root / "provenance"
    logs_root.mkdir(parents=True, exist_ok=True)
    provenance_root.mkdir(parents=True, exist_ok=True)

    telemetry_status = candidate["telemetry"]["status"]
    native_enabled = telemetry_status == "wired"
    selected_campaign = campaign_id(token, identity)

    try:
        profile_smoke.run_checked([str(workload), "migrate"], env=env)
        profile_smoke.run_checked(
            [str(workload), "seed", "--rows", str(ROWS), "--seed", str(SEED)], env=env
        )

        command = [
            str(runner),
            "--campaign-id",
            selected_campaign,
            "--cycles",
            str(args.cycles),
            "--idle-ms",
            str(args.idle_ms),
        ]
        if native_enabled:
            native_root.mkdir(parents=True, exist_ok=True)
            command.extend(["--native-telemetry-root", str(native_root.resolve())])

        completed = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
        safe_stdout = completed.stdout.replace(database_url, "[REDACTED_DATABASE_URL]")
        safe_stderr = completed.stderr.replace(database_url, "[REDACTED_DATABASE_URL]")
        stdout_path = logs_root / "runner.stdout.json"
        stderr_path = logs_root / "runner.stderr.txt"
        stdout_path.write_text(safe_stdout, encoding="utf-8")
        stderr_path.write_text(safe_stderr, encoding="utf-8")
        if completed.returncode != 0:
            fail(
                f"allocator {identity} same-process runner exited {completed.returncode}: "
                f"{safe_stderr[-4000:]}"
            )
        try:
            report = json.loads(safe_stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"allocator {identity} runner stdout is not JSON") from exc

        validated = validate_runner_report(
            report,
            allocator=identity,
            cycles=args.cycles,
            idle_ms=args.idle_ms,
            source_rows=ROWS,
            native_enabled=native_enabled,
        )

        snapshots: list[dict[str, Any]] = []
        if native_enabled:
            for sequence in range(1, args.cycles + 1):
                for phase in NATIVE_PHASES:
                    snapshot_path = native_root / f"{sequence:06}" / phase / "snapshot.json"
                    snapshot = allocator_telemetry_profile.validate_native_snapshot(
                        snapshot_path, identity
                    )
                    snapshots.append(
                        {
                            "sequence": sequence,
                            "phase": phase,
                            "path": str(snapshot_path.relative_to(allocator_root)),
                            "sha256": profile_smoke.sha256_file(snapshot_path),
                            "native_api": snapshot["native_api"],
                        }
                    )

        retained_lock = provenance_root / "Cargo.lock"
        retained_registry = provenance_root / "allocator_registry.json"
        retained_candidate = provenance_root / "allocator.json"
        shutil.copy2(Path("Cargo.lock"), retained_lock)
        shutil.copy2(allocator_registry.DEFAULT_PATH, retained_registry)
        profile_smoke.write_json(retained_candidate, candidate)

        phase_summary = summarize_phases(validated["phase_processes"])
        return {
            "allocator": identity,
            "correctness": "PASS",
            "process_id": validated["pid"],
            "cycles": args.cycles,
            "cursor_cycles": args.cycles // 2,
            "paging_cycles": args.cycles // 2,
            "source_rows": ROWS,
            "phase_metrics": phase_summary,
            "post_idle_retention": {
                metric: phase_summary["post_idle"][metric]
                for metric in ("rss_bytes", "pss_bytes", "smaps_rollup_rss_bytes")
            },
            "telemetry": {
                "status": telemetry_status,
                "capabilities": candidate["telemetry"]["capabilities"],
                "reason": candidate["telemetry"].get("reason"),
                "native_snapshot_count": len(snapshots),
                "native_snapshots": snapshots,
            },
            "runner_stdout": str(stdout_path.relative_to(args.output)),
            "runner_stdout_sha256": profile_smoke.sha256_file(stdout_path),
            "runner_stderr": str(stderr_path.relative_to(args.output)),
            "runner_binary_sha256": profile_smoke.sha256_file(runner),
            "workload_binary_sha256": profile_smoke.sha256_file(workload),
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

    summary = {
        "schema": SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "claim": "same-process-real-workload-allocation-churn",
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
            "cursor_fetch_size": FETCH_SIZE,
            "paging_page_size": PAGE_SIZE,
            "cycles_per_allocator": args.cycles,
            "cursor_cycles": args.cycles // 2,
            "paging_cycles": args.cycles // 2,
            "idle_ms": args.idle_ms,
        },
        "allocators": allocator_results,
        "limitations": [
            "this lane is same-process allocator retention/churn evidence, not benchmark timing",
            "each allocator uses one fresh process that remains alive for all of its cycles",
            "business rows are deleted between cycles while OxideBatch metadata accumulates",
            "job::run recomputes the streaming source digest on every cycle as part of the real workload",
            "phase-boundary process samples do not replace Phase 4 peak-memory measurements",
            "Phase 5A owns per-iteration independent semantic verification; this lane requires successful jobs and exact projection cardinality",
            "allocator-native payloads retain native semantics and are not normalized into one fragmentation score",
            "system and tcmalloc remain explicitly not-wired for native numeric telemetry",
            "rising RSS or PSS alone is not a memory-leak verdict",
            "OxideBatch 0.7.0 observations must not be generalized to later releases",
        ],
    }
    profile_smoke.write_json(args.output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--idle-ms", type=int, default=DEFAULT_IDLE_MS)
    args = parser.parse_args()
    summary = run_campaign(args)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
