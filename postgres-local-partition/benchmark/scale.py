#!/usr/bin/env python3
"""Campaign #93 dense single-host local-partition scaling harness.

Every sample is cloned from one deterministic PostgreSQL template. Build,
migration, seed, clone, verification, statistics collection, and cleanup are
outside the durable execution interval used for scaling ratios. Numeric output
is observational only: this harness defines no performance threshold and does
not infer framework bottleneck ownership.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

DENSE_WORKER_POINTS = (1, 2, 4, 8, 16, 32, 64)
DB_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
TIME_FORMAT = "elapsed=%e\nuser=%U\nsystem=%S\nmax_rss_kib=%M\n"
MAX_ROWS = 10_000_000
MAX_PARTITIONS = 1024
MAX_WARMUPS = 3
MAX_MEASURED_RUNS = 14
OBSERVER_READY_TIMEOUT_SECONDS = 10.0
FRAMEWORK_APPLICATION_NAME = "oxide-batch"
BUSINESS_APPLICATION_NAME = "oxide-batch-workload-local-partition"


def run_checked(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, env=env, capture_output=True)


def wait_for_observer_ready(path: Path, observer: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + OBSERVER_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return
        returncode = observer.poll()
        if returncode is not None:
            raise RuntimeError(f"session observer exited before readiness ({returncode})")
        time.sleep(0.01)
    raise RuntimeError("session observer did not become ready before timeout")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: Iterable[float], p: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("cannot compute percentile of empty values")
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def distribution(values: Iterable[float | int]) -> dict[str, float]:
    ordered = [float(value) for value in values]
    if not ordered:
        raise ValueError("cannot summarize empty values")
    return {
        "min": min(ordered),
        "median": statistics.median(ordered),
        "p95": percentile(ordered, 0.95),
        "max": max(ordered),
    }


def parse_worker_points(raw: str) -> tuple[int, ...]:
    try:
        points = tuple(int(value) for value in raw.split(",") if value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("worker points must be comma-separated integers") from exc
    if not points or len(set(points)) != len(points):
        raise argparse.ArgumentTypeError("worker points must be nonempty and unique")
    if any(point not in DENSE_WORKER_POINTS for point in points):
        raise argparse.ArgumentTypeError(f"worker points must be drawn from {DENSE_WORKER_POINTS}")
    if points != tuple(sorted(points)) or points[0] != 1:
        raise argparse.ArgumentTypeError("worker points must be ascending and start at 1")
    return points


def cyclic_order(points: tuple[int, ...], round_index: int) -> tuple[int, ...]:
    offset = round_index % len(points)
    return points[offset:] + points[:offset]


def database_url(base_url: str, database: str) -> str:
    if not DB_NAME.fullmatch(database):
        raise ValueError(f"unsafe PostgreSQL database name: {database}")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
        raise ValueError("base database URL must be PostgreSQL")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment))


def compose_command(compose_files: list[Path], *args: str) -> list[str]:
    command = ["docker", "compose"]
    for path in compose_files:
        command.extend(["-f", str(path)])
    command.extend(args)
    return command


def db_command(compose_files: list[Path], command: str, database: str, *, template: str | None = None) -> None:
    if command not in {"createdb", "dropdb"} or not DB_NAME.fullmatch(database):
        raise ValueError("unsafe database operation")
    args = compose_command(compose_files, "exec", "-T", "postgres", command)
    if command == "dropdb":
        args.extend(["--if-exists", "--force"])
    args.extend(["-U", "oxide_batch_workload"])
    if template is not None:
        if command != "createdb" or not DB_NAME.fullmatch(template):
            raise ValueError("unsafe database template")
        args.extend(["-T", template])
    args.append(database)
    run_checked(args)


def psql_json(compose_files: list[Path], database: str, sql: str) -> Any:
    completed = run_checked(
        compose_command(
            compose_files,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "oxide_batch_workload",
            "-d",
            database,
            "-Atqc",
            sql,
        )
    )
    text = completed.stdout.strip()
    return json.loads(text) if text else None


def pg_stat_statements_available(compose_files: list[Path], database: str) -> bool:
    try:
        value = psql_json(
            compose_files,
            database,
            "SELECT to_json(EXISTS (SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements'))",
        )
        return value is True
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def reset_pg_stat_statements(compose_files: list[Path], database: str) -> bool:
    if not pg_stat_statements_available(compose_files, database):
        return False
    run_checked(
        compose_command(
            compose_files,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "oxide_batch_workload",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-Atqc",
            "SELECT pg_stat_statements_reset()",
        )
    )
    return True


def statement_class(query: str) -> str:
    normalized = " ".join(query.lower().split())
    if "pg_stat_activity" in normalized or "pg_stat_statements" in normalized:
        return "observer"
    if "app_business.local_partition_projection" in normalized or "app_source.local_partition_source" in normalized:
        return "business_or_verifier"
    return "framework_or_runtime"


def pg_statement_snapshot(compose_files: list[Path], database: str) -> dict[str, Any] | None:
    if not pg_stat_statements_available(compose_files, database):
        return None
    sql = """
