#!/usr/bin/env python3
"""Run campaign #266 system-allocator heap attribution with Perfetto heapprofd."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import heap_profile
import paired
import perfetto_heap_probe
import profile_manifest
import profile_smoke

SYSTEM_ALLOCATOR = "system"
DEFAULT_ROWS = 2_000
DEFAULT_SEED = heap_profile.DEFAULT_SEED
DEFAULT_CHUNK_SIZE = 200
DEFAULT_FETCH_SIZE = 128
DEFAULT_PAGE_SIZE = 128
WORKLOAD_SAMPLING_INTERVAL_BYTES = 4_096
WORKLOAD_BLOCK_CLIENT = True
WORKLOAD_SAMPLING_MODE = "poisson-sampled-attribution"
WORKLOAD_COUNTER_CLAIM = "sampled-attribution-not-exact-allocation-event-count"
RUST_SYMBOL_CLASSIFICATION_MODE = "demangled-or-rust-v0"
RUST_DEMANGLED_SEPARATOR = "::"
RUST_V0_SYMBOL_PREFIX = "_R"
DEFAULT_PROFILER_TIMEOUT_SECONDS = 600
PROFILER_TERMINATE_GRACE_SECONDS = 10
VERSION_PATTERN = re.compile(r"\bversion\s+([^\s]+)", re.IGNORECASE)


def fail(message: str) -> None:
    raise RuntimeError(message)


def sampling_contract() -> dict[str, Any]:
    return {
        "interval_bytes": WORKLOAD_SAMPLING_INTERVAL_BYTES,
        "mode": WORKLOAD_SAMPLING_MODE,
        "block_client": WORKLOAD_BLOCK_CLIENT,
        "overflow_policy": "block-client",
        "allocation_counter_claim": WORKLOAD_COUNTER_CLAIM,
    }


def rust_symbol_contract() -> dict[str, Any]:
    return {
        "mode": RUST_SYMBOL_CLASSIFICATION_MODE,
        "demangled_separator": RUST_DEMANGLED_SEPARATOR,
        "rust_v0_prefix": RUST_V0_SYMBOL_PREFIX,
        "crate_identity_markers": ["postgres_postgres", "oxide_batch"],
    }


def is_rust_symbol_name(name: str) -> bool:
    return (
        RUST_DEMANGLED_SEPARATOR in name
        or name.startswith(RUST_V0_SYMBOL_PREFIX)
    )


def rust_symbol_sql_predicate() -> str:
    return (
        "(instr(name, '::') > 0 "
        "OR substr(name, 1, 2) = '_R')"
    )


def perfetto_import_name(reader: str) -> str:
    value = f"perfetto_heap_system_{reader}"
    if not paired.IMPORT_NAME.fullmatch(value):
        fail(f"unsafe generated Perfetto heap import name: {value!r}")
    return value


def symbolizer_identity() -> dict[str, Any]:
    path = shutil.which("llvm-symbolizer")
    if path is None:
        return {
            "available": False,
            "path": None,
            "sha256": None,
            "version": None,
            "version_output": None,
            "version_exit_status": None,
        }

    completed = subprocess.run(
        [path, "--version"],
        check=False,
        text=True,
        capture_output=True,
    )
    version_output = (completed.stdout or completed.stderr).strip()
    match = VERSION_PATTERN.search(version_output)
    version = match.group(1) if match is not None else None
    return {
        "available": completed.returncode == 0 and bool(version_output) and version is not None,
        "path": path,
        "sha256": profile_smoke.sha256_file(Path(path)),
        "version": version,
        "version_output": version_output,
        "version_exit_status": completed.returncode,
    }


def tool_capability(args: argparse.Namespace) -> dict[str, Any]:
    host = perfetto_heap_probe.host_document()
    script = perfetto_heap_probe.script_record(args.heap_profile_script.resolve())
    tracebox = perfetto_heap_probe.prebuilt_record(
        args.tracebox_binary.resolve(),
        "tracebox",
    )
    preload = perfetto_heap_probe.prebuilt_record(
        args.preload_library.resolve(),
        "preload",
    )
    trace_processor = perfetto_heap_probe.prebuilt_record(
        args.trace_processor_binary.resolve(),
        "trace_processor",
    )
    dependencies = perfetto_heap_probe.dynamic_dependencies(
        args.preload_library.resolve()
    )
    replacement_dependencies = perfetto_heap_probe.replacement_allocator_dependencies(
        dependencies["lines"]
    )
    symbolizer = symbolizer_identity()

    provenance_ok = all(
        record["matches_expected"]
        for record in (script, tracebox, preload, trace_processor)
    )
    allocator_boundary_ok = (
        dependencies["exit_status"] == 0
        and bool(dependencies["lines"])
        and not replacement_dependencies
    )
    supported = (
        host["supported_host"]
        and provenance_ok
        and allocator_boundary_ok
        and symbolizer["available"]
    )

    document = {
        "schema": "oxide-batch-perfetto-heap-workload-tools/v2",
        "campaign": profile_manifest.CAMPAIGN,
        "profiler": "perfetto-heapprofd-host",
        "perfetto": {
            "release": perfetto_heap_probe.PERFETTO_RELEASE,
            "commit": perfetto_heap_probe.PERFETTO_COMMIT,
            "heap_profile_git_blob_sha1": perfetto_heap_probe.HEAP_PROFILE_BLOB_SHA1,
            "prebuilt_roll": perfetto_heap_probe.PREBUILT_ROLL,
            "sampling": sampling_contract(),
        },
        "host": host,
        "provenance": {
            "heap_profile_script": script,
            "tracebox": tracebox,
            "preload": preload,
            "trace_processor": trace_processor,
            "matches_expected": provenance_ok,
        },
        "allocator_boundary": {
            "identity": SYSTEM_ALLOCATOR,
            "interceptor": "glibc LD_PRELOAD",
            "preload_dynamic_dependencies": dependencies,
            "replacement_allocator_dependencies": replacement_dependencies,
            "allocator_semantics_preserved": allocator_boundary_ok,
            "coverage_scope": "system-allocator-malloc-family-interposition",
        },
        "symbolization": {
            "required": True,
            "llvm_symbolizer": symbolizer,
        },
        "supported": supported,
    }
    return document


def require_tool_capability(document: dict[str, Any]) -> None:
    if not document["host"]["supported_host"]:
        fail("Perfetto workload profiling requires Linux aarch64")
    if not document["provenance"]["matches_expected"]:
        fail("Perfetto workload profiling inputs do not match pinned provenance")
    if not document["allocator_boundary"]["allocator_semantics_preserved"]:
        fail("Perfetto preload violates the system-allocator boundary")
    if not document["symbolization"]["llvm_symbolizer"]["available"]:
        fail("llvm-symbolizer is required on PATH for Perfetto workload attribution")
    if not document["supported"]:
        fail("Perfetto workload tool capability is unsupported")


def profiler_command(
    *,
    heap_profile_script: Path,
    tracebox: Path,
    preload: Path,
    traceconv_compat: Path,
    output: Path,
    workload: list[str],
) -> list[str]:
    return [
        sys.executable,
        str(heap_profile_script),
        "host",
        "--interval",
        str(WORKLOAD_SAMPLING_INTERVAL_BYTES),
        "--shmem-size",
        str(perfetto_heap_probe.SHMEM_SIZE_BYTES),
        "--block-client",
        "--output",
        str(output),
        "--traceconv-binary",
        str(traceconv_compat),
        "--preload-library",
        str(preload),
        "--tracebox-binary",
        str(tracebox),
        "--",
        *workload,
    ]


def _tee_stream(stream: Any, sink: Any, console: Any) -> None:
    try:
        for line in iter(stream.readline, ""):
            sink.write(line)
            sink.flush()
            console.write(line)
            console.flush()
    finally:
        stream.close()


def run_profiler_observable(
    *,
    command: list[str],
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    terminate_grace_seconds: float = PROFILER_TERMINATE_GRACE_SECONDS,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("profiler timeout must be positive")
    if terminate_grace_seconds <= 0:
        raise ValueError("profiler terminate grace must be positive")

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    with stdout_path.open("w", encoding="utf-8", buffering=1) as stdout_file, \
         stderr_path.open("w", encoding="utf-8", buffering=1) as stderr_file:
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=1,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            fail("Perfetto profiler subprocess pipes were not created")

        stdout_thread = threading.Thread(
            target=_tee_stream,
            args=(process.stdout, stdout_file, sys.stdout),
            daemon=True,
            name="perfetto-stdout-tee",
        )
        stderr_thread = threading.Thread(
            target=_tee_stream,
            args=(process.stderr, stderr_file, sys.stderr),
            daemon=True,
            name="perfetto-stderr-tee",
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        termination = "natural"
        try:
            exit_status = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            termination = "timeout-sigterm"
            print(
                f"Perfetto profiler exceeded {timeout_seconds:g}s; "
                f"terminating process group {process.pid}",
                file=sys.stderr,
                flush=True,
            )
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                exit_status = process.wait(timeout=terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                termination = "timeout-sigkill"
                print(
                    "Perfetto profiler process group ignored SIGTERM; "
                    "sending SIGKILL",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                exit_status = process.wait()

        stdout_thread.join(timeout=terminate_grace_seconds)
        stderr_thread.join(timeout=terminate_grace_seconds)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            fail("Perfetto profiler output tee threads did not terminate")

    return {
        "schema": "oxide-batch-perfetto-profiler-process/v1",
        "exit_status": exit_status,
        "timed_out": timed_out,
        "termination": termination,
        "timeout_seconds": timeout_seconds,
        "terminate_grace_seconds": terminate_grace_seconds,
        "process_group_isolated": True,
    }


def stack_summary_query() -> str:
    rust_symbol_predicate = rust_symbol_sql_predicate()
    return f"""
