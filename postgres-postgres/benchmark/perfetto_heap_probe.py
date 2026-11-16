#!/usr/bin/env python3
"""Probe Perfetto heapprofd host-mode allocation-stack attribution for campaign #266."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PERFETTO_RELEASE = "v59.1"
PERFETTO_COMMIT = "1e9c61e9844affd7dccd3d42d9ea9f3a3d10cf6d"
HEAP_PROFILE_BLOB_SHA1 = "1d5b1cc8d9bd38749c6d200a8d5aac9b616b84ed"
HEAP_PROFILE_URL = (
    "https://raw.githubusercontent.com/google/perfetto/"
    f"{PERFETTO_COMMIT}/tools/heap_profile"
)
PREBUILT_ROLL = "v58.2"
PROBE_FUNCTION = "oxide_batch_perfetto_probe_allocate"
PROBE_ALLOCATIONS = 32
SAMPLING_INTERVAL_BYTES = 1
SHMEM_SIZE_BYTES = 8 * 1024 * 1024
KNOWN_REPLACEMENT_ALLOCATOR_TOKENS = (
    "jemalloc",
    "mimalloc",
    "snmalloc",
    "tcmalloc",
    "rpmalloc",
)

ARM64_PREBUILTS = {
    "tracebox": {
        "file_name": "tracebox",
        "size": 3_470_168,
        "sha256": "2d21883e9c55818f851fd8c391afb9b366032b3110076b4fd41f31f30b5849fa",
        "url": (
            "https://commondatastorage.googleapis.com/perfetto-luci-artifacts/"
            "v58.2/linux-arm64/tracebox"
        ),
    },
    "preload": {
        "file_name": "libheapprofd_glibc_preload.so",
        "size": 1_034_480,
        "sha256": "4ca10d32455973389d0f772f1c9ad8616b61eb46edc3b995a2825dee9fab2662",
        "url": (
            "https://commondatastorage.googleapis.com/perfetto-luci-artifacts/"
            "v58.2/linux-arm64/libheapprofd_glibc_preload.so"
        ),
    },
    "trace_processor": {
        "file_name": "trace_processor_shell",
        "size": 14_086_160,
        "sha256": "0e6e0c5452c505c8d46fe472fd196a0d17d963460727e2ce2013b02aa1309555",
        "url": (
            "https://commondatastorage.googleapis.com/perfetto-luci-artifacts/"
            "v58.2/linux-arm64/trace_processor_shell"
        ),
    },
}

PROBE_SOURCE = rf"""
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

volatile uint64_t oxide_batch_perfetto_probe_checksum = 0;

__attribute__((noinline))
void {PROBE_FUNCTION}(void) {{
    for (size_t i = 0; i < {PROBE_ALLOCATIONS}; ++i) {{
        size_t size = 4096 + i * 17;
        unsigned char *ptr = (unsigned char *)malloc(size);
        if (ptr == NULL) {{
            abort();
        }}
        memset(ptr, (int)(i + 1), size);
        oxide_batch_perfetto_probe_checksum += ptr[i];
        free(ptr);
    }}
}}

