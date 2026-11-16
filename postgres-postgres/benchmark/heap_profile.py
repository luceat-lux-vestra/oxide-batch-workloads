#!/usr/bin/env python3
"""Run one campaign #266 heap/allocation attribution profile with heaptrack."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import allocator_registry
import cpu_profile
import paired
import profile_manifest
import profile_smoke

ALLOCATOR_REGISTRY = allocator_registry.load_registry()
PROFILER_PACKAGES = ("heaptrack", "libheaptrack")
MAX_ROWS = 100_000
MAX_SIZE = 100_000
DEFAULT_ROWS = 25_000
DEFAULT_SEED = 266
DEFAULT_CHUNK_SIZE = 1_000
DEFAULT_FETCH_SIZE = 500
DEFAULT_PAGE_SIZE = 750
SYSTEM_ALLOCATOR = "system"
KNOWN_REPLACEMENT_ALLOCATOR_TOKENS = (
    "jemalloc",
    "mimalloc",
    "snmalloc",
    "tcmalloc",
    "rpmalloc",
)
ALLOCATION_COUNT = re.compile(r"^calls to allocation functions:\s*(\d+)\b", re.MULTILINE)
TEMPORARY_COUNT = re.compile(r"^temporary memory allocations:\s*(\d+)\b", re.MULTILINE)
MASSIF_HEAP = re.compile(r"^mem_heap_B=(\d+)\s*$", re.MULTILINE)


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


def validate_allocator_scope(allocator: str) -> None:
    if allocator != SYSTEM_ALLOCATOR:
        raise ValueError(
            "generic heaptrack attribution is intentionally limited to the system "
            "allocator because replacement Rust global allocators may bypass the "
            "malloc-family functions heaptrack interposes"
        )


def package_versions() -> dict[str, str]:
    completed = profile_smoke.run_checked(
        [
            "dpkg-query",
            "-W",
            "-f=${Package}=${Version}\n",
            *PROFILER_PACKAGES,
        ]
    )
    versions: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        package, version = line.split("=", 1)
        package = package.strip()
        version = version.strip()
        if package in PROFILER_PACKAGES and version:
            versions[package] = version
    missing = sorted(set(PROFILER_PACKAGES) - set(versions))
    if missing:
        fail(f"missing heaptrack package versions: {missing}")
    return versions


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        fail(f"{name} is unavailable; install the heaptrack package")
    return path


def find_preload_library() -> Path:
    completed = profile_smoke.run_checked(["dpkg-query", "-L", "libheaptrack"])
    candidates = [
        Path(line.strip()).resolve()
        for line in completed.stdout.splitlines()
        if line.strip().endswith("/libheaptrack_preload.so")
        and Path(line.strip()).is_file()
    ]
    if len(candidates) != 1:
        fail(
            "expected exactly one libheaptrack_preload.so from libheaptrack, "
            f"found {[str(path) for path in candidates]}"
        )
    return candidates[0]


def dynamic_dependencies(library: Path) -> list[str]:
    completed = profile_smoke.run_checked(["ldd", str(library)])
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        fail(f"ldd returned no dependencies for {library}")
    return lines


def validate_profiler_allocator_boundary(dependencies: list[str]) -> None:
    offenders = [
        line
        for line in dependencies
        if any(token in line.lower() for token in KNOWN_REPLACEMENT_ALLOCATOR_TOKENS)
    ]
    if offenders:
        fail(
            "heaptrack preload library dynamically depends on a replacement allocator "
            "and would invalidate the system-allocator attribution boundary: "
            + "; ".join(offenders)
        )


def capability_document() -> dict[str, Any]:
    versions = package_versions()
    heaptrack = find_tool("heaptrack")
    heaptrack_print = find_tool("heaptrack_print")
    preload = find_preload_library()
    dependencies = dynamic_dependencies(preload)
    validate_profiler_allocator_boundary(dependencies)

    version_run = profile_smoke.run_checked([heaptrack, "--version"])
    version_text = (version_run.stdout or version_run.stderr).strip()
    print_version_run = profile_smoke.run_checked([heaptrack_print, "--version"])
    print_version_text = (print_version_run.stdout or print_version_run.stderr).strip()
    if not version_text or not print_version_text:
        fail("heaptrack tools returned no version text")

    return {
        "schema": "oxide-batch-heaptrack-capability/v1",
        "profiler": "heaptrack",
        "packages": versions,
        "heaptrack_path": heaptrack,
        "heaptrack_version": version_text,
        "heaptrack_print_path": heaptrack_print,
        "heaptrack_print_version": print_version_text,
        "preload_library_path": str(preload),
        "preload_library_sha256": profile_smoke.sha256_file(preload),
        "preload_dynamic_dependencies": dependencies,
        "allocator_semantics_preserved": True,
        "coverage_scope": "system-allocator-malloc-family-interposition",
        "replacement_allocator_generic_coverage": "unsupported",
        "static_checks_passed": True,
    }


def runtime_capability_probe(output: Path) -> dict[str, Any]:
    capability = capability_document()
    probe_root = output.parent / ".heaptrack-runtime-probe"
    if probe_root.exists():
        shutil.rmtree(probe_root)
    probe_root.mkdir(parents=True, exist_ok=True)
    output_base = probe_root / "probe"
    true_binary = shutil.which("true")
    if true_binary is None:
        fail("runtime capability probe requires /usr/bin/true or equivalent")

    record_status = None
    print_status = None
    trace_bytes = 0
    report_nonempty = False
    record_stderr = None
    print_stderr = None
    try:
        record = subprocess.run(
            heaptrack_record_command(
                heaptrack=capability["heaptrack_path"],
                output_base=output_base,
                workload=[true_binary],
            ),
            check=False,
            text=True,
            capture_output=True,
            env=os.environ.copy(),
        )
        record_status = record.returncode
        record_stderr = record.stderr[-4000:]
        if record.returncode == 0:
            trace = find_native_trace(output_base)
            trace_bytes = trace.stat().st_size
            printed = subprocess.run(
                [
                    capability["heaptrack_print_path"],
                    "--file",
                    str(trace),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=os.environ.copy(),
            )
            print_status = printed.returncode
            print_stderr = printed.stderr[-4000:]
            report_nonempty = bool(printed.stdout.strip())
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)

    supported = (
        record_status == 0
        and print_status == 0
        and trace_bytes > 0
        and report_nonempty
    )
    document = {
        **capability,
        "runtime_probe": {
            "debuggee": true_binary,
            "record_exit_status": record_status,
            "record_stderr": record_stderr,
            "trace_bytes": trace_bytes,
            "heaptrack_print_exit_status": print_status,
            "heaptrack_print_stderr": print_stderr,
            "report_nonempty": report_nonempty,
        },
        "supported": supported,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    profile_smoke.write_json(output, document)
    return document


def write_probe(output: Path) -> int:
    document = runtime_capability_probe(output)
    print(json.dumps(document, sort_keys=True))
    if not document["supported"]:
        fail("heaptrack runtime capability probe failed")
    return 0


def heap_build_environment(base: dict[str, str]) -> dict[str, str]:
    existing_debug = base.get("CARGO_PROFILE_RELEASE_DEBUG")
    if existing_debug not in (None, "", "1"):
        fail(
            "refusing conflicting CARGO_PROFILE_RELEASE_DEBUG="
            f"{existing_debug!r}; heap attribution requires exact value '1'"
        )
    env = base.copy()
    env["CARGO_PROFILE_RELEASE_DEBUG"] = "1"
    env["CARGO_TARGET_DIR"] = str(Path("target/heap-profile-build").resolve())
    return env


def build_variant(*, target: str, output_root: Path, base_env: dict[str, str]) -> Path:
    cpu_profile.qualified_allocator(SYSTEM_ALLOCATOR, target)
    build_env = heap_build_environment(base_env)
    profile_smoke.run_checked(
        profile_smoke.cargo_build_args(SYSTEM_ALLOCATOR),
        env=build_env,
    )
    built = Path(build_env["CARGO_TARGET_DIR"]) / "release" / "postgres-postgres"
    if not built.is_file():
        fail(f"cargo build did not produce {built}")

    binary_root = output_root / "binary"
    binary_root.mkdir(parents=True, exist_ok=True)
    retained = binary_root / "postgres-postgres-system"
    shutil.copy2(built, retained)
    retained.chmod(0o755)
    return retained.resolve()


def heap_import_name(reader: str) -> str:
    value = f"heap_profile_system_{reader}"
    if not paired.IMPORT_NAME.fullmatch(value):
        fail(f"unsafe generated heap profile import name: {value!r}")
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


def heaptrack_record_command(
    *,
    heaptrack: str,
    output_base: Path,
    workload: list[str],
) -> list[str]:
    return [
        heaptrack,
        "--record-only",
        "--output",
        str(output_base),
        *workload,
    ]


def find_native_trace(output_base: Path) -> Path:
    candidates = sorted(
        path
        for path in output_base.parent.glob(output_base.name + ".*")
        if path.is_file() and path.suffix in {".gz", ".zst"}
    )
    if len(candidates) != 1:
        fail(
            f"expected one heaptrack native trace for {output_base}, "
            f"found {[str(path) for path in candidates]}"
        )
    if candidates[0].stat().st_size <= 0:
        fail("heaptrack native trace is empty")
    return candidates[0]


def projection_command(
    *,
    heaptrack_print: str,
    trace: Path,
    histogram: Path,
    allocation_stacks: Path,
    massif: Path,
) -> list[str]:
    return [
        heaptrack_print,
        "--file",
        str(trace),
        "--peak-limit",
        "20",
        "--sub-peak-limit",
        "10",
        "--print-histogram",
        str(histogram),
        "--print-flamegraph",
        str(allocation_stacks),
        "--flamegraph-cost-type",
        "allocations",
        "--print-massif",
        str(massif),
    ]


def temporary_stack_command(
    *,
    heaptrack_print: str,
    trace: Path,
    output: Path,
) -> list[str]:
    return [
        heaptrack_print,
        "--file",
        str(trace),
        "--print-flamegraph",
        str(output),
        "--flamegraph-cost-type",
        "temporary",
    ]


def parse_histogram(text: str) -> dict[str, int]:
    bucket_count = 0
    allocation_count = 0
    cumulative_allocated_bytes = 0
    largest_allocation_bytes = 0
    previous_size = -1

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            fail(f"invalid heaptrack histogram line {number}: {raw_line!r}")
        try:
            size = int(fields[0])
            count = int(fields[1])
        except ValueError as exc:
            raise RuntimeError(
                f"non-integer heaptrack histogram line {number}: {raw_line!r}"
            ) from exc
        if size < 0 or count <= 0:
            fail(f"invalid heaptrack histogram values at line {number}")
        if size <= previous_size:
            fail("heaptrack histogram sizes must be strictly increasing")
        previous_size = size
        bucket_count += 1
        allocation_count += count
        cumulative_allocated_bytes += size * count
        largest_allocation_bytes = max(largest_allocation_bytes, size)

    if allocation_count <= 0:
        fail("heaptrack histogram contained no allocations")
    return {
        "bucket_count": bucket_count,
        "allocation_count": allocation_count,
        "cumulative_allocated_bytes": cumulative_allocated_bytes,
        "largest_allocation_bytes": largest_allocation_bytes,
    }


def parse_report_counts(text: str) -> tuple[int, int]:
    allocations = ALLOCATION_COUNT.search(text)
    temporary = TEMPORARY_COUNT.search(text)
    if allocations is None or temporary is None:
        fail("heaptrack_print report is missing global allocation counters")
    allocation_count = int(allocations.group(1))
    temporary_count = int(temporary.group(1))
    if allocation_count <= 0:
        fail("heaptrack_print reported no allocations")
    if temporary_count < 0 or temporary_count > allocation_count:
        fail("heaptrack_print temporary allocation count is invalid")
    return allocation_count, temporary_count


def parse_massif_peak(text: str) -> int:
    values = [int(match) for match in MASSIF_HEAP.findall(text)]
    if not values:
        fail("heaptrack Massif projection contains no mem_heap_B snapshots")
    peak = max(values)
    if peak <= 0:
        fail("heaptrack Massif projection reported no positive live heap")
    return peak


def build_heap_summary(
    *,
    report_text: str,
    histogram_text: str,
    massif_text: str,
) -> dict[str, Any]:
    report_allocations, temporary_allocations = parse_report_counts(report_text)
    histogram = parse_histogram(histogram_text)
    if histogram["allocation_count"] != report_allocations:
        fail(
            "heaptrack histogram allocation count does not match report: "
            f"{histogram['allocation_count']} != {report_allocations}"
        )
    peak_live_bytes = parse_massif_peak(massif_text)
    return {
        "schema": "oxide-batch-heap-profile-summary/v1",
        "allocation_count": report_allocations,
        "cumulative_allocated_bytes": histogram["cumulative_allocated_bytes"],
        "peak_live_bytes": peak_live_bytes,
        "temporary_allocation_count": temporary_allocations,
        "temporary_allocation_ratio": temporary_allocations / report_allocations,
        "size_histogram_bucket_count": histogram["bucket_count"],
        "largest_allocation_bytes": histogram["largest_allocation_bytes"],
        "coverage_scope": "system-allocator-malloc-family-interposition",
        "timing_claim": "attribution-only",
    }


def heap_limitations() -> list[str]:
    return [
        "heaptrack runtime is allocation-attribution evidence only and must not be compared with benchmark-lane timing",
        "generic heaptrack coverage is intentionally limited to the system allocator because replacement Rust global allocators can bypass malloc-family interposition",
        "the profiled release build sets CARGO_PROFILE_RELEASE_DEBUG=1 for symbolization and is not the benchmark-lane binary",
        "cumulative allocated bytes are derived from heaptrack's event-counted size histogram and are accepted only when histogram count equals the heaptrack global allocation count",
        "heaptrack peak-live bytes are derived from the Massif projection; the native heaptrack trace remains authoritative",
        "hosted-runner heap attribution validates orchestration and trace readability, not stable-host absolute memory-performance truth",
        "OxideBatch 0.6.0 Phase 0 evidence must not be generalized to the post-M7 allocator decision",
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
    heaptrack_version: str,
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
                    "name": "heaptrack",
                    "version": heaptrack_version,
                    "role": "heap/allocation tracer with allocation-stack attribution",
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
        "limitations": heap_limitations(),
    }
    profile_manifest.validate_manifest(manifest)
    return manifest


def run_profile(args: argparse.Namespace) -> dict[str, Any]:
    validate_bounds(args)
    validate_allocator_scope(args.allocator)
    profile_smoke.prepare_output_root(args.output)

    base_env = os.environ.copy()
    if base_env.get("LD_PRELOAD"):
        fail("refusing heap attribution with a pre-existing LD_PRELOAD")
    base_env["RUST_LOG"] = "error"

    capability = capability_document()
    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    cpu_profile.qualified_allocator(SYSTEM_ALLOCATOR, target)
    binary = build_variant(target=target, output_root=args.output, base_env=base_env)

    subject_sha = profile_smoke.workload_sha()
    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = profile_smoke.sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.6.0")
    profile_smoke.validate_allocator_lock(lock_path, target)

    token = profile_smoke.run_token()
    template_database = profile_smoke.database_name(token, "heap_template")
    sample_database = profile_smoke.database_name(token, "heap_system")
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
        import_value = heap_import_name(args.reader)

        heap_root = args.output / "heap"
        correctness_root = args.output / "correctness"
        heap_root.mkdir(parents=True, exist_ok=True)
        correctness_root.mkdir(parents=True, exist_ok=True)

        output_base = heap_root / "heaptrack"
        workload = workload_command(
            binary=binary,
            import_value=import_value,
            reader=args.reader,
            chunk_size=args.chunk_size,
            fetch_size=args.fetch_size,
            page_size=args.page_size,
        )
        profiled_env = base_env.copy()
        profiled_env["DATABASE_URL"] = database_url

        record = subprocess.run(
            heaptrack_record_command(
                heaptrack=capability["heaptrack_path"],
                output_base=output_base,
                workload=workload,
            ),
            check=False,
            text=True,
            capture_output=True,
            env=profiled_env,
        )
        record_stdout = heap_root / "heaptrack.stdout.txt"
        record_stderr = heap_root / "heaptrack.stderr.txt"
        record_stdout.write_text(record.stdout, encoding="utf-8")
        record_stderr.write_text(record.stderr, encoding="utf-8")
        if record.returncode != 0:
            fail(
                f"heaptrack exited {record.returncode}: "
                f"{record.stderr[-2000:]}"
            )
        native_trace = find_native_trace(output_base)

        report_path = heap_root / "heaptrack-report.txt"
        report_stderr_path = heap_root / "heaptrack-print.stderr.txt"
        histogram_path = heap_root / "allocation-size-histogram.tsv"
        allocation_stacks_path = heap_root / "allocation-stacks.folded"
        temporary_stacks_path = heap_root / "temporary-allocation-stacks.folded"
        massif_path = heap_root / "heaptrack.massif"

        projection = subprocess.run(
            projection_command(
                heaptrack_print=capability["heaptrack_print_path"],
                trace=native_trace,
                histogram=histogram_path,
                allocation_stacks=allocation_stacks_path,
                massif=massif_path,
            ),
            check=False,
            text=True,
            capture_output=True,
            env=base_env,
        )
        report_path.write_text(projection.stdout, encoding="utf-8")
        report_stderr_path.write_text(projection.stderr, encoding="utf-8")
        if projection.returncode != 0:
            fail(
                f"heaptrack_print exited {projection.returncode}: "
                f"{projection.stderr[-2000:]}"
            )
        if not projection.stdout.strip():
            fail("heaptrack_print report projection was empty")

        temporary_projection = subprocess.run(
            temporary_stack_command(
                heaptrack_print=capability["heaptrack_print_path"],
                trace=native_trace,
                output=temporary_stacks_path,
            ),
            check=False,
            text=True,
            capture_output=True,
            env=base_env,
        )
        if temporary_projection.returncode != 0:
            fail(
                "heaptrack_print temporary-stack projection exited "
                f"{temporary_projection.returncode}: "
                f"{temporary_projection.stderr[-2000:]}"
            )

        for path in (
            histogram_path,
            allocation_stacks_path,
            massif_path,
        ):
            if not path.is_file() or path.stat().st_size <= 0:
                fail(f"heaptrack projection is missing or empty: {path}")
        if not temporary_stacks_path.is_file():
            fail("heaptrack temporary-allocation stack projection is missing")

        summary = build_heap_summary(
            report_text=report_path.read_text(encoding="utf-8"),
            histogram_text=histogram_path.read_text(encoding="utf-8"),
            massif_text=massif_path.read_text(encoding="utf-8"),
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

        capability_path = heap_root / "capability.json"
        profile_smoke.write_json(capability_path, capability)

        run_metadata = {
            "schema": "oxide-batch-heap-profile-run/v1",
            "profiler": {
                "name": "heaptrack",
                "packages": capability["packages"],
                "heaptrack_version": capability["heaptrack_version"],
                "heaptrack_print_version": capability["heaptrack_print_version"],
                "preload_library_path": capability["preload_library_path"],
                "preload_library_sha256": capability["preload_library_sha256"],
                "preload_dynamic_dependencies": capability[
                    "preload_dynamic_dependencies"
                ],
            },
            "allocator": SYSTEM_ALLOCATOR,
            "reader_mode": args.reader,
            "rows": args.rows,
            "seed": args.seed,
            "stack_depth": "heaptrack-1.5-tool-default",
            "build_flags": ["CARGO_PROFILE_RELEASE_DEBUG=1"],
            "profiled_binary_sha256": profile_smoke.sha256_file(binary),
            "native_trace": native_trace.name,
            "child_exit_status": record.returncode,
            "timing_claim": "attribution-only",
            "coverage_scope": capability["coverage_scope"],
        }
        metadata_path = heap_root / "run.json"
        profile_smoke.write_json(metadata_path, run_metadata)

        artifacts = [
            profile_smoke.artifact_record(args.output, native_trace),
            profile_smoke.artifact_record(args.output, record_stdout),
            profile_smoke.artifact_record(args.output, record_stderr),
            {
                **profile_smoke.artifact_record(args.output, report_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, report_stderr_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, histogram_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, allocation_stacks_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, temporary_stacks_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, massif_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, summary_path),
                "kind": "projection",
            },
            profile_smoke.artifact_record(args.output, capability_path),
            profile_smoke.artifact_record(args.output, metadata_path),
            profile_smoke.artifact_record(args.output, verifier_path),
            profile_smoke.artifact_record(args.output, binary),
        ]

        manifest = build_manifest(
            run_id=f"heap-profile-{token}-system-{args.reader}",
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
            heaptrack_version=capability["packages"]["heaptrack"],
        )
        profile_smoke.write_json(args.output / "manifest.json", manifest)
        return manifest
    finally:
        paired.compose_db("dropdb", sample_database)
        paired.compose_db("dropdb", template_database)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument("--base-database-url")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allocator",
        default=SYSTEM_ALLOCATOR,
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
    args = parser.parse_args()

    if args.probe_output is not None:
        if args.base_database_url is not None or args.output is not None:
            parser.error(
                "--probe-output cannot be combined with profile execution arguments"
            )
        return write_probe(args.probe_output)

    if args.base_database_url is None or args.output is None:
        parser.error(
            "--base-database-url and --output are required for profile execution"
        )

    manifest = run_profile(args)
    print(
        json.dumps(
            {
                "valid": True,
                "lane": manifest["lane"],
                "allocator": manifest["allocator"]["identity"],
                "reader": manifest["workload"]["reader_mode"],
                "profiler": manifest["instrumentation"]["tools"][0],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