WITH RECURSIVE
  root_ids(root_id) AS (
    SELECT DISTINCT callsite_id
    FROM heap_profile_allocation
    WHERE size > 0 AND callsite_id IS NOT NULL
  ),
  stack(root_id, id, frame_id, parent_id, depth) AS (
    SELECT r.root_id, c.id, c.frame_id, c.parent_id, 0
    FROM root_ids r
    JOIN stack_profile_callsite c ON c.id = r.root_id
    UNION ALL
    SELECT s.root_id, c.id, c.frame_id, c.parent_id, s.depth + 1
    FROM stack s
    JOIN stack_profile_callsite c ON c.id = s.parent_id
    WHERE s.depth < 256
  ),
  frames AS (
    SELECT s.root_id, s.depth, f.name
    FROM stack s
    JOIN stack_profile_frame f ON f.id = s.frame_id
  ),
  per_root AS (
    SELECT
      root_id,
      COUNT(*) AS frame_rows,
      SUM(CASE WHEN name IS NOT NULL AND name <> '' THEN 1 ELSE 0 END)
        AS named_frame_rows,
      MAX(CASE WHEN {rust_symbol_predicate} THEN 1 ELSE 0 END)
        AS has_rust_symbol,
      MAX(CASE WHEN instr(lower(name), 'oxide_batch') > 0 THEN 1 ELSE 0 END)
        AS has_oxide_batch,
      MAX(CASE WHEN instr(lower(name), 'postgres_postgres') > 0 THEN 1 ELSE 0 END)
        AS has_workload
    FROM frames
    GROUP BY root_id
  )