int main(void) {{
    {PROBE_FUNCTION}();
    return oxide_batch_perfetto_probe_checksum == UINT64_MAX ? 1 : 0;
}}
"""


def fail(message: str) -> None:
    raise RuntimeError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def bounded(text: str, limit: int = 8000) -> str:
    return text[-limit:]


def host_document() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "supported_host": (
            platform.system() == "Linux" and platform.machine().lower() == "aarch64"
        ),
    }


def script_record(path: Path) -> dict[str, Any]:
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    actual_blob = git_blob_sha1(path) if exists else None
    return {
        "path": str(path),
        "exists": exists,
        "size": size,
        "git_blob_sha1": actual_blob,
        "expected_git_blob_sha1": HEAP_PROFILE_BLOB_SHA1,
        "matches_expected": exists and actual_blob == HEAP_PROFILE_BLOB_SHA1,
        "source_url": HEAP_PROFILE_URL,
    }


def prebuilt_record(path: Path, identity: str) -> dict[str, Any]:
    expected = ARM64_PREBUILTS[identity]
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    actual_sha256 = sha256_file(path) if exists else None
    executable_required = identity in {"tracebox", "trace_processor"}
    executable = exists and os.access(path, os.X_OK)
    return {
        "identity": identity,
        "path": str(path),
        "exists": exists,
        "size": size,
        "expected_size": expected["size"],
        "sha256": actual_sha256,
        "expected_sha256": expected["sha256"],
        "executable_required": executable_required,
        "executable": executable,
        "matches_expected": (
            exists
            and size == expected["size"]
            and actual_sha256 == expected["sha256"]
            and (not executable_required or executable)
        ),
        "source_url": expected["url"],
        "prebuilt_roll": PREBUILT_ROLL,
    }


def dynamic_dependencies(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "exit_status": None,
            "stdout": "",
            "stderr": "preload library is missing",
            "lines": [],
        }
    try:
        completed = subprocess.run(
            ["ldd", str(path)],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        return {
            "exit_status": None,
            "stdout": "",
            "stderr": str(exc),
            "lines": [],
        }
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return {
        "exit_status": completed.returncode,
        "stdout": bounded(completed.stdout),
        "stderr": bounded(completed.stderr),
        "lines": lines,
    }


def llvm_symbolizer_record() -> dict[str, Any]:
    path = shutil.which("llvm-symbolizer")
    if path is None:
        return {
            "available": False,
            "path": None,
            "version": None,
            "version_exit_status": None,
        }
    completed = subprocess.run(
        [path, "--version"],
        check=False,
        text=True,
        capture_output=True,
    )
    version_text = (completed.stdout or completed.stderr).strip()
    return {
        "available": completed.returncode == 0,
        "path": path,
        "version": version_text.splitlines()[0] if version_text else None,
        "version_exit_status": completed.returncode,
    }


def replacement_allocator_dependencies(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if any(token in line.lower() for token in KNOWN_REPLACEMENT_ALLOCATOR_TOKENS)
    ]


def compile_probe(root: Path) -> tuple[Path | None, dict[str, Any]]:
    compiler = shutil.which("cc") or shutil.which("gcc")
    source = root / "perfetto-heap-probe.c"
    binary = root / "perfetto-heap-probe"
    source.write_text(PROBE_SOURCE, encoding="utf-8")

    if compiler is None:
        return None, {
            "compiler": None,
            "compiler_version": None,
            "compile_exit_status": None,
            "compile_stdout": "",
            "compile_stderr": "no cc/gcc compiler found",
            "binary_bytes": 0,
            "required_symbol": PROBE_FUNCTION,
        }

    version = subprocess.run(
        [compiler, "--version"],
        check=False,
        text=True,
        capture_output=True,
    )
    completed = subprocess.run(
        [
            compiler,
            "-g",
            "-O0",
            "-fno-omit-frame-pointer",
            "-fno-optimize-sibling-calls",
            "-rdynamic",
            "-Wl,--build-id",
            str(source),
            "-o",
            str(binary),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    binary_bytes = binary.stat().st_size if binary.is_file() else 0
    evidence = {
        "compiler": compiler,
        "compiler_version": (
            (version.stdout or version.stderr).splitlines()[0]
            if (version.stdout or version.stderr).strip()
            else None
        ),
        "compile_exit_status": completed.returncode,
        "compile_stdout": bounded(completed.stdout),
        "compile_stderr": bounded(completed.stderr),
        "binary_bytes": binary_bytes,
        "required_symbol": PROBE_FUNCTION,
    }
    if completed.returncode != 0 or binary_bytes <= 0:
        return None, evidence
    return binary.resolve(), evidence


def write_traceconv_compat_shim(root: Path, trace_processor: Path) -> tuple[Path, dict[str, Any]]:
    shim = root / "traceconv-compat.py"
    script = f'''#!/usr/bin/env python3
import pathlib
import subprocess
import sys
import tempfile

TRACE_PROCESSOR = {str(trace_processor)!r}

def main() -> int:
    args = list(sys.argv[1:])
    if not args:
        print("traceconv compatibility shim requires a mode", file=sys.stderr)
        return 2
    mode = args.pop(0)
    if mode == "symbolize":
        if len(args) != 1:
            print("symbolize requires exactly one trace path", file=sys.stderr)
            return 2
        completed = subprocess.run(
            [TRACE_PROCESSOR, "util", "symbolize", args[0]],
            check=False,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
        )
        return completed.returncode
    if mode == "profile":
        no_annotations = False
        if args and args[0] == "--no-annotations":
            no_annotations = True
            args.pop(0)
        if len(args) != 1:
            print("profile requires exactly one trace path", file=sys.stderr)
            return 2
        output_dir = pathlib.Path(tempfile.mkdtemp(prefix="heap_profile-"))
        command = [TRACE_PROCESSOR, "convert", "profile"]
        if no_annotations:
            command.append("--no-annotations")
        command += ["--output-dir", str(output_dir), args[0]]
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            sys.stderr.write(completed.stdout)
            sys.stderr.write(completed.stderr)
            return completed.returncode
        print(output_dir)
        return 0
    print(f"unsupported legacy traceconv mode: {{mode}}", file=sys.stderr)
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
'''
    shim.write_text(script, encoding="utf-8")
    shim.chmod(0o755)
    return shim, {
        "path": str(shim),
        "sha256": sha256_file(shim),
        "bytes": shim.stat().st_size,
        "trace_processor": str(trace_processor),
        "legacy_modes": ["symbolize", "profile"],
        "translated_commands": [
            "util symbolize",
            "convert profile --output-dir",
        ],
    }


def profiler_command(
    *,
    heap_profile_script: Path,
    tracebox: Path,
    preload: Path,
    traceconv_compat: Path,
    output: Path,
    debuggee: Path,
) -> list[str]:
    return [
        sys.executable,
        str(heap_profile_script),
        "host",
        "--interval",
        str(SAMPLING_INTERVAL_BYTES),
        "--shmem-size",
        str(SHMEM_SIZE_BYTES),
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
        str(debuggee),
    ]


def trace_query_command(
    trace_processor: Path,
    trace: Path,
    query: str,
) -> list[str]:
    return [
        str(trace_processor),
        "query",
        str(trace),
        query,
    ]


def parse_single_csv_row(text: str) -> dict[str, str]:
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if len(rows) != 1:
        fail(f"expected exactly one CSV data row, got {len(rows)}")
    return {str(key): str(value) for key, value in rows[0].items()}


def int_field(row: dict[str, str], field: str) -> int:
    value = row.get(field)
    if value is None or value == "":
        fail(f"trace query did not return integer field {field!r}")
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(
            f"trace query field {field!r} was not an integer: {value!r}"
        ) from exc


def allocation_stats_query() -> str:
    return """
