#!/usr/bin/env python3
"""Campaign #79 four-way same-host measurement harness.

Reuses #73's already-reviewed paired-harness primitives for PostgreSQL cloning,
GNU-time parsing, independent verification, Cargo provenance, and writer
arithmetic. This file owns only the four-candidate orchestration, Java timing,
balanced rotation, and Java/Spring recovery differences.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PAIRED_PATH = Path(__file__).with_name("paired.py")
PAIRED_SPEC = importlib.util.spec_from_file_location("paired_primitives", PAIRED_PATH)
if PAIRED_SPEC is None or PAIRED_SPEC.loader is None:
    raise RuntimeError(f"cannot load paired benchmark primitives from {PAIRED_PATH}")
base = importlib.util.module_from_spec(PAIRED_SPEC)
PAIRED_SPEC.loader.exec_module(base)

CANDIDATES = ("raw_rust", "oxide", "raw_java", "spring")
READER_MODES = ("cursor", "paging")
PAIRINGS = (
    ("raw_rust", "oxide"),
    ("raw_java", "spring"),
    ("raw_rust", "raw_java"),
    ("oxide", "spring"),
)
MAX_MEASURED_RUNS = 20
DATABASE_NAME = base.DATABASE_NAME
HEX64 = re.compile(r"^[0-9a-f]{64}$")
LAUNCHER_MAIN = "io.oxidebatch.workloads.postgres.benchmark.ci.BenchmarkLauncher"
RAW_JAVA_MAIN = "io.oxidebatch.workloads.postgres.benchmark.rawjdbc.RawJdbcMain"
SPRING_MAIN = "io.oxidebatch.workloads.postgres.benchmark.springbatch.SpringBatchMain"
CANONICAL = dict(rows=1_000_000, seed=20260904, chunk_size=1000, fetch_size=500,
                 page_size=750, warmups=2, measured_runs=8)

distribution = base.distribution
writer_metrics = base.writer_metrics
recovery_kill_chunk = base.recovery_kill_chunk


def candidate_order(ordinal: int) -> tuple[str, ...]:
    shift = ordinal % 4
    return CANDIDATES[shift:] + CANDIDATES[:shift]


def position_counts(rounds: int) -> dict[str, list[int]]:
    out = {candidate: [0, 0, 0, 0] for candidate in CANDIDATES}
    for ordinal in range(rounds):
        for position, candidate in enumerate(candidate_order(ordinal)):
            out[candidate][position] += 1
    return out


def assert_balanced_measured_rounds(rounds: int) -> None:
    if rounds < 4 or rounds > MAX_MEASURED_RUNS or rounds % 4:
        raise ValueError("measured rounds must be between 4 and 20 and divisible by four")
    expected = rounds // 4
    if any(counts != [expected] * 4 for counts in position_counts(rounds).values()):
        raise ValueError("candidate-position exposure is not balanced")


def parse_java_metrics(stderr: str, elapsed: float) -> dict[str, Any]:
    pids = re.findall(r"^OXIDEBATCH_BENCH_PID=([0-9]+)$", stderr, re.MULTILINE)
    active = re.findall(r"^OXIDEBATCH_BENCH_ACTIVE_WORK_NS=([0-9]+)$", stderr, re.MULTILINE)
    if len(pids) != 1 or len(active) != 1:
        raise RuntimeError("Java launcher must emit exactly one PID and active-work interval")
    seconds = int(active[0]) / 1_000_000_000.0
    if seconds <= 0 or seconds > elapsed + 0.25:
        raise RuntimeError("invalid Java active-work interval")
    return {
        "candidate_pid": int(pids[0]),
        "active_work_seconds": seconds,
        "jvm_launcher_bootstrap_seconds": max(0.0, elapsed - seconds),
        "application_context_bootstrap_seconds": None,
        "application_context_bootstrap_status": "not-separately-observed",
    }


def parse_process_pid(stderr: str) -> int:
    values = re.findall(r"^OXIDEBATCH_PROCESS_PID=([0-9]+)$", stderr, re.MULTILINE)
    if len(values) != 1:
        raise RuntimeError("timed wrapper must emit exactly one process PID")
    return int(values[0])


def parse_verify_report(stdout: str, rows: int) -> dict[str, Any]:
    report = base.parse_verify_report(stdout, rows)
    digest = report.get("source_digest")
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise RuntimeError("verifier source digest is not canonical SHA-256")
    return report


def jdbc_url(base_url: str, database: str) -> str:
    if not DATABASE_NAME.fullmatch(database):
        raise ValueError("unsafe PostgreSQL database name")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise ValueError("base database URL must be PostgreSQL")
    port = f":{parsed.port}" if parsed.port else ""
    query = f"?{parsed.query}" if parsed.query else ""
    return f"jdbc:postgresql://{parsed.hostname}{port}/{database}{query}"


def db_credentials(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url)
    if parsed.username is None:
        raise ValueError("base database URL must include user")
    return parsed.username, parsed.password or ""


def java_env(env: dict[str, str], base_url: str, database: str, candidate: str) -> dict[str, str]:
    user, password = db_credentials(base_url)
    prefix = "RAW_JDBC" if candidate == "raw_java" else "SPRING_BATCH"
    out = env.copy()
    out[f"{prefix}_DATABASE_URL"] = jdbc_url(base_url, database)
    out[f"{prefix}_DATABASE_USER"] = user
    out[f"{prefix}_DATABASE_PASSWORD"] = password
    return out


def java_command(classes: Path, classpath: str, target: str, args: list[str]) -> list[str]:
    return ["java", "-cp", f"{classes}:{classpath}", LAUNCHER_MAIN, target, *args]


def reader_args(mode: str, fetch_size: int, page_size: int) -> list[str]:
    return ["--reader", mode, "--fetch-size", str(fetch_size)] if mode == "cursor" \
        else ["--reader", mode, "--page-size", str(page_size)]


def command(candidate: str, *, oxide: Path, raw_rust: Path, classes: Path,
            raw_java_cp: str, spring_cp: str, url: str, import_name: str,
            mode: str, chunk_size: int, fetch_size: int, page_size: int) -> list[str]:
    common = ["--import-name", import_name, "--chunk-size", str(chunk_size),
              *reader_args(mode, fetch_size, page_size)]
    if candidate == "oxide":
        return [str(oxide), "run", "--database-url", url, *common]
    if candidate == "raw_rust":
        return [str(raw_rust), "--database-url", url, "run", *common]
    if candidate == "raw_java":
        return java_command(classes, raw_java_cp, RAW_JAVA_MAIN, ["run", *common])
    if candidate == "spring":
        return java_command(classes, spring_cp, SPRING_MAIN, ["run", *common])
    raise ValueError(candidate)


def migrate_command(candidate: str, *, oxide: Path, raw_rust: Path, classes: Path,
                    raw_java_cp: str, spring_cp: str, url: str) -> list[str]:
    if candidate == "oxide":
        return [str(oxide), "migrate", "--database-url", url]
    if candidate == "raw_rust":
        return [str(raw_rust), "--database-url", url, "migrate"]
    if candidate == "raw_java":
        return java_command(classes, raw_java_cp, RAW_JAVA_MAIN, ["migrate"])
    if candidate == "spring":
        return java_command(classes, spring_cp, SPRING_MAIN, ["migrate"])
    raise ValueError(candidate)


def template_name() -> str:
    token = re.sub(r"[^0-9]", "", os.getenv("GITHUB_RUN_ID", ""))[-12:] or "local"
    return f"fw_template_{token}"[:63]


def sample_database_name(mode: str, kind: str, ordinal: int, candidate: str) -> str:
    m = {"cursor": "c", "paging": "p"}[mode]
    k = {"warmup": "w", "measured": "m", "recovery": "r"}[kind]
    c = {"raw_rust": "rr", "oxide": "ox", "raw_java": "rj", "spring": "sp"}[candidate]
    return f"fw_{m}_{k}_{ordinal:02d}_{c}"


def import_name(mode: str, kind: str, ordinal: int, candidate: str) -> str:
    value = f"fw_{mode}_{kind}_{ordinal}_{candidate}"
    if not base.IMPORT_NAME.fullmatch(value):
        raise ValueError("unsafe import name")
    return value


def prepare_template(args: argparse.Namespace, env: dict[str, str], name: str) -> None:
    base.compose_db("dropdb", name)
    base.compose_db("createdb", name, template="template0")
    url = base.database_url(args.base_database_url, name)
    # Oxide owns canonical app/framework schema initialization. Benchmark-owned
    # metadata migrations are layered on only after that foundation exists.
    for candidate in ("oxide", "raw_rust", "raw_java", "spring"):
        candidate_env = java_env(env, args.base_database_url, name, candidate) \
            if candidate in {"raw_java", "spring"} else env
        base.run_checked(migrate_command(
            candidate, oxide=args.oxide_binary, raw_rust=args.raw_rust_binary,
            classes=args.launcher_classes, raw_java_cp=args.raw_java_classpath,
            spring_cp=args.spring_classpath, url=url), env=candidate_env)
    base.run_checked([str(args.oxide_binary), "seed", "--database-url", url,
                      "--rows", str(args.rows), "--seed", str(args.seed)], env=env)


def query_scalar(database: str, sql: str) -> str:
    return base.run_checked(["docker", "compose", "exec", "-T", "postgres", "psql",
                             "-U", "oxide_batch_workload", "-d", database, "-Atqc", sql]).stdout.strip()


def durability_state(database: str, candidate: str, name: str, mode: str) -> dict[str, Any]:
    if candidate in {"raw_rust", "raw_java"}:
        table = "benchmark_raw.checkpoint" if candidate == "raw_rust" else "benchmark_java.raw_checkpoint"
        value = query_scalar(database, f"SELECT last_customer_id, committed_chunks, committed_rows FROM {table} "
                                       f"WHERE import_name = '{name}' AND reader_mode = '{mode}'")
        if not value:
            return {"status": "missing"}
        last_id, chunks, rows = value.split("|")
        return {"status": "observed", "last_customer_id": int(last_id),
                "committed_chunks": int(chunks), "committed_rows": int(rows)}
    if candidate == "spring":
        writes = query_scalar(database,
            "SELECT COALESCE(sum(se.write_count),0) FROM spring_batch.batch_step_execution se "
            "JOIN spring_batch.batch_job_execution je ON je.job_execution_id=se.job_execution_id "
            "JOIN spring_batch.batch_job_execution_params p ON p.job_execution_id=je.job_execution_id "
            f"WHERE p.parameter_name='import_name' AND p.parameter_value='{name}'")
        status = query_scalar(database,
            "SELECT je.status FROM spring_batch.batch_job_execution je "
            "JOIN spring_batch.batch_job_execution_params p ON p.job_execution_id=je.job_execution_id "
            f"WHERE p.parameter_name='import_name' AND p.parameter_value='{name}' "
            "ORDER BY je.job_execution_id DESC LIMIT 1")
        return {"status": "observed", "write_count": int(writes), "latest_status": status}
    return {"status": "not-directly-observed",
            "reason": "benchmark does not query OxideBatch private metadata"}


def timed_argv(command_: list[str], time_file: Path) -> list[str]:
    shell = (
        'if [[ -n "${OXIDEBATCH_SPAWN_PID_FILE:-}" ]]; then '
        'printf "%s\n" "$$" > "$OXIDEBATCH_SPAWN_PID_FILE"; fi; '
        'echo "OXIDEBATCH_PROCESS_PID=$$" >&2; exec "$@"'
    )
    return ["/usr/bin/time", "-q", "-f", base.TIME_FORMAT, "-o", str(time_file),
            "bash", "-c", shell, "benchmark-timed", *command_]


def timed_process(command_: list[str], *, env: dict[str, str], java: bool) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(prefix="fw-time-", delete=False) as handle:
        time_file = Path(handle.name)
    started_at, started_perf = time.time(), time.perf_counter()
    proc = subprocess.Popen(timed_argv(command_, time_file), text=True, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate()
        elapsed = time.perf_counter() - started_perf
        metrics = base.parse_time_file(time_file)
    finally:
        time_file.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"candidate exited {proc.returncode}: {stderr[-4000:]}")
    pid = parse_process_pid(stderr)
    result: dict[str, Any] = {
        "exit_status": 0, "candidate_pid": pid, "time_wrapper_pid": proc.pid,
        "started_at_unix": started_at, "finished_at_unix": time.time(),
        "elapsed_seconds": elapsed, "gnu_time_elapsed_seconds": float(metrics["elapsed"]),
        "user_cpu_seconds": float(metrics["user"]), "system_cpu_seconds": float(metrics["system"]),
        "max_rss_kib": int(metrics["max_rss_kib"]),
        "stdout_tail": stdout[-2000:], "stderr_tail": stderr[-4000:],
    }
    if java:
        java_metrics = parse_java_metrics(stderr, elapsed)
        if java_metrics["candidate_pid"] != pid:
            raise RuntimeError("Java launcher PID differs from timed candidate PID")
        result.update(java_metrics)
    else:
        result.update(active_work_seconds=elapsed, jvm_launcher_bootstrap_seconds=None,
                      application_context_bootstrap_seconds=None,
                      application_context_bootstrap_status="not-applicable")
    return result


def marker_pid(path: Path) -> int:
    first = path.read_text(encoding="utf-8").splitlines()[0].strip()
    match = re.fullmatch(r"pid=([0-9]+)(?: .*)?", first)
    value = match.group(1) if match else first
    if not value.isdigit():
        raise RuntimeError("invalid crash marker PID")
    return int(value)


def timed_crash(command_: list[str], *, env: dict[str, str], marker: Path, java: bool) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(prefix="fw-crash-time-", delete=False) as handle:
        time_file = Path(handle.name)
    active_start: Path | None = None
    spawn_pid_file: Path | None = None
    crash_env = env.copy()
    with tempfile.NamedTemporaryFile(prefix="fw-spawn-pid-", delete=False) as handle:
        spawn_pid_file = Path(handle.name)
    spawn_pid_file.unlink(missing_ok=True)
    crash_env["OXIDEBATCH_SPAWN_PID_FILE"] = str(spawn_pid_file)
    if java:
        with tempfile.NamedTemporaryFile(prefix="fw-active-", delete=False) as handle:
            active_start = Path(handle.name)
        active_start.unlink(missing_ok=True)
        crash_env["OXIDEBATCH_BENCH_ACTIVE_START_FILE"] = str(active_start)
    marker.unlink(missing_ok=True)
    started_at, started_perf = time.time(), time.perf_counter()
    proc = subprocess.Popen(timed_argv(command_, time_file), text=True, env=crash_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + base.MARKER_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("candidate exited before crash marker")
            if marker.is_file() and not marker.is_symlink() and marker.stat().st_size:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("timed out waiting for crash marker")
        killed_pid = marker_pid(marker)
        if spawn_pid_file is None or not spawn_pid_file.is_file():
            raise RuntimeError("timed wrapper did not emit spawned candidate PID before crash")
        spawn_text = spawn_pid_file.read_text(encoding="utf-8").strip()
        if not spawn_text.isdigit() or int(spawn_text) != killed_pid:
            raise RuntimeError(
                f"semantic marker PID {killed_pid} differs from spawned candidate PID {spawn_text!r}"
            )
        try:
            os.kill(killed_pid, 0)
        except ProcessLookupError as exc:
            raise RuntimeError("candidate died before parent SIGKILL") from exc
        os.kill(killed_pid, signal.SIGKILL)
        stdout, stderr = proc.communicate(timeout=30)
        elapsed, finished_at = time.perf_counter() - started_perf, time.time()
        metrics = base.parse_time_file(time_file)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        time_file.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        if spawn_pid_file is not None:
            spawn_pid_file.unlink(missing_ok=True)
    if proc.returncode != 137:
        raise RuntimeError(f"crash process exited {proc.returncode}, expected 137")
    pid = parse_process_pid(stderr)
    if pid != killed_pid:
        raise RuntimeError("semantic marker PID differs from spawned candidate PID")
    active = elapsed
    if java:
        if active_start is None or not active_start.is_file():
            raise RuntimeError("Java crash path did not emit active-start marker")
        text = active_start.read_text(encoding="utf-8").strip()
        match = re.fullmatch(r"pid=([0-9]+) active_start_epoch_ns=([0-9]+)", text)
        active_start.unlink(missing_ok=True)
        if not match or int(match.group(1)) != pid:
            raise RuntimeError("invalid Java active-start marker")
        active = finished_at - int(match.group(2)) / 1_000_000_000.0
        if active <= 0 or active > elapsed + 0.25:
            raise RuntimeError("invalid Java crash active interval")
    return {
        "exit_status": 137, "candidate_pid": pid, "time_wrapper_pid": proc.pid,
        "started_at_unix": started_at, "finished_at_unix": finished_at,
        "elapsed_seconds": elapsed, "gnu_time_elapsed_seconds": float(metrics["elapsed"]),
        "active_work_seconds": active, "user_cpu_seconds": float(metrics["user"]),
        "system_cpu_seconds": float(metrics["system"]), "max_rss_kib": int(metrics["max_rss_kib"]),
        "stdout_tail": stdout[-2000:], "stderr_tail": stderr[-4000:],
    }


def clean_sample(candidate: str, *, mode: str, ordinal: int, kind: str,
                 template: str, args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    database = sample_database_name(mode, kind, ordinal, candidate)
    name = import_name(mode, kind, ordinal, candidate)
    base.clone_sample_database(template, database)
    url = base.database_url(args.base_database_url, database)
    candidate_env = java_env(env, args.base_database_url, database, candidate) \
        if candidate in {"raw_java", "spring"} else env
    try:
        metrics = timed_process(command(
            candidate, oxide=args.oxide_binary, raw_rust=args.raw_rust_binary,
            classes=args.launcher_classes, raw_java_cp=args.raw_java_classpath,
            spring_cp=args.spring_classpath, url=url, import_name=name, mode=mode,
            chunk_size=args.chunk_size, fetch_size=args.fetch_size, page_size=args.page_size),
            env=candidate_env, java=candidate in {"raw_java", "spring"})
        verified = base.verify_final_state(oxide_binary=args.oxide_binary,
            database_url_value=url, import_name=name, rows=args.rows, env=env)
        verified = parse_verify_report(json.dumps(verified), args.rows)
        business = base.query_business_state(database, name)
        if business != {"committed_rows": args.rows, "last_customer_id": args.rows}:
            raise RuntimeError("clean business state mismatch")
        metrics.update(candidate=candidate, reader_mode=mode, database=database,
            import_name=name, rows_per_second=args.rows / metrics["elapsed_seconds"],
            active_rows_per_second=args.rows / metrics["active_work_seconds"],
            business_state=business, durability_state=durability_state(database, candidate, name, mode),
            verification=verified, derived_work=writer_metrics(args.rows, args.chunk_size),
            transaction_counts={"status": "not-reliably-observed", "commits": None, "rollbacks": None})
        return metrics
    finally:
        base.compose_db("dropdb", database)


def ratio(samples: dict[str, Any], a: str, b: str) -> dict[str, Any]:
    left, right = samples[a], samples[b]
    return {"a": a, "b": b,
            "elapsed_b_over_a": right["elapsed_seconds"] / left["elapsed_seconds"],
            "throughput_b_over_a": right["rows_per_second"] / left["rows_per_second"],
            "max_rss_b_over_a": right["max_rss_kib"] / max(left["max_rss_kib"], 1),
            "active_elapsed_b_over_a": right["active_work_seconds"] / left["active_work_seconds"]}


def clean_round(mode: str, ordinal: int, kind: str, *, template: str,
                args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    order, samples = candidate_order(ordinal - 1), {}
    for candidate in order:
        samples[candidate] = clean_sample(candidate, mode=mode, ordinal=ordinal, kind=kind,
                                           template=template, args=args, env=env)
    digests = {samples[c]["verification"]["source_digest"] for c in CANDIDATES}
    if len(digests) != 1:
        raise RuntimeError("four candidates observed different source identities")
    return {"round_index": ordinal, "order": list(order), "source_digest": next(iter(digests)),
            "candidates": samples, "paired_ratios": [ratio(samples, a, b) for a, b in PAIRINGS]}


def summarize(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    candidates: dict[str, Any] = {}
    for candidate in CANDIDATES:
        samples = [r["candidates"][candidate] for r in rounds]
        candidates[candidate] = {metric: distribution([s[metric] for s in samples]) for metric in
            ("elapsed_seconds", "rows_per_second", "active_work_seconds", "active_rows_per_second",
             "max_rss_kib", "user_cpu_seconds", "system_cpu_seconds")}
        if candidate in {"raw_java", "spring"}:
            candidates[candidate]["jvm_launcher_bootstrap_seconds"] = distribution(
                [s["jvm_launcher_bootstrap_seconds"] for s in samples])
    pairs: dict[str, Any] = {}
    for a, b in PAIRINGS:
        values = [next(x for x in r["paired_ratios"] if x["a"] == a and x["b"] == b) for r in rounds]
        pairs[f"{a}__vs__{b}"] = {metric: distribution([v[metric] for v in values]) for metric in
            ("elapsed_b_over_a", "throughput_b_over_a", "max_rss_b_over_a", "active_elapsed_b_over_a")}
    return {"measured_rounds": len(rounds), "candidate_position_counts": position_counts(len(rounds)),
            "candidates": candidates, "paired_ratios": pairs}


def crash_command(candidate: str, normal: list[str], marker: Path, chunk: int) -> list[str]:
    if candidate == "oxide":
        return [*normal, "--fail-at-chunk", str(chunk), "--failure-mode", "during-write",
                "--pause-for-kill", str(marker)]
    return [*normal, "--pause-at-chunk", str(chunk), "--pause-phase", "before-commit",
            "--pause-marker", str(marker)]


def recover_command(candidate: str, *, args: argparse.Namespace, url: str, name: str, mode: str) -> list[str] | None:
    if candidate == "oxide":
        return [str(args.oxide_binary), "recover", "--database-url", url,
                "--import-name", name, "--reader", mode]
    if candidate == "spring":
        return java_command(args.launcher_classes, args.spring_classpath, SPRING_MAIN,
            ["recover", "--import-name", name, "--chunk-size", str(args.chunk_size),
             *reader_args(mode, args.fetch_size, args.page_size)])
    return None


def recovery_sample(candidate: str, *, mode: str, ordinal: int, template: str,
                    args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    database = sample_database_name(mode, "recovery", ordinal, candidate)
    name = import_name(mode, "recovery", ordinal, candidate)
    base.clone_sample_database(template, database)
    url = base.database_url(args.base_database_url, database)
    candidate_env = java_env(env, args.base_database_url, database, candidate) \
        if candidate in {"raw_java", "spring"} else env
    kill_chunk = recovery_kill_chunk(args.rows, args.chunk_size)
    durable_rows = min(args.rows, (kill_chunk - 1) * args.chunk_size)
    normal = command(candidate, oxide=args.oxide_binary, raw_rust=args.raw_rust_binary,
        classes=args.launcher_classes, raw_java_cp=args.raw_java_classpath,
        spring_cp=args.spring_classpath, url=url, import_name=name, mode=mode,
        chunk_size=args.chunk_size, fetch_size=args.fetch_size, page_size=args.page_size)
    try:
        with tempfile.TemporaryDirectory(prefix=f"fw-{candidate}-{mode}-") as tmp:
            marker = Path(tmp) / "marker"
            first = timed_crash(crash_command(candidate, normal, marker, kill_chunk), env=candidate_env,
                                marker=marker, java=candidate in {"raw_java", "spring"})
        try:
            os.kill(first["candidate_pid"], 0)
        except ProcessLookupError:
            crashed_gone = True
        else:
            raise RuntimeError("killed candidate PID still exists before recovery")
        durable_business = base.query_business_state(database, name)
        if durable_business != {"committed_rows": durable_rows, "last_customer_id": durable_rows}:
            raise RuntimeError(f"unexpected durable prefix: {durable_business}")
        durable_metadata = durability_state(database, candidate, name, mode)
        if candidate in {"raw_rust", "raw_java"}:
            if durable_metadata.get("committed_rows") != durable_rows:
                raise RuntimeError(f"raw durability prefix differs from business prefix: {durable_metadata}")
        elif candidate == "spring":
            if durable_metadata.get("write_count") != durable_rows or durable_metadata.get("latest_status") != "STARTED":
                raise RuntimeError(f"Spring crash metadata does not prove STARTED durable prefix: {durable_metadata}")
        operator = None
        recover = recover_command(candidate, args=args, url=url, name=name, mode=mode)
        if recover:
            operator = timed_process(recover, env=candidate_env, java=candidate == "spring")
            if operator["candidate_pid"] == first["candidate_pid"]:
                raise RuntimeError("operator recovery reused crashed PID")
        continuation = timed_process(normal, env=candidate_env, java=candidate in {"raw_java", "spring"})
        forbidden = {first["candidate_pid"]} | ({operator["candidate_pid"]} if operator else set())
        if continuation["candidate_pid"] in forbidden:
            raise RuntimeError("continuation did not run in a genuinely new process")
        verified = base.verify_final_state(oxide_binary=args.oxide_binary,
            database_url_value=url, import_name=name, rows=args.rows, env=env)
        verified = parse_verify_report(json.dumps(verified), args.rows)
        final_business = base.query_business_state(database, name)
        if final_business != {"committed_rows": args.rows, "last_customer_id": args.rows}:
            raise RuntimeError("recovery final business state mismatch")
        phases = [first] + ([operator] if operator else []) + [continuation]
        combined_elapsed = sum(p["elapsed_seconds"] for p in phases)
        combined_active = sum(p["active_work_seconds"] for p in phases)
        return {"candidate": candidate, "reader_mode": mode, "kill_chunk": kill_chunk,
                "semantic_boundary": "after-business-write-before-commit",
                "expected_durable_rows_after_kill": durable_rows,
                "durable_business_after_kill": durable_business,
                "durability_state_after_kill": durable_metadata,
                "crashed_process_gone_before_recovery": crashed_gone,
                "first_phase": first, "operator_recover": operator, "continuation": continuation,
                "combined_elapsed_seconds": combined_elapsed,
                "combined_active_work_seconds": combined_active,
                "rows_per_second_combined_active": args.rows / combined_active,
                "combined_peak_rss_kib": max(p["max_rss_kib"] for p in phases),
                "resume_accounting": {"durable_resume_position": durable_rows,
                    "reprocessed_rows": min(args.chunk_size, args.rows - durable_rows),
                    "duplicate_rows": 0, "skipped_rows": 0, "lost_rows": 0},
                "verification": verified, "final_business_state": final_business}
    finally:
        base.compose_db("dropdb", database)


def recovery_round(mode: str, *, template: str, args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    order, samples = candidate_order(0), {}
    for candidate in order:
        samples[candidate] = recovery_sample(candidate, mode=mode, ordinal=1, template=template,
                                               args=args, env=env)
    digests = {samples[c]["verification"]["source_digest"] for c in CANDIDATES}
    if len(digests) != 1:
        raise RuntimeError("recovery candidates observed different source identities")
    ratios = []
    for a, b in PAIRINGS:
        left, right = samples[a], samples[b]
        ratios.append({"a": a, "b": b,
            "combined_elapsed_b_over_a": right["combined_elapsed_seconds"] / left["combined_elapsed_seconds"],
            "combined_active_elapsed_b_over_a": right["combined_active_work_seconds"] / left["combined_active_work_seconds"],
            "combined_peak_rss_b_over_a": right["combined_peak_rss_kib"] / max(left["combined_peak_rss_kib"], 1)})
    return {"order": list(order), "source_digest": next(iter(digests)),
            "candidates": samples, "paired_ratios": ratios}


def dependency_line(path: Path, needle: str) -> str:
    matches = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if needle in line]
    if len(matches) != 1:
        raise RuntimeError(f"expected one dependency-tree line for {needle}, found {len(matches)}")
    return matches[0]


def required_command_output(command_: list[str]) -> str:
    completed = subprocess.run(command_, check=True, text=True, capture_output=True)
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if not output:
        raise RuntimeError(f"provenance command produced no output: {command_!r}")
    return output


def provenance(args: argparse.Namespace) -> dict[str, Any]:
    return {
        **base.host_provenance(args.oxide_binary, args.raw_rust_binary),
        "java": required_command_output(["java", "-version"]),
        "javac": required_command_output(["javac", "-version"]),
        "maven": required_command_output(["mvn", "-version"]),
        "jvm_flags": required_command_output(["java", "-XX:+PrintCommandLineFlags", "-version"]),
        "ambient_jvm_options": {name: os.getenv(name) for name in
                                ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")},
        "java_subjects": {
            "raw_pgjdbc": dependency_line(args.raw_java_dependency_tree, "org.postgresql:postgresql:jar:42.7.13"),
            "spring_core": dependency_line(args.spring_dependency_tree, "org.springframework.batch:spring-batch-core:jar:6.0.5"),
            "spring_infrastructure": dependency_line(args.spring_dependency_tree, "org.springframework.batch:spring-batch-infrastructure:jar:6.0.5"),
            "spring_pgjdbc": dependency_line(args.spring_dependency_tree, "org.postgresql:postgresql:jar:42.7.13"),
        },
        "java_artifacts": {
            "raw_java_jar": {"path": str(args.raw_java_jar), "sha256": base.sha256_file(args.raw_java_jar)},
            "spring_jar": {"path": str(args.spring_jar), "sha256": base.sha256_file(args.spring_jar)},
            "launcher_class": {"path": str(args.launcher_class), "sha256": base.sha256_file(args.launcher_class)},
            "raw_dependency_tree_sha256": base.sha256_file(args.raw_java_dependency_tree),
            "spring_dependency_tree_sha256": base.sha256_file(args.spring_dependency_tree),
        },
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    for name in ("oxide-binary", "raw-rust-binary", "launcher-classes", "launcher-class",
                 "raw-java-jar", "spring-jar", "raw-java-dependency-tree", "spring-dependency-tree"):
        p.add_argument(f"--{name}", type=Path, required=True)
    p.add_argument("--raw-java-classpath", required=True)
    p.add_argument("--spring-classpath", required=True)
    p.add_argument("--base-database-url", required=True)
    for name in ("rows", "seed", "chunk-size", "fetch-size", "page-size"):
        p.add_argument(f"--{name}", type=int, required=True)
    p.add_argument("--warmups", type=int, default=2)
    p.add_argument("--measured-runs", type=int, default=8)
    p.add_argument("--output", type=Path, required=True)
    return p.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name, low, high in (("rows", 1, base.MAX_ROWS), ("seed", 0, base.MAX_U64),
        ("chunk_size", 1, base.MAX_CHUNK_SIZE), ("fetch_size", 1, base.MAX_READ_BATCH_SIZE),
        ("page_size", 1, base.MAX_READ_BATCH_SIZE), ("warmups", 0, base.MAX_WARMUPS)):
        if not low <= getattr(args, name) <= high:
            raise SystemExit(f"{name} out of range")
    try:
        assert_balanced_measured_rounds(args.measured_runs)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if math.ceil(args.rows / args.chunk_size) < 4:
        raise SystemExit("configuration must yield at least four chunks")
    for path in (args.oxide_binary, args.raw_rust_binary, args.launcher_class, args.raw_java_jar,
                 args.spring_jar, args.raw_java_dependency_tree, args.spring_dependency_tree):
        if not path.is_file():
            raise SystemExit(f"required artifact missing: {path}")
    if not args.launcher_classes.is_dir() or not Path("/usr/bin/time").is_file():
        raise SystemExit("launcher classes and /usr/bin/time are required")
    base.database_url(args.base_database_url, "validation_db")
    if any(os.getenv(name) for name in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")):
        raise SystemExit("ambient JVM tuning variables are forbidden")


def canonical(args: argparse.Namespace) -> bool:
    return all(getattr(args, name) == value for name, value in CANONICAL.items())


def main() -> int:
    args = parse_args(); validate_args(args)
    env = os.environ.copy(); env.setdefault("RUST_LOG", "error")
    template = template_name()
    report: dict[str, Any] = {
        "schema_version": 1, "campaign": "postgres-postgres-four-way-attribution", "issue": 79,
        "comparison_class": "semantic-parity-minimal-durability",
        "claim_class": "same-host-observational-attribution", "performance_threshold": None,
        "canonical_configuration": canonical(args),
        "configuration": {"rows": args.rows, "seed": args.seed, "chunk_size": args.chunk_size,
            "cursor_fetch_size": args.fetch_size, "paging_page_size": args.page_size,
            "warmups_per_mode_candidate": args.warmups, "measured_rounds_per_mode": args.measured_runs,
            "recovery_scenarios_per_mode_candidate": 1,
            "candidate_position_counts": position_counts(args.measured_runs),
            "database_isolation": "fresh-clone-of-one-deterministic-template-per-candidate-sample"},
        "interpretation": {
            "raw_rust_vs_oxide": "Rust framework/lifecycle attribution pair",
            "raw_java_vs_spring": "JVM-side framework/lifecycle attribution pair",
            "raw_rust_vs_raw_java": "runtime/driver/system observation, not framework verdict",
            "oxide_vs_spring": "product-level observation; runtime/language differences remain",
            "java_active_work": "target main() interval; JVM pre-main derived separately; application-context bootstrap not separately observed"},
        "limitations": [
            "Hosted-runner measurements are observational, never numeric merge thresholds.",
            "Application-context bootstrap is not separately isolated.",
            "Cross-language product comparisons do not subtract startup.",
            "Transaction counts are not claimed without symmetric non-perturbing observation.",
            "Recovery timing includes marker-to-SIGKILL harness reaction latency."],
        "clean": {}, "recovery": {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    status = 1
    try:
        report["environment"] = provenance(args)
        prepare_template(args, env, template)
        for mode in READER_MODES:
            warmups = [clean_round(mode, i + 1, "warmup", template=template, args=args, env=env)
                       for i in range(args.warmups)]
            measured = [clean_round(mode, i + 1, "measured", template=template, args=args, env=env)
                        for i in range(args.measured_runs)]
            report["clean"][mode] = {"warmup_rounds": warmups, "measured_rounds": measured,
                                     "summary": summarize(measured)}
        for mode in READER_MODES:
            report["recovery"][mode] = recovery_round(mode, template=template, args=args, env=env)
        report["status"] = "passed"; status = 0
    except Exception as exc:
        report["status"] = "failed"; report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            base.compose_db("dropdb", template)
        except Exception as exc:
            report.setdefault("cleanup_warnings", []).append(str(exc))
            if report.get("status") == "passed":
                report["status"] = "failed"; report["failure"] = {"type": type(exc).__name__, "message": f"template cleanup failed: {exc}"}; status = 1
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({m: report.get("clean", {}).get(m, {}).get("summary", {}) for m in READER_MODES}, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