SELECT
  COALESCE(SUM(frame_rows), 0) AS frame_rows,
  COALESCE(SUM(named_frame_rows), 0) AS named_frame_rows,
  COALESCE(SUM(has_rust_symbol), 0) AS rust_symbol_callsite_count,
  COALESCE(SUM(has_oxide_batch), 0) AS oxide_batch_callsite_count,
  COALESCE(SUM(has_workload), 0) AS workload_callsite_count,
  COALESCE(SUM(
    CASE
      WHEN has_rust_symbol = 1 AND has_oxide_batch = 1 AND has_workload = 1
      THEN 1 ELSE 0
    END
  ), 0) AS cross_boundary_callsite_count
FROM per_root;
""".strip()


def stack_symbols_query() -> str:
    return """
WITH RECURSIVE
  root_ids(root_id) AS (
    SELECT DISTINCT callsite_id
    FROM heap_profile_allocation
    WHERE size > 0 AND callsite_id IS NOT NULL
  ),
  stack(root_id, id, frame_id, parent_id, depth) AS (
    SELECT r.root_id, c.id, c.frame_id, c.parent_id, 0
    FROM root_ids r
    JOIN stack_profile_callsite c ON c.id = r.root_id
    UNION ALL
    SELECT s.root_id, c.id, c.frame_id, c.parent_id, s.depth + 1
    FROM stack s
    JOIN stack_profile_callsite c ON c.id = s.parent_id
    WHERE s.depth < 256
  )