SELECT
  COALESCE(SUM(CASE WHEN count > 0 THEN count ELSE 0 END), 0)
    AS positive_allocation_count,
  COALESCE(SUM(CASE WHEN size > 0 THEN size ELSE 0 END), 0)
    AS positive_allocation_bytes,
  COUNT(DISTINCT CASE WHEN size > 0 THEN callsite_id END)
    AS positive_callsite_count
FROM heap_profile_allocation;
""".strip()


def stack_readability_query() -> str:
    return f"""
WITH RECURSIVE
  roots(id) AS (
    SELECT DISTINCT callsite_id
    FROM heap_profile_allocation
    WHERE size > 0 AND callsite_id IS NOT NULL
  ),
  stack(id, frame_id, parent_id) AS (
    SELECT c.id, c.frame_id, c.parent_id
    FROM stack_profile_callsite c
    JOIN roots r ON r.id = c.id
    UNION ALL
    SELECT c.id, c.frame_id, c.parent_id
    FROM stack_profile_callsite c
    JOIN stack s ON c.id = s.parent_id
  )
SELECT
  COUNT(*) AS frame_rows,
  COALESCE(SUM(
    CASE
      WHEN f.name IS NOT NULL AND f.name <> '' THEN 1
      ELSE 0
    END
  ), 0) AS named_frame_rows,
  COALESCE(SUM(
    CASE WHEN f.name = '{PROBE_FUNCTION}' THEN 1 ELSE 0 END
  ), 0) AS known_symbol_rows
FROM stack s
JOIN stack_profile_frame f ON f.id = s.frame_id;
""".strip()


def data_loss_query() -> str:
    return """
SELECT
  COALESCE(SUM(
    CASE
      WHEN name IN (
        'heapprofd_buffer_corrupted',
        'heapprofd_hit_guardrail',
        'heapprofd_buffer_overran',
        'heapprofd_client_error',
        'heapprofd_malformed_packet',
        'heapprofd_missing_packet',
        'heapprofd_non_finalized_profile',
        'heapprofd_sampling_interval_adjusted'
      )
      THEN value
      ELSE 0
    END
  ), 0) AS heapprofd_error_count