SELECT COALESCE(json_agg(json_build_object(
  'query', query,
  'calls', calls,
  'total_exec_time_ms', total_exec_time,
  'rows', rows,
  'shared_blks_hit', shared_blks_hit,
  'shared_blks_read', shared_blks_read,
  'temp_blks_written', temp_blks_written
) ORDER BY total_exec_time DESC), '[]'::json)
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
"""
    rows = psql_json(compose_files, database, sql)
    if not isinstance(rows, list):
        raise RuntimeError("pg_stat_statements snapshot was not a JSON array")
    classes: dict[str, dict[str, float]] = {}
    for row in rows:
        category = statement_class(str(row.get("query", "")))
        bucket = classes.setdefault(category, {"calls": 0.0, "total_exec_time_ms": 0.0, "rows": 0.0})
        bucket["calls"] += float(row.get("calls", 0.0))
        bucket["total_exec_time_ms"] += float(row.get("total_exec_time_ms", 0.0))
        bucket["rows"] += float(row.get("rows", 0.0))
    return {"classes": classes, "statements": rows}


def parse_time_file(path: Path) -> dict[str, float | int]:
    values: dict[str, float | int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        key, value = line.split("=", 1)
        values[key] = int(value) if key == "max_rss_kib" else float(value)
    if set(values) != {"elapsed", "user", "system", "max_rss_kib"}:
        raise RuntimeError(f"unexpected /usr/bin/time output: {values}")
    return values


def parse_json_stdout(stdout: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} did not emit one JSON value") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} JSON was not an object")
    return value


def lock_subject(lock_path: Path, name: str, version: str) -> dict[str, Any]:
    import tomllib

    packages = tomllib.loads(lock_path.read_text(encoding="utf-8")).get("package", [])
    matches = [package for package in packages if package.get("name") == name and package.get("version") == version]
    if len(matches) != 1:
        raise RuntimeError(f"expected one lock entry for {name} {version}, found {len(matches)}")
    package = matches[0]
    return {
        "name": name,
        "version": version,
        "source": package.get("source"),
        "checksum": package.get("checksum"),
    }


def optional(command: list[str]) -> str | None:
    try:
        return run_checked(command).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def host_provenance(binary: Path, measurementctl: Path, compose_files: list[Path]) -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "uname": " ".join(platform.uname()),
        "logical_cpu_count": os.cpu_count(),
        "rustc": optional(["rustc", "--version"]),
        "cargo": optional(["cargo", "--version"]),
        "postgres": optional(compose_command(compose_files, "exec", "-T", "postgres", "postgres", "--version")),
        "oxide_batch_subject": lock_subject(Path("Cargo.lock"), "oxide-batch", "0.6.0"),
        "binary_sha256": sha256_file(binary),
        "measurementctl_sha256": sha256_file(measurementctl),
        "github": {
            "repository": os.getenv("GITHUB_REPOSITORY"),
            "sha": os.getenv("GITHUB_SHA"),
            "run_id": os.getenv("GITHUB_RUN_ID"),
            "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
            "runner_name": os.getenv("RUNNER_NAME"),
            "runner_os": os.getenv("RUNNER_OS"),
            "runner_arch": os.getenv("RUNNER_ARCH"),
        },
    }


def unique_token() -> str:
    raw = re.sub(r"[^0-9]", "", os.getenv("GITHUB_RUN_ID", ""))[-10:]
    return raw or str(os.getpid())


def template_name() -> str:
    return f"lp_scale_tpl_{unique_token()}"[:63]


def sample_name(kind: str, ordinal: int, workers: int) -> str:
    kind_token = "w" if kind == "warmup" else "m"
    return f"lp_scale_{unique_token()}_{kind_token}{ordinal:02d}_w{workers}"[:63]


def prepare_template(
    *, compose_files: list[Path], base_url: str, name: str, binary: Path, rows: int, seed: int,
    enable_pg_stat_statements: bool,
) -> dict[str, Any]:
    db_command(compose_files, "dropdb", name)
    db_command(compose_files, "createdb", name, template="template0")
    url = database_url(base_url, name)
    run_checked([str(binary), "--database-url", url, "migrate"])
    if enable_pg_stat_statements:
        run_checked(
            compose_command(
                compose_files,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "oxide_batch_workload",
                "-d",
                name,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "CREATE EXTENSION IF NOT EXISTS pg_stat_statements",
            )
        )
    seed_report = parse_json_stdout(
        run_checked([str(binary), "--database-url", url, "seed", "--rows", str(rows), "--seed", str(seed)]).stdout,
        "seed",
    )
    return seed_report


def validate_session_report(report: dict[str, Any]) -> None:
    if int(report.get("samples", 0)) < 1:
        raise RuntimeError("session observer emitted no successful samples")
    peaks = report.get("peaks")
    if not isinstance(peaks, dict):
        raise RuntimeError("session observer omitted peak map")
    for application_name in (FRAMEWORK_APPLICATION_NAME, BUSINESS_APPLICATION_NAME):
        if application_name not in peaks:
            raise RuntimeError(f"session observer omitted {application_name} bucket")


def run_sample(
    *, compose_files: list[Path], base_url: str, template: str, binary: Path, measurementctl: Path,
    rows: int, partitions: int, workers: int, kind: str, ordinal: int, require_pg_stats: bool,
) -> dict[str, Any]:
    database = sample_name(kind, ordinal, workers)
    db_command(compose_files, "dropdb", database)
    db_command(compose_files, "createdb", database, template=template)
    url = database_url(base_url, database)
    run_name = "scale"
    try:
        pg_stats_enabled = reset_pg_stat_statements(compose_files, database)
        if require_pg_stats and not pg_stats_enabled:
            raise RuntimeError("pg_stat_statements is required but unavailable")

        with tempfile.TemporaryDirectory(prefix="oxide-lp-scale-") as temp:
            temp_path = Path(temp)
            time_path = temp_path / "time.txt"
            session_path = temp_path / "sessions.json"
            ready_path = temp_path / "observer.ready"
            stop_path = temp_path / "observer.stop"
            observer = subprocess.Popen(
                [
                    str(measurementctl),
                    "--database-url",
                    url,
                    "observe-sessions",
                    "--output",
                    str(session_path),
                    "--ready-file",
                    str(ready_path),
                    "--stop-file",
                    str(stop_path),
                    "--interval-ms",
                    "10",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                wait_for_observer_ready(ready_path, observer)
                completed = subprocess.run(
                    [
                        "/usr/bin/time",
                        "-f",
                        TIME_FORMAT,
                        "-o",
                        str(time_path),
                        str(binary),
                        "--database-url",
                        url,
                        "run",
                        "--run-name",
                        run_name,
                        "--rows",
                        str(rows),
                        "--partitions",
                        str(partitions),
                        "--workers",
                        str(workers),
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            finally:
                stop_path.touch()
                observer_stdout, observer_stderr = observer.communicate(timeout=15)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"workload failed ({completed.returncode})\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
                )
            if observer.returncode != 0:
                raise RuntimeError(
                    f"session observer failed ({observer.returncode})\nstdout:\n{observer_stdout}\nstderr:\n{observer_stderr}"
                )
            run_report = parse_json_stdout(completed.stdout, "run")
            session_report = json.loads(session_path.read_text(encoding="utf-8"))
            if not isinstance(session_report, dict):
                raise RuntimeError("session observer JSON was not an object")
            validate_session_report(session_report)
            process_time = parse_time_file(time_path)

        statement_stats = pg_statement_snapshot(compose_files, database) if pg_stats_enabled else None
        timing_report = parse_json_stdout(
            run_checked(
                [str(measurementctl), "--database-url", url, "inspect-timings", "--run-name", run_name]
            ).stdout,
            "timing inspection",
        )
        verify_report = parse_json_stdout(
            run_checked(
                [
                    str(binary),
                    "--database-url",
                    url,
                    "verify",
                    "--run-name",
                    run_name,
                    "--rows",
                    str(rows),
                    "--partitions",
                    str(partitions),
                ]
            ).stdout,
            "verify",
        )
        framework = verify_report.get("framework", {})
        business = verify_report.get("business", {})
        if not framework.get("terminal_state_consistent"):
            raise RuntimeError("independent verifier rejected framework terminal state")
        if business.get("destination_rows") != rows:
            raise RuntimeError("independent verifier row count mismatch")
        if business.get("expected_destination_digest") != business.get("actual_destination_digest"):
            raise RuntimeError("independent verifier digest mismatch")
        if not business.get("range_ownership_complete"):
            raise RuntimeError("independent verifier range ownership mismatch")

        elapsed = float(timing_report["job_created_to_ended_seconds"])
        if elapsed <= 0.0:
            raise RuntimeError("durable execution elapsed must be positive")
        return {
            "kind": kind,
            "ordinal": ordinal,
            "workers": workers,
            "database": database,
            "run_name": run_name,
            "launch_elapsed_seconds": elapsed,
            "rows_per_second": rows / elapsed,
            "durable_timings": timing_report,
            "process_window": {
                "elapsed_seconds": float(process_time["elapsed"]),
                "user_cpu_seconds": float(process_time["user"]),
                "system_cpu_seconds": float(process_time["system"]),
                "max_rss_kib": int(process_time["max_rss_kib"]),
                "note": "process window includes workload-owned post-launch verification; scaling ratios use durable execution elapsed",
            },
            "session_observer": session_report,
            "pg_stat_statements": statement_stats,
            "verification": {
                "peak_active_workers": run_report.get("peak_active_workers"),
                "active_workers_after_join": run_report.get("active_workers_after_join"),
                "destination_digest_sha256": business.get("actual_destination_digest"),
                "source_digest_sha256": business.get("source_digest"),
            },
        }
    finally:
        db_command(compose_files, "dropdb", database)


def summarize(samples: list[dict[str, Any]], points: tuple[int, ...], measured_runs: int) -> dict[str, Any]:
    measured = [sample for sample in samples if sample["kind"] == "measured"]
    if len(measured) != len(points) * measured_runs:
        raise RuntimeError("measured sample count does not match campaign shape")
    per_worker: dict[str, Any] = {}
    for workers in points:
        selected = [sample for sample in measured if sample["workers"] == workers]
        per_worker[str(workers)] = {
            "launch_elapsed_seconds": distribution(sample["launch_elapsed_seconds"] for sample in selected),
            "rows_per_second": distribution(sample["rows_per_second"] for sample in selected),
            "process_max_rss_kib": distribution(sample["process_window"]["max_rss_kib"] for sample in selected),
            "job_worker_duration_seconds": distribution(
                duration
                for sample in selected
                for duration in sample["durable_timings"]["worker_duration_seconds"]
            ),
            "aggregation_tail_seconds": distribution(
                sample["durable_timings"]["aggregation_tail_seconds"] for sample in selected
            ),
        }

    paired: dict[str, list[float]] = {str(workers): [] for workers in points}
    efficiencies: dict[str, list[float]] = {str(workers): [] for workers in points}
    rounds = []
    for round_index in range(measured_runs):
        current = [sample for sample in measured if sample["ordinal"] == round_index]
        by_worker = {sample["workers"]: sample for sample in current}
        if set(by_worker) != set(points):
            raise RuntimeError(f"measured round {round_index} is incomplete")
        baseline = float(by_worker[1]["launch_elapsed_seconds"])
        ratios: dict[str, Any] = {}
        for workers in points:
            speedup = baseline / float(by_worker[workers]["launch_elapsed_seconds"])
            efficiency = speedup / float(workers)
            paired[str(workers)].append(speedup)
            efficiencies[str(workers)].append(efficiency)
            ratios[str(workers)] = {"speedup": speedup, "efficiency": efficiency}
        rounds.append({"round": round_index, "order": list(cyclic_order(points, round_index)), "paired": ratios})

    return {
        "per_worker": per_worker,
        "paired_speedup": {key: distribution(values) for key, values in paired.items()},
        "scaling_efficiency": {key: distribution(values) for key, values in efficiencies.items()},
        "rounds": rounds,
    }


def validate_args(args: argparse.Namespace) -> None:
    if not (1 <= args.rows <= MAX_ROWS):
        raise SystemExit(f"rows must be 1..={MAX_ROWS}")
    if not (1 <= args.partitions <= MAX_PARTITIONS):
        raise SystemExit(f"partitions must be 1..={MAX_PARTITIONS}")
    if args.rows % args.partitions != 0:
        raise SystemExit("rows must be exactly divisible by partitions")
    if max(args.worker_points) > args.partitions:
        raise SystemExit("largest worker point cannot exceed partitions")
    if not (0 <= args.warmups <= MAX_WARMUPS):
        raise SystemExit(f"warmups must be 0..={MAX_WARMUPS}")
    if not (1 <= args.measured_runs <= MAX_MEASURED_RUNS):
        raise SystemExit(f"measured runs must be 1..={MAX_MEASURED_RUNS}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--measurementctl", type=Path, required=True)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--partitions", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--measured-runs", type=int, default=7)
    parser.add_argument("--worker-points", type=parse_worker_points, default=DENSE_WORKER_POINTS)
    parser.add_argument("--compose-file", action="append", type=Path, default=[])
    parser.add_argument("--require-pg-stat-statements", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.compose_file:
        args.compose_file = [Path("docker-compose.yml")]
    validate_args(args)
    for path in (args.binary, args.measurementctl):
        if not path.is_file():
            raise SystemExit(f"binary not found: {path}")

    template = template_name()
    samples: list[dict[str, Any]] = []
    enable_pg_stats = args.require_pg_stat_statements
    try:
        seed_report = prepare_template(
            compose_files=args.compose_file,
            base_url=args.base_database_url,
            name=template,
            binary=args.binary,
            rows=args.rows,
            seed=args.seed,
            enable_pg_stat_statements=enable_pg_stats,
        )
        if args.require_pg_stat_statements and not pg_stat_statements_available(args.compose_file, template):
            raise RuntimeError("canonical campaign requires pg_stat_statements")

        for warmup in range(args.warmups):
            for workers in cyclic_order(args.worker_points, warmup):
                samples.append(
                    run_sample(
                        compose_files=args.compose_file,
                        base_url=args.base_database_url,
                        template=template,
                        binary=args.binary,
                        measurementctl=args.measurementctl,
                        rows=args.rows,
                        partitions=args.partitions,
                        workers=workers,
                        kind="warmup",
                        ordinal=warmup,
                        require_pg_stats=args.require_pg_stat_statements,
                    )
                )
        for round_index in range(args.measured_runs):
            for workers in cyclic_order(args.worker_points, round_index):
                samples.append(
                    run_sample(
                        compose_files=args.compose_file,
                        base_url=args.base_database_url,
                        template=template,
                        binary=args.binary,
                        measurementctl=args.measurementctl,
                        rows=args.rows,
                        partitions=args.partitions,
                        workers=workers,
                        kind="measured",
                        ordinal=round_index,
                        require_pg_stats=args.require_pg_stat_statements,
                    )
                )

        report = {
            "schema_version": 1,
            "campaign": 93,
            "classification": "observational scaling evidence; no numeric regression threshold",
            "configuration": {
                "rows": args.rows,
                "partitions": args.partitions,
                "seed": args.seed,
                "worker_points": list(args.worker_points),
                "warmups": args.warmups,
                "measured_runs": args.measured_runs,
                "sample_isolation": "fresh PostgreSQL clone from one migrated/seeded deterministic template per sample",
                "timed_metric": "durable job execution created_at -> ended_at",
            },
            "source_identity": seed_report,
            "provenance": host_provenance(args.binary, args.measurementctl, args.compose_file),
            "samples": samples,
            "summary": summarize(samples, args.worker_points, args.measured_runs),
            "ownership": {
                "status": "UNKNOWN",
                "optimization_issue_allowed": False,
                "reason": "numeric scaling and resource observations do not by themselves isolate framework ownership; profiling/minimal reproduction is a later gate",
                "known_measurement_limits": [
                    "process CPU/RSS includes workload-owned post-launch verification",
                    "business-pool acquire wait is not directly observable from the unchanged external-consumer run path",
                    "pg_stat_statements process window includes built-in final verification SQL",
                ],
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        db_command(args.compose_file, "dropdb", template)


if __name__ == "__main__":
    main()