SELECT
  s.root_id,
  s.depth,
  f.name
FROM stack s
JOIN stack_profile_frame f ON f.id = s.frame_id
WHERE f.name IS NOT NULL AND f.name <> ''
ORDER BY s.root_id, s.depth;
""".strip()


def run_multirow_query(
    *,
    trace_processor: Path,
    trace: Path,
    query: str,
    output: Path,
) -> dict[str, Any]:
    completed = subprocess.run(
        perfetto_heap_probe.trace_query_command(trace_processor, trace, query),
        check=False,
        text=True,
        capture_output=True,
    )
    output.write_text(completed.stdout, encoding="utf-8")
    rows: list[dict[str, str]] = []
    if completed.returncode == 0 and completed.stdout.strip():
        rows = [
            {str(key): str(value) for key, value in row.items()}
            for row in csv.DictReader(io.StringIO(completed.stdout))
        ]
    return {
        "exit_status": completed.returncode,
        "stderr": perfetto_heap_probe.bounded(completed.stderr),
        "row_count": len(rows),
        "rows": rows,
    }


def supported_workload_attribution(
    *,
    profiler_exit_status: int,
    raw_trace_bytes: int,
    symbol_packets_bytes: int,
    symbolized_trace_bytes: int,
    pprof_count: int,
    allocation_count: int,
    allocation_bytes: int,
    positive_callsite_count: int,
    frame_rows: int,
    named_frame_rows: int,
    rust_symbol_callsite_count: int,
    oxide_batch_callsite_count: int,
    workload_callsite_count: int,
    cross_boundary_callsite_count: int,
    heapprofd_error_count: int,
    symbol_projection_rows: int,
) -> bool:
    return (
        profiler_exit_status == 0
        and raw_trace_bytes > 0
        and symbol_packets_bytes > 0
        and symbolized_trace_bytes > 0
        and pprof_count > 0
        and allocation_count > 0
        and allocation_bytes > 0
        and positive_callsite_count > 0
        and frame_rows > 0
        and named_frame_rows > 0
        and rust_symbol_callsite_count > 0
        and oxide_batch_callsite_count > 0
        and workload_callsite_count > 0
        and cross_boundary_callsite_count > 0
        and heapprofd_error_count == 0
        and symbol_projection_rows > 0
    )


def build_summary(
    *,
    profiler_exit_status: int,
    raw_trace_bytes: int,
    symbol_packets_bytes: int,
    symbolized_trace_bytes: int,
    pprof_count: int,
    allocation_row: dict[str, str],
    stack_row: dict[str, str],
    loss_row: dict[str, str],
    symbol_projection_rows: int,
) -> dict[str, Any]:
    allocation_count = perfetto_heap_probe.int_field(
        allocation_row,
        "positive_allocation_count",
    )
    allocation_bytes = perfetto_heap_probe.int_field(
        allocation_row,
        "positive_allocation_bytes",
    )
    positive_callsite_count = perfetto_heap_probe.int_field(
        allocation_row,
        "positive_callsite_count",
    )
    frame_rows = perfetto_heap_probe.int_field(stack_row, "frame_rows")
    named_frame_rows = perfetto_heap_probe.int_field(
        stack_row,
        "named_frame_rows",
    )
    rust_symbol_callsite_count = perfetto_heap_probe.int_field(
        stack_row,
        "rust_symbol_callsite_count",
    )
    oxide_batch_callsite_count = perfetto_heap_probe.int_field(
        stack_row,
        "oxide_batch_callsite_count",
    )
    workload_callsite_count = perfetto_heap_probe.int_field(
        stack_row,
        "workload_callsite_count",
    )
    cross_boundary_callsite_count = perfetto_heap_probe.int_field(
        stack_row,
        "cross_boundary_callsite_count",
    )
    heapprofd_error_count = perfetto_heap_probe.int_field(
        loss_row,
        "heapprofd_error_count",
    )

    supported = supported_workload_attribution(
        profiler_exit_status=profiler_exit_status,
        raw_trace_bytes=raw_trace_bytes,
        symbol_packets_bytes=symbol_packets_bytes,
        symbolized_trace_bytes=symbolized_trace_bytes,
        pprof_count=pprof_count,
        allocation_count=allocation_count,
        allocation_bytes=allocation_bytes,
        positive_callsite_count=positive_callsite_count,
        frame_rows=frame_rows,
        named_frame_rows=named_frame_rows,
        rust_symbol_callsite_count=rust_symbol_callsite_count,
        oxide_batch_callsite_count=oxide_batch_callsite_count,
        workload_callsite_count=workload_callsite_count,
        cross_boundary_callsite_count=cross_boundary_callsite_count,
        heapprofd_error_count=heapprofd_error_count,
        symbol_projection_rows=symbol_projection_rows,
    )
    if not supported:
        fail("Perfetto workload allocation-stack attribution did not pass fail-closed gates")

    return {
        "schema": "oxide-batch-perfetto-heap-profile-summary/v3",
        "profiler_exit_status": profiler_exit_status,
        "raw_trace_bytes": raw_trace_bytes,
        "symbol_packets_bytes": symbol_packets_bytes,
        "symbolized_trace_bytes": symbolized_trace_bytes,
        "pprof_count": pprof_count,
        "positive_allocation_count": allocation_count,
        "positive_allocation_bytes": allocation_bytes,
        "positive_allocation_callsite_count": positive_callsite_count,
        "frame_rows": frame_rows,
        "named_frame_rows": named_frame_rows,
        "rust_symbol_callsite_count": rust_symbol_callsite_count,
        "oxide_batch_callsite_count": oxide_batch_callsite_count,
        "workload_callsite_count": workload_callsite_count,
        "cross_boundary_callsite_count": cross_boundary_callsite_count,
        "symbol_projection_rows": symbol_projection_rows,
        "heapprofd_error_count": heapprofd_error_count,
        "sampling": sampling_contract(),
        "rust_symbol_classification": rust_symbol_contract(),
        "allocation_stack_attribution": "symbolized-rust-workload-to-oxide-batch",
        "coverage_scope": "system-allocator-malloc-family-interposition",
        "timing_claim": "attribution-only",
        "supported": True,
    }


def limitations() -> list[str]:
    return [
        "Perfetto heapprofd runtime is allocation-attribution evidence only and must not be compared with benchmark-lane timing",
        "the real workload uses Perfetto's 4096-byte default Poisson sampling interval for practical stack attribution; interval 1 remains capability-probe-only and is not used for the workload",
        "generic Perfetto coverage in this producer is intentionally limited to the system allocator because replacement Rust global allocators can bypass glibc malloc-family interposition",
        "the profiled release build sets CARGO_PROFILE_RELEASE_DEBUG=1 for symbolization and is not the benchmark-lane binary",
        "the raw Perfetto trace is authoritative; pprof and SQL outputs are derived projections",
        "Perfetto heap_profile host mode does not propagate the child workload exit code; the independent postgres-postgres verifier digest is the authoritative workload-completion/correctness gate",
        "workload attribution is accepted only when one allocation-reachable stack contains a symbolized Rust frame (demangled :: form or Rust v0 _R form) plus postgres_postgres and oxide_batch crate identity markers",
        "positive allocation counts and bytes are sampled trace-attribution counters, not exact allocation-event totals, and are not generalized into stable-host absolute memory-performance claims",
        "hosted-runner evidence validates orchestration, correctness, and stack readability rather than stable-machine memory-performance truth",
        "OxideBatch 0.7.0 evidence must not be generalized to later releases or a permanent allocator decision",
    ]


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
    symbolizer_version: str,
) -> dict[str, Any]:
    manifest = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": "heap",
        "run": {"id": run_id, "repetition": 1},
        "subject": {
            "workload_sha": subject_sha,
            "cargo_lock_sha256": cargo_lock_sha256,
            "rustc": rustc_version,
            "cargo": cargo_version,
            "target": target,
            "build_profile": "release",
            "build_flags": ["CARGO_PROFILE_RELEASE_DEBUG=1"],
            "oxide_batch": {
                "name": oxide_subject["name"],
                "version": oxide_subject["version"],
                "source": oxide_subject["source"],
                "checksum": oxide_subject["checksum"],
            },
        },
        "allocator": profile_smoke.allocator_record(SYSTEM_ALLOCATOR),
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
                    "name": "perfetto-heapprofd-host",
                    "version": perfetto_heap_probe.PERFETTO_RELEASE,
                    "role": "heap/allocation tracer with allocation-stack attribution",
                },
                {
                    "name": "trace_processor_shell",
                    "version": perfetto_heap_probe.PREBUILT_ROLL,
                    "role": "Perfetto trace projection and SQL attribution",
                },
                {
                    "name": "llvm-symbolizer",
                    "version": symbolizer_version,
                    "role": "native Rust/C symbolization",
                },
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
    heap_profile.validate_bounds(args)
    heap_profile.validate_allocator_scope(args.allocator)
    profile_smoke.prepare_output_root(args.output)

    base_env = os.environ.copy()
    if base_env.get("LD_PRELOAD"):
        fail("refusing Perfetto heap attribution with a pre-existing LD_PRELOAD")
    base_env["RUST_LOG"] = "error"

    heap_root = args.output / "heap"
    correctness_root = args.output / "correctness"
    heap_root.mkdir(parents=True, exist_ok=True)
    correctness_root.mkdir(parents=True, exist_ok=True)

    capability = tool_capability(args)
    capability_path = heap_root / "capability.json"
    profile_smoke.write_json(capability_path, capability)
    require_tool_capability(capability)

    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    binary = heap_profile.build_variant(
        target=target,
        output_root=args.output,
        base_env=base_env,
    )

    subject_sha = profile_smoke.workload_sha()
    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = profile_smoke.sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.7.0")
    profile_smoke.validate_allocator_lock(lock_path, target)

    token = profile_smoke.run_token()
    template_database = profile_smoke.database_name(token, "perfetto_heap_template")
    sample_database = profile_smoke.database_name(token, "perfetto_heap_system")
    paired.compose_db("dropdb", template_database)
    paired.compose_db("createdb", template_database, template="template0")
    template_url = paired.database_url(args.base_database_url, template_database)

    try:
        profile_smoke.run_checked(
            [str(binary), "migrate", "--database-url", template_url],
            env=base_env,
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
            env=base_env,
        )

        paired.compose_db("dropdb", sample_database)
        paired.compose_db("createdb", sample_database, template=template_database)
        database_url = paired.database_url(args.base_database_url, sample_database)
        import_value = perfetto_import_name(args.reader)

        workload = heap_profile.workload_command(
            binary=binary,
            import_value=import_value,
            reader=args.reader,
            chunk_size=args.chunk_size,
            fetch_size=args.fetch_size,
            page_size=args.page_size,
        )

        compat_root = heap_root / "compat"
        compat_root.mkdir()
        traceconv_compat, traceconv_evidence = (
            perfetto_heap_probe.write_traceconv_compat_shim(
                compat_root,
                args.trace_processor_binary.resolve(),
            )
        )

        profile_root = heap_root / "profile"
        profile_root.mkdir()
        command = profiler_command(
            heap_profile_script=args.heap_profile_script.resolve(),
            tracebox=args.tracebox_binary.resolve(),
            preload=args.preload_library.resolve(),
            traceconv_compat=traceconv_compat,
            output=profile_root,
            workload=workload,
        )
        profiled_env = base_env.copy()
        profiled_env["DATABASE_URL"] = database_url
        profiled_env["PERFETTO_BINARY_PATH"] = str(binary.parent)

        stdout_path = heap_root / "heap_profile.stdout.txt"
        stderr_path = heap_root / "heap_profile.stderr.txt"
        process_path = heap_root / "profiler-process.json"
        process_result = run_profiler_observable(
            command=command,
            env=profiled_env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=args.profiler_timeout_seconds,
        )
        profile_smoke.write_json(process_path, process_result)
        if process_result["timed_out"]:
            fail(
                "Perfetto heap_profile exceeded explicit profiler timeout "
                f"{args.profiler_timeout_seconds}s"
            )
        if process_result["exit_status"] != 0:
            stderr_tail = stderr_path.read_text(
                encoding="utf-8",
                errors="replace",
            )[-2000:]
            fail(
                f"Perfetto heap_profile exited {process_result['exit_status']}: "
                f"{stderr_tail}"
            )

        raw_trace = profile_root / "raw-trace"
        symbols = profile_root / "symbols"
        symbolized_trace = profile_root / "symbolized-trace"
        for path, label in (
            (raw_trace, "raw Perfetto trace"),
            (symbols, "Perfetto symbol packets"),
            (symbolized_trace, "symbolized Perfetto trace"),
        ):
            if not path.is_file() or path.stat().st_size <= 0:
                fail(f"{label} is missing or empty: {path}")

        pprof_files = sorted(
            path
            for path in profile_root.glob("heap_dump.*.pb.gz")
            if path.is_file() and path.stat().st_size > 0
        )
        if not pprof_files:
            fail("Perfetto workload profiling produced no non-empty pprof projection")

        allocation_path = heap_root / "allocation-stats.csv"
        allocation_result = perfetto_heap_probe.run_trace_query(
            trace_processor=args.trace_processor_binary.resolve(),
            trace=symbolized_trace,
            query=perfetto_heap_probe.allocation_stats_query(),
            output=allocation_path,
        )
        stack_path = heap_root / "stack-summary.csv"
        stack_result = perfetto_heap_probe.run_trace_query(
            trace_processor=args.trace_processor_binary.resolve(),
            trace=symbolized_trace,
            query=stack_summary_query(),
            output=stack_path,
        )
        loss_path = heap_root / "heapprofd-errors.csv"
        loss_result = perfetto_heap_probe.run_trace_query(
            trace_processor=args.trace_processor_binary.resolve(),
            trace=symbolized_trace,
            query=perfetto_heap_probe.data_loss_query(),
            output=loss_path,
        )
        symbols_path = heap_root / "allocation-stack-symbols.csv"
        symbol_result = run_multirow_query(
            trace_processor=args.trace_processor_binary.resolve(),
            trace=symbolized_trace,
            query=stack_symbols_query(),
            output=symbols_path,
        )

        for name, result in (
            ("allocation stats", allocation_result),
            ("stack summary", stack_result),
            ("heapprofd error", loss_result),
            ("stack symbol projection", symbol_result),
        ):
            if result["exit_status"] != 0:
                fail(f"Perfetto {name} query failed: {result.get('stderr', '')}")

        if (
            allocation_result["row"] is None
            or stack_result["row"] is None
            or loss_result["row"] is None
        ):
            fail("Perfetto workload SQL projection returned no summary row")

        summary = build_summary(
            profiler_exit_status=process_result["exit_status"],
            raw_trace_bytes=raw_trace.stat().st_size,
            symbol_packets_bytes=symbols.stat().st_size,
            symbolized_trace_bytes=symbolized_trace.stat().st_size,
            pprof_count=len(pprof_files),
            allocation_row=allocation_result["row"],
            stack_row=stack_result["row"],
            loss_row=loss_result["row"],
            symbol_projection_rows=symbol_result["row_count"],
        )
        summary_path = heap_root / "summary.json"
        profile_smoke.write_json(summary_path, summary)

        verifier_report, verifier_stdout = profile_smoke.run_verifier(
            binary=binary,
            database_url=database_url,
            import_value=import_value,
            rows=args.rows,
            env=base_env,
        )
        verifier_path = correctness_root / "verifier.json"
        verifier_path.write_text(verifier_stdout, encoding="utf-8")

        run_metadata = {
            "schema": "oxide-batch-perfetto-heap-profile-run/v3",
            "profiler": {
                "name": "perfetto-heapprofd-host",
                "release": perfetto_heap_probe.PERFETTO_RELEASE,
                "commit": perfetto_heap_probe.PERFETTO_COMMIT,
                "prebuilt_roll": perfetto_heap_probe.PREBUILT_ROLL,
                "sampling": sampling_contract(),
                "trace_processor_sha256": capability["provenance"][
                    "trace_processor"
                ]["sha256"],
                "preload_sha256": capability["provenance"]["preload"]["sha256"],
                "traceconv_compat": traceconv_evidence,
                "llvm_symbolizer": capability["symbolization"]["llvm_symbolizer"],
            },
            "allocator": SYSTEM_ALLOCATOR,
            "reader_mode": args.reader,
            "rows": args.rows,
            "seed": args.seed,
            "build_flags": ["CARGO_PROFILE_RELEASE_DEBUG=1"],
            "profiled_binary_sha256": profile_smoke.sha256_file(binary),
            "profiler_exit_status": process_result["exit_status"],
            "profiler_process": process_result,
            "rust_symbol_classification": rust_symbol_contract(),
            "timing_claim": "attribution-only",
            "coverage_scope": capability["allocator_boundary"]["coverage_scope"],
        }
        metadata_path = heap_root / "run.json"
        profile_smoke.write_json(metadata_path, run_metadata)

        artifacts: list[dict[str, str]] = [
            profile_smoke.artifact_record(args.output, raw_trace),
            {
                **profile_smoke.artifact_record(args.output, symbols),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, symbolized_trace),
                "kind": "projection",
            },
            profile_smoke.artifact_record(args.output, stdout_path),
            profile_smoke.artifact_record(args.output, stderr_path),
            profile_smoke.artifact_record(args.output, process_path),
            profile_smoke.artifact_record(args.output, capability_path),
            profile_smoke.artifact_record(args.output, metadata_path),
            {
                **profile_smoke.artifact_record(args.output, summary_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, allocation_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, stack_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, loss_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, symbols_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, traceconv_compat),
                "kind": "instrumentation",
            },
            profile_smoke.artifact_record(args.output, verifier_path),
            profile_smoke.artifact_record(args.output, binary),
        ]
        artifacts.extend(
            {
                **profile_smoke.artifact_record(args.output, path),
                "kind": "projection",
            }
            for path in pprof_files
        )

        manifest = build_manifest(
            run_id=f"perfetto-heap-profile-{token}-system-{args.reader}",
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
            symbolizer_version=capability["symbolization"]["llvm_symbolizer"][
                "version"
            ],
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
    parser.add_argument("--heap-profile-script", type=Path, required=True)
    parser.add_argument("--tracebox-binary", type=Path, required=True)
    parser.add_argument("--preload-library", type=Path, required=True)
    parser.add_argument("--trace-processor-binary", type=Path, required=True)
    parser.add_argument(
        "--profiler-timeout-seconds",
        type=float,
        default=DEFAULT_PROFILER_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--allocator",
        choices=(SYSTEM_ALLOCATOR,),
        default=SYSTEM_ALLOCATOR,
    )
    parser.add_argument("--reader", choices=("cursor", "paging"), default="paging")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--fetch-size", type=int, default=DEFAULT_FETCH_SIZE)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    args = parser.parse_args()
    if args.profiler_timeout_seconds <= 0:
        parser.error("--profiler-timeout-seconds must be positive")

    manifest = run_profile(args)
    print(
        json.dumps(
            {
                "valid": True,
                "lane": manifest["lane"],
                "allocator": manifest["allocator"]["identity"],
                "reader": manifest["workload"]["reader_mode"],
                "profiler": manifest["instrumentation"]["tools"][0]["name"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