FROM stats;
""".strip()


def run_trace_query(
    *,
    trace_processor: Path,
    trace: Path,
    query: str,
    output: Path,
) -> dict[str, Any]:
    completed = subprocess.run(
        trace_query_command(trace_processor, trace, query),
        check=False,
        text=True,
        capture_output=True,
    )
    output.write_text(completed.stdout, encoding="utf-8")
    return {
        "exit_status": completed.returncode,
        "stdout": bounded(completed.stdout),
        "stderr": bounded(completed.stderr),
        "row": (
            parse_single_csv_row(completed.stdout)
            if completed.returncode == 0 and completed.stdout.strip()
            else None
        ),
    }


def evidence_record(root: Path, path: Path, kind: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "kind": kind,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def supported_capability(
    *,
    host_ok: bool,
    provenance_ok: bool,
    dependency_ok: bool,
    compile_ok: bool,
    symbolizer_ok: bool,
    profiler_exit_status: int | None,
    raw_trace_bytes: int,
    pprof_count: int,
    allocation_count: int,
    allocation_bytes: int,
    named_frame_rows: int,
    known_symbol_rows: int,
    heapprofd_error_count: int,
) -> bool:
    return (
        host_ok
        and provenance_ok
        and dependency_ok
        and compile_ok
        and symbolizer_ok
        and profiler_exit_status == 0
        and raw_trace_bytes > 0
        and pprof_count > 0
        and allocation_count > 0
        and allocation_bytes > 0
        and named_frame_rows > 0
        and known_symbol_rows > 0
        and heapprofd_error_count == 0
    )


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    root = args.output.resolve()
    if root.exists():
        if any(root.iterdir()):
            fail(f"output directory must be empty: {root}")
    else:
        root.mkdir(parents=True)

    host = host_document()
    script = script_record(args.heap_profile_script.resolve())
    tracebox = prebuilt_record(args.tracebox_binary.resolve(), "tracebox")
    preload = prebuilt_record(args.preload_library.resolve(), "preload")
    trace_processor = prebuilt_record(
        args.trace_processor_binary.resolve(),
        "trace_processor",
    )
    dependencies = dynamic_dependencies(args.preload_library.resolve())
    symbolizer = llvm_symbolizer_record()
    replacement_dependencies = replacement_allocator_dependencies(
        dependencies["lines"]
    )

    provenance_ok = all(
        record["matches_expected"]
        for record in (script, tracebox, preload, trace_processor)
    )
    dependency_ok = (
        dependencies["exit_status"] == 0
        and bool(dependencies["lines"])
        and not replacement_dependencies
    )

    probe_root = root / "probe"
    probe_root.mkdir()
    debuggee, compile_evidence = compile_probe(probe_root)
    traceconv_compat, traceconv_compat_evidence = write_traceconv_compat_shim(
        probe_root,
        args.trace_processor_binary.resolve(),
    )
    compile_ok = (
        debuggee is not None
        and compile_evidence["compile_exit_status"] == 0
        and compile_evidence["binary_bytes"] > 0
    )

    profile_root = root / "profile"
    profile_root.mkdir()
    profiler_stdout = root / "heap_profile.stdout.txt"
    profiler_stderr = root / "heap_profile.stderr.txt"

    profiler_status = None
    profiler_command_value = None
    probe_error = None
    if (
        host["supported_host"]
        and provenance_ok
        and dependency_ok
        and compile_ok
        and symbolizer["available"]
        and debuggee is not None
    ):
        if os.environ.get("LD_PRELOAD"):
            probe_error = "refusing Perfetto probe with pre-existing LD_PRELOAD"
        else:
            profiler_command_value = profiler_command(
                heap_profile_script=args.heap_profile_script.resolve(),
                tracebox=args.tracebox_binary.resolve(),
                preload=args.preload_library.resolve(),
                traceconv_compat=traceconv_compat,
                output=profile_root,
                debuggee=debuggee,
            )
            profiler_env = os.environ.copy()
            profiler_env["PERFETTO_BINARY_PATH"] = str(probe_root)
            completed = subprocess.run(
                profiler_command_value,
                check=False,
                text=True,
                capture_output=True,
                env=profiler_env,
            )
            profiler_status = completed.returncode
            profiler_stdout.write_text(completed.stdout, encoding="utf-8")
            profiler_stderr.write_text(completed.stderr, encoding="utf-8")
    else:
        profiler_stdout.write_text("", encoding="utf-8")
        profiler_stderr.write_text("", encoding="utf-8")

    raw_trace = profile_root / "raw-trace"
    raw_trace_bytes = raw_trace.stat().st_size if raw_trace.is_file() else 0
    symbols = profile_root / "symbols"
    symbolized_trace = profile_root / "symbolized-trace"
    symbolized_trace_bytes = (
        symbolized_trace.stat().st_size if symbolized_trace.is_file() else 0
    )
    query_trace = symbolized_trace if symbolized_trace_bytes > 0 else raw_trace
    pprof_files = sorted(
        path
        for path in profile_root.glob("heap_dump.*.pb.gz")
        if path.is_file() and path.stat().st_size > 0
    )

    allocation_result: dict[str, Any] | None = None
    stack_result: dict[str, Any] | None = None
    loss_result: dict[str, Any] | None = None
    allocation_count = 0
    allocation_bytes = 0
    positive_callsite_count = 0
    frame_rows = 0
    named_frame_rows = 0
    known_symbol_rows = 0
    heapprofd_error_count = 0

    if profiler_status == 0 and raw_trace_bytes > 0:
        try:
            allocation_result = run_trace_query(
                trace_processor=args.trace_processor_binary.resolve(),
                trace=query_trace,
                query=allocation_stats_query(),
                output=root / "allocation-stats.csv",
            )
            stack_result = run_trace_query(
                trace_processor=args.trace_processor_binary.resolve(),
                trace=query_trace,
                query=stack_readability_query(),
                output=root / "stack-readability.csv",
            )
            loss_result = run_trace_query(
                trace_processor=args.trace_processor_binary.resolve(),
                trace=query_trace,
                query=data_loss_query(),
                output=root / "heapprofd-errors.csv",
            )
            if (
                allocation_result["exit_status"] == 0
                and allocation_result["row"] is not None
            ):
                allocation_count = int_field(
                    allocation_result["row"],
                    "positive_allocation_count",
                )
                allocation_bytes = int_field(
                    allocation_result["row"],
                    "positive_allocation_bytes",
                )
                positive_callsite_count = int_field(
                    allocation_result["row"],
                    "positive_callsite_count",
                )
            if stack_result["exit_status"] == 0 and stack_result["row"] is not None:
                frame_rows = int_field(stack_result["row"], "frame_rows")
                named_frame_rows = int_field(
                    stack_result["row"],
                    "named_frame_rows",
                )
                known_symbol_rows = int_field(
                    stack_result["row"],
                    "known_symbol_rows",
                )
            if loss_result["exit_status"] == 0 and loss_result["row"] is not None:
                heapprofd_error_count = int_field(
                    loss_result["row"],
                    "heapprofd_error_count",
                )
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            probe_error = str(exc)

    query_ok = (
        allocation_result is not None
        and allocation_result["exit_status"] == 0
        and stack_result is not None
        and stack_result["exit_status"] == 0
        and loss_result is not None
        and loss_result["exit_status"] == 0
    )
    supported = query_ok and supported_capability(
        host_ok=host["supported_host"],
        provenance_ok=provenance_ok,
        dependency_ok=dependency_ok,
        compile_ok=compile_ok,
        symbolizer_ok=symbolizer["available"],
        profiler_exit_status=profiler_status,
        raw_trace_bytes=raw_trace_bytes,
        pprof_count=len(pprof_files),
        allocation_count=allocation_count,
        allocation_bytes=allocation_bytes,
        named_frame_rows=named_frame_rows,
        known_symbol_rows=known_symbol_rows,
        heapprofd_error_count=heapprofd_error_count,
    )

    artifacts = []
    for path, kind in (
        (profiler_stdout, "log"),
        (profiler_stderr, "log"),
        (probe_root / "perfetto-heap-probe.c", "probe-source"),
        (traceconv_compat, "compat-shim"),
    ):
        if path.is_file():
            artifacts.append(evidence_record(root, path, kind))
    if debuggee is not None and debuggee.is_file():
        artifacts.append(evidence_record(root, debuggee, "probe-binary"))
    if raw_trace.is_file():
        artifacts.append(evidence_record(root, raw_trace, "raw"))
    for path, kind in (
        (symbols, "symbolization"),
        (symbolized_trace, "projection"),
    ):
        if path.is_file():
            artifacts.append(evidence_record(root, path, kind))
    for path in pprof_files:
        artifacts.append(evidence_record(root, path, "projection"))
    for path in (
        root / "allocation-stats.csv",
        root / "stack-readability.csv",
        root / "heapprofd-errors.csv",
    ):
        if path.is_file():
            artifacts.append(evidence_record(root, path, "projection"))

    document = {
        "schema": "oxide-batch-perfetto-heap-capability/v1",
        "campaign": 266,
        "profiler": "perfetto-heapprofd-host",
        "perfetto": {
            "release": PERFETTO_RELEASE,
            "commit": PERFETTO_COMMIT,
            "heap_profile_git_blob_sha1": HEAP_PROFILE_BLOB_SHA1,
            "prebuilt_roll": PREBUILT_ROLL,
            "sampling_interval_bytes": SAMPLING_INTERVAL_BYTES,
            "sampling_claim": "fully-accurate-high-overhead",
        },
        "host": host,
        "provenance": {
            "heap_profile_script": script,
            "tracebox": tracebox,
            "preload": preload,
            "trace_processor": trace_processor,
            "matches_expected": provenance_ok,
        },
        "symbolization": {
            "llvm_symbolizer": symbolizer,
            "required": True,
        },
        "allocator_boundary": {
            "identity": "system",
            "interceptor": "glibc LD_PRELOAD",
            "upstream_contract": (
                "heapprofd_glibc_preload uses glibc __libc_malloc-family "
                "symbols to keep the system allocator serving allocations"
            ),
            "dynamic_dependencies": dependencies,
            "replacement_allocator_dependencies": replacement_dependencies,
            "allocator_semantics_preserved": dependency_ok,
        },
        "probe": {
            **compile_evidence,
            "profiler_command": profiler_command_value,
            "traceconv_compat": traceconv_compat_evidence,
            "profiler_exit_status": profiler_status,
            "profiler_stdout_path": profiler_stdout.relative_to(root).as_posix(),
            "profiler_stderr_path": profiler_stderr.relative_to(root).as_posix(),
            "raw_trace_bytes": raw_trace_bytes,
            "symbolized_trace_bytes": symbolized_trace_bytes,
            "query_trace_path": query_trace.relative_to(root).as_posix(),
            "symbolization_binary_path": probe_root.as_posix(),
            "pprof_count": len(pprof_files),
            "pprof_files": [path.name for path in pprof_files],
            "allocation_count": allocation_count,
            "allocation_bytes": allocation_bytes,
            "positive_callsite_count": positive_callsite_count,
            "frame_rows": frame_rows,
            "named_frame_rows": named_frame_rows,
            "known_symbol_rows": known_symbol_rows,
            "known_probe_symbol_observed": known_symbol_rows > 0,
            "heapprofd_error_count": heapprofd_error_count,
            "allocation_query": allocation_result,
            "stack_query": stack_result,
            "data_loss_query": loss_result,
            "probe_error": probe_error,
        },
        "artifacts": artifacts,
        "supported": supported,
        "claim_boundary": [
            "capability only; no workload or benchmark result is produced",
            "sampling interval 1 is intentionally high-overhead and must not be used as benchmark timing",
            "support requires llvm-symbolizer on PATH and records its exact observed version",
            "support requires the known C allocation function to survive capture, remote unwind, trace import, and symbolization",
            "the retained traceconv compatibility shim only translates legacy heap_profile calls onto the pinned trace_processor_shell subcommands",
            "support requires zero recorded heapprofd corruption/overrun/guardrail/client/missing-packet errors",
            "replacement Rust global allocators remain outside this generic system-allocator probe",
            "Perfetto host-mode capability does not by itself establish OxideBatch workload stack readability",
        ],
    }
    write_json(root / "capability.json", document)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--heap-profile-script", type=Path, required=True)
    parser.add_argument("--tracebox-binary", type=Path, required=True)
    parser.add_argument("--preload-library", type=Path, required=True)
    parser.add_argument("--trace-processor-binary", type=Path, required=True)
    args = parser.parse_args()

    document = run_probe(args)
    print(
        json.dumps(
            {
                "supported": document["supported"],
                "profiler": document["profiler"],
                "release": document["perfetto"]["release"],
                "prebuilt_roll": document["perfetto"]["prebuilt_roll"],
                "known_probe_symbol_observed": document["probe"][
                    "known_probe_symbol_observed"
                ],
            },
            sort_keys=True,
        )
    )
    if not document["supported"]:
        fail("Perfetto heapprofd host capability probe failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
