#!/usr/bin/env python3
"""Same-host paired PostgreSQL -> PostgreSQL raw sqlx vs OxideBatch benchmark.

Every candidate sample starts from a fresh clone of one deterministic template
PostgreSQL database. Compilation, database creation/cloning, migration, seed,
cleanup, and verification are outside timed intervals. The report is
observational evidence; no numeric performance threshold is enforced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import signal
import statistics
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

TIME_FORMAT = "elapsed=%e\nuser=%U\nsystem=%S\nmax_rss_kib=%M\n"
CANDIDATES = ("oxide", "raw")
READER_MODES = ("cursor", "paging")
COLUMNS_PER_ROW = 7
MAX_PARAMETERS_PER_STATEMENT = 2_000
ROWS_PER_STATEMENT = MAX_PARAMETERS_PER_STATEMENT // COLUMNS_PER_ROW
MAX_BOUND_PARAMETERS = ROWS_PER_STATEMENT * COLUMNS_PER_ROW
MAX_ROWS = 10_000_000
MAX_CHUNK_SIZE = 1_000_000
MAX_READ_BATCH_SIZE = 1_000_000
MAX_WARMUPS = 10
MAX_MEASURED_RUNS = 20
MAX_U64 = (1 << 64) - 1
MARKER_TIMEOUT_SECONDS = 300.0
DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
IMPORT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def run_checked(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, env=env, capture_output=True)


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("cannot compute percentile of empty list")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    weight = rank - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def distribution(values: list[float | int]) -> dict[str, float]:
    if not values:
        raise ValueError("cannot summarize empty values")
    ordered = sorted(float(value) for value in values)
    return {
        "min": min(ordered),
        "median": statistics.median(ordered),
        "max": max(ordered),
        "p95": percentile(ordered, 0.95),
    }


def parse_time_file(path: Path) -> dict[str, float | int]:
    values: dict[str, float | int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        key, value = line.split("=", 1)
        values[key] = int(value) if key == "max_rss_kib" else float(value)
    expected = {"elapsed", "user", "system", "max_rss_kib"}
    if set(values) != expected:
        raise RuntimeError(f"unexpected /usr/bin/time output keys: {sorted(values)}")
    return values


def parse_verify_report(stdout: str, expected_rows: int) -> dict[str, Any]:
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("verifier did not emit valid JSON") from exc
    required = {
        "source_digest",
        "source_rows",
        "destination_rows",
        "row_counts_match",
        "expected_digest_sha256",
        "actual_digest_sha256",
        "digests_match",
        "total_mismatches",
    }
    if not isinstance(report, dict) or not required.issubset(report):
        keys = sorted(report) if isinstance(report, dict) else []
        raise RuntimeError(f"unexpected verifier report keys: {keys}")
    if int(report["source_rows"]) != expected_rows or int(report["destination_rows"]) != expected_rows:
        raise RuntimeError("verifier row counts do not match benchmark configuration")
    if not report["row_counts_match"] or not report["digests_match"]:
        raise RuntimeError(f"final-state verifier mismatch: {json.dumps(report, sort_keys=True)}")
    if int(report["total_mismatches"]) != 0:
        raise RuntimeError(f"verifier reported {report['total_mismatches']} mismatches")
    if report["expected_digest_sha256"] != report["actual_digest_sha256"]:
        raise RuntimeError("verifier digest booleans contradict digest values")
    return report


def candidate_order(ordinal: int) -> tuple[str, str]:
    return CANDIDATES if ordinal % 2 == 0 else tuple(reversed(CANDIDATES))


def recovery_kill_chunk(rows: int, chunk_size: int) -> int:
    chunks = math.ceil(rows / chunk_size)
    if chunks < 4:
        raise ValueError("recovery benchmark requires at least four chunks")
    return chunks // 2 + 1


def writer_metrics(rows: int, chunk_size: int) -> dict[str, int]:
    if rows < 0 or chunk_size <= 0:
        raise ValueError("invalid writer metric input")
    statements = 0
    remaining = rows
    while remaining:
        chunk_rows = min(chunk_size, remaining)
        statements += math.ceil(chunk_rows / ROWS_PER_STATEMENT)
        remaining -= chunk_rows
    return {
        "committed_rows": rows,
        "committed_chunks": math.ceil(rows / chunk_size) if rows else 0,
        "writer_statements": statements,
        "bound_parameters_total": rows * COLUMNS_PER_ROW,
        "max_parameters_per_statement": MAX_PARAMETERS_PER_STATEMENT,
        "rows_per_full_statement": ROWS_PER_STATEMENT,
        "max_bound_parameters_per_full_statement": MAX_BOUND_PARAMETERS,
    }


def paired_ratios(oxide: dict[str, Any], raw: dict[str, Any]) -> dict[str, float]:
    return {
        "elapsed_raw_to_oxide": float(raw["elapsed_seconds"]) / float(oxide["elapsed_seconds"]),
        "throughput_raw_to_oxide": float(raw["rows_per_second"]) / float(oxide["rows_per_second"]),
        "max_rss_raw_to_oxide": float(raw["max_rss_kib"]) / float(oxide["max_rss_kib"]),
        "user_cpu_raw_to_oxide": float(raw["user_cpu_seconds"]) / max(float(oxide["user_cpu_seconds"]), 1e-12),
        "system_cpu_raw_to_oxide": float(raw["system_cpu_seconds"]) / max(float(oxide["system_cpu_seconds"]), 1e-12),
    }


def summarize_pairs(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    if not pairs:
        raise ValueError("cannot summarize empty pair set")
    candidate_summary: dict[str, Any] = {}
    for candidate in CANDIDATES:
        samples = [pair["candidates"][candidate] for pair in pairs]
        candidate_summary[candidate] = {
            "elapsed_seconds": distribution([sample["elapsed_seconds"] for sample in samples]),
            "rows_per_second": distribution([sample["rows_per_second"] for sample in samples]),
            "max_rss_kib": distribution([sample["max_rss_kib"] for sample in samples]),
            "user_cpu_seconds": distribution([sample["user_cpu_seconds"] for sample in samples]),
            "system_cpu_seconds": distribution([sample["system_cpu_seconds"] for sample in samples]),
        }
    ratio_keys = sorted(pairs[0]["ratios"])
    return {
        "measured_pairs": len(pairs),
        "candidates": candidate_summary,
        "paired_ratios": {
            key: distribution([pair["ratios"][key] for pair in pairs]) for key in ratio_keys
        },
    }


def lock_package(lock_path: Path, name: str, version: str) -> dict[str, Any]:
    packages = tomllib.loads(lock_path.read_text(encoding="utf-8")).get("package", [])
    matches = [
        package for package in packages
        if package.get("name") == name and package.get("version") == version
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one Cargo.lock package {name} {version}, found {len(matches)}"
        )
    package = matches[0]
    return {
        "name": package["name"],
        "version": package["version"],
        "source": package.get("source"),
        "checksum": package.get("checksum"),
    }


def optional(command: list[str]) -> str | None:
    try:
        return run_checked(command).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def read_first_matching(path: Path, prefix: str) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def host_provenance(oxide_binary: Path, raw_binary: Path) -> dict[str, Any]:
    lock_path = Path("Cargo.lock")
    return {
        "platform": platform.platform(),
        "uname": " ".join(platform.uname()),
        "logical_cpu_count": os.cpu_count(),
        "cpu_model": read_first_matching(Path("/proc/cpuinfo"), "model name"),
        "memory_total": read_first_matching(Path("/proc/meminfo"), "MemTotal"),
        "rustc": optional(["rustc", "--version"]),
        "cargo": optional(["cargo", "--version"]),
        "postgres_server": optional(["docker", "compose", "exec", "-T", "postgres", "postgres", "--version"]),
        "postgres_configured_images": (optional(["docker", "compose", "config", "--images"]) or "").splitlines(),
        "postgres_image_id": optional(["docker", "compose", "images", "-q", "postgres"]),
        "oxide_batch_subject": lock_package(lock_path, "oxide-batch", "0.6.0"),
        "raw_sqlx_subject": lock_package(lock_path, "sqlx", "0.9.0"),
        "binaries": {
            "oxide": {"path": str(oxide_binary), "sha256": sha256_file(oxide_binary)},
            "raw": {"path": str(raw_binary), "sha256": sha256_file(raw_binary)},
        },
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


def database_url(base_database_url: str, database: str) -> str:
    if not DATABASE_NAME.fullmatch(database):
        raise ValueError(f"unsafe PostgreSQL database name: {database}")
    parsed = urlsplit(base_database_url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.netloc:
        raise ValueError("base database URL must be a PostgreSQL URL")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment))


def compose_db(command: str, database: str, *, template: str | None = None) -> None:
    if command not in {"createdb", "dropdb"} or not DATABASE_NAME.fullmatch(database):
        raise ValueError("invalid PostgreSQL database operation")
    args = ["docker", "compose", "exec", "-T", "postgres", command]
    if command == "dropdb":
        args.extend(["--if-exists", "--force"])
    args.extend(["-U", "oxide_batch_workload"])
    if template is not None:
        if command != "createdb" or not DATABASE_NAME.fullmatch(template):
            raise ValueError("invalid template database")
        args.extend(["-T", template])
    args.append(database)
    run_checked(args)


def template_database_name() -> str:
    run_id = re.sub(r"[^0-9]", "", os.getenv("GITHUB_RUN_ID", ""))[-12:] or "local"
    return f"bench_template_{run_id}"[:63]


def sample_database_name(mode: str, kind: str, ordinal: int, candidate: str) -> str:
    mode_token = {"cursor": "c", "paging": "p"}[mode]
    kind_token = {"warmup": "w", "measured": "m", "recovery": "r"}[kind]
    candidate_token = {"oxide": "o", "raw": "r"}[candidate]
    return f"bench_{mode_token}_{kind_token}_{ordinal:02d}_{candidate_token}"


def prepare_template(
    *, name: str, base_database_url: str, oxide_binary: Path, raw_binary: Path,
    rows: int, seed: int, env: dict[str, str]
) -> None:
    compose_db("dropdb", name)
    compose_db("createdb", name, template="template0")
    url = database_url(base_database_url, name)
    run_checked([str(oxide_binary), "migrate", "--database-url", url], env=env)
    run_checked([str(raw_binary), "--database-url", url, "migrate"], env=env)
    run_checked([
        str(oxide_binary), "seed", "--database-url", url,
        "--rows", str(rows), "--seed", str(seed)
    ], env=env)


def clone_sample_database(template: str, sample: str) -> None:
    compose_db("dropdb", sample)
    compose_db("createdb", sample, template=template)


def query_business_state(database: str, import_name: str) -> dict[str, int]:
    if not DATABASE_NAME.fullmatch(database) or not IMPORT_NAME.fullmatch(import_name):
        raise ValueError("unsafe benchmark database/import name")
    sql = (
        "SELECT count(*), COALESCE(max(customer_id), 0) "
        "FROM app_business.customer_projection "
        f"WHERE import_name = '{import_name}'"
    )
    completed = run_checked([
        "docker", "compose", "exec", "-T", "postgres", "psql",
        "-U", "oxide_batch_workload", "-d", database, "-Atqc", sql
    ])
    fields = completed.stdout.strip().split("|")
    if len(fields) != 2:
        raise RuntimeError(f"unexpected business-state query output: {completed.stdout!r}")
    return {"committed_rows": int(fields[0]), "last_customer_id": int(fields[1])}


def reader_args(mode: str, fetch_size: int, page_size: int) -> list[str]:
    if mode == "cursor":
        return ["--reader", "cursor", "--fetch-size", str(fetch_size)]
    if mode == "paging":
        return ["--reader", "paging", "--page-size", str(page_size)]
    raise ValueError(f"unknown reader mode: {mode}")


def run_command(
    *, candidate: str, oxide_binary: Path, raw_binary: Path,
    database_url_value: str, import_name: str, mode: str,
    chunk_size: int, fetch_size: int, page_size: int
) -> list[str]:
    common = [
        "--import-name", import_name, "--chunk-size", str(chunk_size),
        *reader_args(mode, fetch_size, page_size)
    ]
    if candidate == "oxide":
        return [str(oxide_binary), "run", "--database-url", database_url_value, *common]
    if candidate == "raw":
        return [str(raw_binary), "--database-url", database_url_value, "run", *common]
    raise ValueError(f"unknown candidate: {candidate}")


def verify_final_state(
    *, oxide_binary: Path, database_url_value: str, import_name: str,
    rows: int, env: dict[str, str]
) -> dict[str, Any]:
    completed = subprocess.run([
        str(oxide_binary), "verify", "--database-url", database_url_value,
        "--import-name", import_name
    ], check=False, text=True, env=env, capture_output=True)
    report = parse_verify_report(completed.stdout, rows)
    if completed.returncode != 0:
        raise RuntimeError(f"verifier exited {completed.returncode}: {completed.stderr}")
    return report


def timed_process(command: list[str], *, env: dict[str, str]) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(prefix="pgpg-time-", delete=False) as handle:
        time_path = Path(handle.name)
    try:
        full = ["/usr/bin/time", "-f", TIME_FORMAT, "-o", str(time_path), *command]
        started_at = time.time()
        started_perf = time.perf_counter()
        wrapper = subprocess.Popen(
            full, text=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = wrapper.communicate()
        elapsed = time.perf_counter() - started_perf
        finished_at = time.time()
        metrics = parse_time_file(time_path)
    finally:
        time_path.unlink(missing_ok=True)
    if wrapper.returncode != 0:
        raise RuntimeError(f"candidate exited {wrapper.returncode}: {stderr[-4000:]}")
    if elapsed <= 0:
        raise RuntimeError("timed process reported non-positive elapsed time")
    return {
        "exit_status": wrapper.returncode,
        "time_wrapper_pid": wrapper.pid,
        "started_at_unix": started_at,
        "finished_at_unix": finished_at,
        "elapsed_seconds": elapsed,
        "gnu_time_elapsed_seconds": float(metrics["elapsed"]),
        "user_cpu_seconds": float(metrics["user"]),
        "system_cpu_seconds": float(metrics["system"]),
        "max_rss_kib": int(metrics["max_rss_kib"]),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


def wait_for_marker(marker: Path, wrapper: subprocess.Popen[str]) -> int:
    deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if wrapper.poll() is not None:
            raise RuntimeError(f"candidate exited before crash marker: {wrapper.returncode}")
        if marker.is_file() and marker.stat().st_size > 0:
            first_line = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            match = re.fullmatch(r"pid=([0-9]+)(?: .*)?", first_line)
            pid_text = match.group(1) if match else first_line
            if not pid_text.isdigit():
                raise RuntimeError(f"invalid crash marker pid line: {first_line!r}")
            return int(pid_text)
        time.sleep(0.01)
    raise RuntimeError(f"timed out waiting for crash marker {marker}")


def timed_crash_process(command: list[str], *, env: dict[str, str], marker: Path) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(prefix="pgpg-time-crash-", delete=False) as handle:
        time_path = Path(handle.name)
    marker.unlink(missing_ok=True)
    full = ["/usr/bin/time", "-f", TIME_FORMAT, "-o", str(time_path), *command]
    started_at = time.time()
    started_perf = time.perf_counter()
    wrapper = subprocess.Popen(full, text=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        candidate_pid = wait_for_marker(marker, wrapper)
        os.kill(candidate_pid, signal.SIGKILL)
        stdout, stderr = wrapper.communicate(timeout=30)
        elapsed = time.perf_counter() - started_perf
        finished_at = time.time()
        metrics = parse_time_file(time_path)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()
        time_path.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
    if wrapper.returncode != 137:
        raise RuntimeError(f"crash wrapper exited {wrapper.returncode}, expected 137: {stderr[-4000:]}")
    return {
        "exit_status": wrapper.returncode,
        "candidate_pid": candidate_pid,
        "time_wrapper_pid": wrapper.pid,
        "started_at_unix": started_at,
        "finished_at_unix": finished_at,
        "elapsed_seconds": elapsed,
        "gnu_time_elapsed_seconds": float(metrics["elapsed"]),
        "user_cpu_seconds": float(metrics["user"]),
        "system_cpu_seconds": float(metrics["system"]),
        "max_rss_kib": int(metrics["max_rss_kib"]),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


def clean_sample(
    *, candidate: str, mode: str, ordinal: int, kind: str, template: str,
    base_database_url: str, oxide_binary: Path, raw_binary: Path, rows: int,
    chunk_size: int, fetch_size: int, page_size: int, env: dict[str, str]
) -> dict[str, Any]:
    database = sample_database_name(mode, kind, ordinal, candidate)
    import_name = f"bench_{mode}_{kind}_{ordinal}_{candidate}"
    clone_sample_database(template, database)
    url = database_url(base_database_url, database)
    try:
        metrics = timed_process(run_command(
            candidate=candidate, oxide_binary=oxide_binary, raw_binary=raw_binary,
            database_url_value=url, import_name=import_name, mode=mode,
            chunk_size=chunk_size, fetch_size=fetch_size, page_size=page_size
        ), env=env)
        verification = verify_final_state(
            oxide_binary=oxide_binary, database_url_value=url,
            import_name=import_name, rows=rows, env=env
        )
        business = query_business_state(database, import_name)
        if business["committed_rows"] != rows:
            raise RuntimeError("clean sample business row count mismatch")
        metrics.update({
            "candidate": candidate,
            "reader_mode": mode,
            "database": database,
            "import_name": import_name,
            "rows_per_second": rows / float(metrics["elapsed_seconds"]),
            "business_state": business,
            "verification": verification,
            "derived_work": writer_metrics(rows, chunk_size),
            "transaction_counts": {"status": "not-reliably-observed", "commits": None, "rollbacks": None},
        })
        return metrics
    finally:
        compose_db("dropdb", database)


def clean_pair(*, mode: str, ordinal: int, kind: str, order_ordinal: int, **kwargs: Any) -> dict[str, Any]:
    order = candidate_order(order_ordinal)
    candidates: dict[str, Any] = {}
    for candidate in order:
        candidates[candidate] = clean_sample(
            candidate=candidate, mode=mode, ordinal=ordinal, kind=kind, **kwargs
        )
    oxide_digest = candidates["oxide"]["verification"]["source_digest"]
    raw_digest = candidates["raw"]["verification"]["source_digest"]
    if oxide_digest != raw_digest:
        raise RuntimeError("paired candidates did not observe identical source identity")
    return {
        "pair_index": ordinal,
        "order": list(order),
        "source_digest": oxide_digest,
        "candidates": candidates,
        "ratios": paired_ratios(candidates["oxide"], candidates["raw"]),
    }


def recovery_sample(
    *, candidate: str, mode: str, ordinal: int, template: str,
    base_database_url: str, oxide_binary: Path, raw_binary: Path, rows: int,
    chunk_size: int, fetch_size: int, page_size: int, env: dict[str, str]
) -> dict[str, Any]:
    database = sample_database_name(mode, "recovery", ordinal, candidate)
    import_name = f"bench_{mode}_recovery_{candidate}"
    clone_sample_database(template, database)
    url = database_url(base_database_url, database)
    kill_chunk = recovery_kill_chunk(rows, chunk_size)
    expected_durable_rows = min(rows, (kill_chunk - 1) * chunk_size)
    reprocessed_rows = min(chunk_size, rows - expected_durable_rows)
    with tempfile.NamedTemporaryFile(prefix=f"pgpg-{candidate}-{mode}-", suffix=".marker", delete=False) as handle:
        marker = Path(handle.name)
    marker.unlink(missing_ok=True)
    base = run_command(
        candidate=candidate, oxide_binary=oxide_binary, raw_binary=raw_binary,
        database_url_value=url, import_name=import_name, mode=mode,
        chunk_size=chunk_size, fetch_size=fetch_size, page_size=page_size
    )
    crash_command = (
        [*base, "--fail-at-chunk", str(kill_chunk), "--failure-mode", "during-write", "--pause-for-kill", str(marker)]
        if candidate == "oxide"
        else [*base, "--pause-at-chunk", str(kill_chunk), "--pause-phase", "before-commit", "--pause-marker", str(marker)]
    )
    try:
        first_phase = timed_crash_process(crash_command, env=env, marker=marker)
        try:
            os.kill(int(first_phase["candidate_pid"]), 0)
        except ProcessLookupError:
            crashed_process_reaped = True
        else:
            raise RuntimeError("killed candidate PID still exists before recovery invocation")
        durable = query_business_state(database, import_name)
        if durable != {"committed_rows": expected_durable_rows, "last_customer_id": expected_durable_rows}:
            raise RuntimeError(f"unexpected durable prefix after kill: {durable}")

        operator_recover = None
        if candidate == "oxide":
            operator_recover = timed_process([
                str(oxide_binary), "recover", "--database-url", url,
                "--import-name", import_name, "--reader", mode
            ], env=env)

        recovery_phase = timed_process(base, env=env)
        verification = verify_final_state(
            oxide_binary=oxide_binary, database_url_value=url,
            import_name=import_name, rows=rows, env=env
        )
        final_business = query_business_state(database, import_name)
        if final_business["committed_rows"] != rows or final_business["last_customer_id"] != rows:
            raise RuntimeError("recovery final business state mismatch")

        operator_elapsed = float(operator_recover["elapsed_seconds"]) if operator_recover else 0.0
        combined_elapsed = float(first_phase["elapsed_seconds"]) + operator_elapsed + float(recovery_phase["elapsed_seconds"])
        combined_user = float(first_phase["user_cpu_seconds"]) + (float(operator_recover["user_cpu_seconds"]) if operator_recover else 0.0) + float(recovery_phase["user_cpu_seconds"])
        combined_system = float(first_phase["system_cpu_seconds"]) + (float(operator_recover["system_cpu_seconds"]) if operator_recover else 0.0) + float(recovery_phase["system_cpu_seconds"])
        combined_peak_rss = max(
            int(first_phase["max_rss_kib"]),
            int(operator_recover["max_rss_kib"]) if operator_recover else 0,
            int(recovery_phase["max_rss_kib"]),
        )
        crash_attempt_rows = min(rows, kill_chunk * chunk_size)
        crash_work = writer_metrics(crash_attempt_rows, chunk_size)
        resume_work = writer_metrics(rows - expected_durable_rows, chunk_size)
        return {
            "candidate": candidate,
            "reader_mode": mode,
            "database": database,
            "import_name": import_name,
            "kill_chunk": kill_chunk,
            "target_progress_fraction": expected_durable_rows / rows,
            "first_phase": first_phase,
            "crashed_process_reaped_before_recovery": crashed_process_reaped,
            "durable_after_kill": durable,
            "operator_recover": operator_recover,
            "recovery_phase": recovery_phase,
            "combined_active_elapsed_seconds": combined_elapsed,
            "combined_user_cpu_seconds": combined_user,
            "combined_system_cpu_seconds": combined_system,
            "combined_peak_rss_kib": combined_peak_rss,
            "rows_per_second_combined_active": rows / combined_elapsed,
            "reprocessed_rows": reprocessed_rows,
            "duplicate_rows": 0,
            "skipped_rows": 0,
            "lost_rows": 0,
            "final_business_state": final_business,
            "verification": verification,
            "derived_work": {
                "first_phase_attempted": crash_work,
                "recovery_phase": resume_work,
                "combined_writer_statements": crash_work["writer_statements"] + resume_work["writer_statements"],
                "combined_bound_parameters": (rows + reprocessed_rows) * COLUMNS_PER_ROW,
            },
            "transaction_counts": {"status": "not-reliably-observed", "commits": None, "rollbacks": None},
        }
    finally:
        marker.unlink(missing_ok=True)
        compose_db("dropdb", database)


def recovery_pair(*, mode: str, ordinal: int, order_ordinal: int, **kwargs: Any) -> dict[str, Any]:
    order = candidate_order(order_ordinal)
    candidates: dict[str, Any] = {}
    for candidate in order:
        candidates[candidate] = recovery_sample(
            candidate=candidate, mode=mode, ordinal=ordinal, **kwargs
        )
    oxide = candidates["oxide"]
    raw = candidates["raw"]
    if oxide["verification"]["source_digest"] != raw["verification"]["source_digest"]:
        raise RuntimeError("recovery pair source identities differ")
    return {
        "order": list(order),
        "source_digest": oxide["verification"]["source_digest"],
        "candidates": candidates,
        "ratios": {
            "combined_elapsed_raw_to_oxide": raw["combined_active_elapsed_seconds"] / oxide["combined_active_elapsed_seconds"],
            "combined_throughput_raw_to_oxide": raw["rows_per_second_combined_active"] / oxide["rows_per_second_combined_active"],
            "combined_peak_rss_raw_to_oxide": raw["combined_peak_rss_kib"] / oxide["combined_peak_rss_kib"],
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oxide-binary", type=Path, required=True)
    parser.add_argument("--raw-binary", type=Path, required=True)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--chunk-size", type=int, required=True)
    parser.add_argument("--fetch-size", type=int, required=True)
    parser.add_argument("--page-size", type=int, required=True)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--measured-runs", type=int, default=7)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    bounded = {
        "rows": (1, MAX_ROWS),
        "seed": (0, MAX_U64),
        "chunk_size": (1, MAX_CHUNK_SIZE),
        "fetch_size": (1, MAX_READ_BATCH_SIZE),
        "page_size": (1, MAX_READ_BATCH_SIZE),
        "warmups": (0, MAX_WARMUPS),
        "measured_runs": (1, MAX_MEASURED_RUNS),
    }
    for name, (minimum, maximum) in bounded.items():
        value = getattr(args, name)
        if not minimum <= value <= maximum:
            raise SystemExit(f"--{name.replace('_', '-')} must be between {minimum} and {maximum}")
    if math.ceil(args.rows / args.chunk_size) < 4:
        raise SystemExit("--rows/--chunk-size must yield at least four chunks for ~50% recovery")
    for binary in (args.oxide_binary, args.raw_binary):
        if not binary.is_file():
            raise SystemExit(f"release binary not found: {binary}")
    if not Path("/usr/bin/time").is_file():
        raise SystemExit("/usr/bin/time is required for resource measurements")
    database_url(args.base_database_url, "validation_db")


def main() -> int:
    args = parse_args()
    validate_args(args)
    env = os.environ.copy()
    env.setdefault("RUST_LOG", "error")
    template = template_database_name()
    report: dict[str, Any] = {
        "schema_version": 1,
        "campaign": "postgres-postgres-raw-vs-oxide-paired",
        "comparison_class": "semantic-parity-minimal-durability",
        "claim_class": "same-host-observational-attribution",
        "performance_threshold": None,
        "configuration": {
            "rows": args.rows,
            "seed": args.seed,
            "chunk_size": args.chunk_size,
            "cursor_fetch_size": args.fetch_size,
            "paging_page_size": args.page_size,
            "warmups_per_mode_candidate": args.warmups,
            "measured_runs_per_mode_candidate": args.measured_runs,
            "recovery_scenarios_per_mode_candidate": 1,
            "writer_parity": {
                "columns_per_row": COLUMNS_PER_ROW,
                "max_parameters_per_statement": MAX_PARAMETERS_PER_STATEMENT,
                "rows_per_full_statement": ROWS_PER_STATEMENT,
                "max_bound_parameters_per_full_statement": MAX_BOUND_PARAMETERS,
            },
            "database_isolation": "fresh-clone-of-one-deterministic-template-per-candidate-sample",
            "candidate_order": "deterministically alternating within paired samples",
            "timed_interval": "candidate run only; recovery combines killed run + explicit operator recover when required + resume run",
        },
        "limitations": [
            "Raw metadata intentionally omits OxideBatch's broader lifecycle/history/diagnostic surface.",
            "Transaction/commit/rollback counts are not reported because no symmetric, non-perturbing observation was available; fields are explicitly null.",
            "GitHub-hosted runner measurements are observational distributions, not merge-blocking performance thresholds.",
            "Recovery timing includes external harness reaction latency between marker observation and SIGKILL; the same mechanism is used for both candidates.",
        ],
        "clean": {},
        "recovery": {},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    return_code = 1
    try:
        report["environment"] = host_provenance(args.oxide_binary, args.raw_binary)
        prepare_template(
            name=template, base_database_url=args.base_database_url,
            oxide_binary=args.oxide_binary, raw_binary=args.raw_binary,
            rows=args.rows, seed=args.seed, env=env
        )
        shared = {
            "template": template,
            "base_database_url": args.base_database_url,
            "oxide_binary": args.oxide_binary,
            "raw_binary": args.raw_binary,
            "rows": args.rows,
            "chunk_size": args.chunk_size,
            "fetch_size": args.fetch_size,
            "page_size": args.page_size,
            "env": env,
        }
        order_ordinal = 0
        for mode in READER_MODES:
            warmup_pairs = []
            measured_pairs = []
            for index in range(args.warmups):
                warmup_pairs.append(clean_pair(
                    mode=mode, ordinal=index + 1, kind="warmup",
                    order_ordinal=order_ordinal, **shared
                ))
                order_ordinal += 1
            for index in range(args.measured_runs):
                measured_pairs.append(clean_pair(
                    mode=mode, ordinal=index + 1, kind="measured",
                    order_ordinal=order_ordinal, **shared
                ))
                order_ordinal += 1
            report["clean"][mode] = {
                "warmup_pairs": warmup_pairs,
                "measured_pairs": measured_pairs,
                "summary": summarize_pairs(measured_pairs),
            }
        for mode in READER_MODES:
            report["recovery"][mode] = recovery_pair(
                mode=mode, ordinal=1, order_ordinal=order_ordinal, **shared
            )
            order_ordinal += 1
        report["status"] = "passed"
        return_code = 0
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            compose_db("dropdb", template)
        except Exception as cleanup_exc:
            report.setdefault("cleanup_warnings", []).append(str(cleanup_exc))
            if report.get("status") == "passed":
                report["status"] = "failed"
                report["failure"] = {
                    "type": type(cleanup_exc).__name__,
                    "message": f"template cleanup failed: {cleanup_exc}",
                }
                return_code = 1
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(
        {mode: report.get("clean", {}).get(mode, {}).get("summary", {}) for mode in READER_MODES},
        indent=2,
        sort_keys=True,
    ))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
