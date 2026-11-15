#!/usr/bin/env python3
"""Run campaign #266 allocator smoke variants and emit validated evidence manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import paired
import profile_manifest

ALLOCATORS = ("system", "jemalloc", "mimalloc")
ALLOCATOR_FEATURES = {
    "system": None,
    "jemalloc": "allocator-jemalloc",
    "mimalloc": "allocator-mimalloc",
}
MAX_ROWS = 100_000
MAX_SIZE = 100_000
SHA40 = re.compile(r"^[0-9a-f]{40}$")
JEMALLOC_SYS_VERSION = "0.7.1+5.3.1-0-g81034ce1f1373e37dc865038e1bc8eeecf559ce8"
MIMALLOC_SYS_VERSION = "0.1.49"


def fail(message: str) -> None:
    raise RuntimeError(message)


def run_checked(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True, env=env)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_rustc_host(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("host: "):
            host = line.split(":", 1)[1].strip()
            if host:
                return host
    fail("rustc -vV did not report a host target")


def parse_memory_total_bytes(text: str) -> int:
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            if len(fields) == 3 and fields[2] == "kB" and fields[1].isdigit():
                value = int(fields[1]) * 1024
                if value > 0:
                    return value
    fail("could not parse positive MemTotal from /proc/meminfo")


def parse_cpu_model(text: str) -> str:
    preferred = ("model name", "Model", "Processor", "Hardware")
    values: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if value and key not in values:
            values[key] = value
    for key in preferred:
        if values.get(key):
            return values[key]
    fail("could not determine CPU model from /proc/cpuinfo")


def cargo_build_args(allocator: str) -> list[str]:
    if allocator not in ALLOCATOR_FEATURES:
        raise ValueError(f"unknown allocator: {allocator}")
    command = [
        "cargo",
        "build",
        "--locked",
        "--release",
        "-p",
        "postgres-postgres",
        "--no-default-features",
    ]
    feature = ALLOCATOR_FEATURES[allocator]
    if feature:
        command.extend(["--features", feature])
    return command


def allocator_record(allocator: str) -> dict[str, Any]:
    if allocator == "system":
        return {
            "identity": "system",
            "wrapper": None,
            "upstream": {
                "status": "unavailable",
                "version": None,
                "reason": "system allocator version is host/toolchain-owned",
            },
        }
    if allocator == "jemalloc":
        return {
            "identity": "jemalloc",
            "wrapper": {"name": "tikv-jemallocator", "version": "0.7.0"},
            "upstream": {"status": "observed", "version": "5.3.1"},
        }
    if allocator == "mimalloc":
        return {
            "identity": "mimalloc",
            "wrapper": {"name": "mimalloc", "version": "0.1.52"},
            "upstream": {
                "status": "unavailable",
                "version": None,
                "reason": (
                    "libmimalloc-sys 0.1.49 does not encode the exact bundled "
                    "upstream mimalloc revision"
                ),
            },
        }
    raise ValueError(f"unknown allocator: {allocator}")


def validate_allocator_lock(lock_path: Path) -> None:
    paired.lock_package(lock_path, "tikv-jemalloc-sys", JEMALLOC_SYS_VERSION)
    paired.lock_package(lock_path, "libmimalloc-sys", MIMALLOC_SYS_VERSION)


def prepare_output_root(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        fail(f"output directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def host_environment() -> tuple[dict[str, Any], str, str]:
    rustc_vv = run_checked(["rustc", "-vV"]).stdout
    rustc_version = rustc_vv.splitlines()[0].strip()
    target = parse_rustc_host(rustc_vv)

    cpuinfo = Path("/proc/cpuinfo")
    meminfo = Path("/proc/meminfo")
    if not cpuinfo.is_file() or not meminfo.is_file():
        fail("profile smoke currently requires Linux /proc host metadata")

    cpu_count = os.cpu_count()
    if not isinstance(cpu_count, int) or cpu_count <= 0:
        fail("could not determine positive logical CPU count")

    images = [
        line.strip()
        for line in run_checked(["docker", "compose", "config", "--images"]).stdout.splitlines()
        if line.strip()
    ]
    images = sorted(set(images))
    if len(images) != 1:
        fail(f"expected exactly one configured PostgreSQL image, found {images!r}")

    postgres_server = run_checked(
        ["docker", "compose", "exec", "-T", "postgres", "postgres", "--version"]
    ).stdout.strip()
    if not postgres_server:
        fail("PostgreSQL server version observation was empty")

    environment = {
        "os": platform.system(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_model": parse_cpu_model(cpuinfo.read_text(encoding="utf-8", errors="replace")),
        "logical_cpu_count": cpu_count,
        "memory_total_bytes": parse_memory_total_bytes(
            meminfo.read_text(encoding="utf-8", errors="replace")
        ),
        "postgres_configured_image": images[0],
        "postgres_server_version": postgres_server,
    }
    return environment, rustc_version, target


def workload_sha() -> str:
    value = run_checked(["git", "rev-parse", "HEAD"]).stdout.strip()
    if not SHA40.fullmatch(value):
        fail(f"git HEAD is not a lowercase 40-character SHA: {value!r}")
    return value


def run_token() -> str:
    raw = "_".join(
        part
        for part in (
            os.getenv("GITHUB_RUN_ID", ""),
            os.getenv("GITHUB_RUN_ATTEMPT", ""),
        )
        if part
    )
    if not raw:
        raw = f"local_{os.getpid()}"
    value = re.sub(r"[^a-zA-Z0-9_]", "_", raw).lower()
    return value[-24:]


def database_name(token: str, suffix: str) -> str:
    value = f"profile_{token}_{suffix}"[:63]
    if not paired.DATABASE_NAME.fullmatch(value):
        fail(f"unsafe generated database name: {value!r}")
    return value


def import_name(allocator: str, reader: str) -> str:
    value = f"profile_smoke_{allocator}_{reader}"
    if not paired.IMPORT_NAME.fullmatch(value):
        fail(f"unsafe generated import name: {value!r}")
    return value


def build_variants() -> dict[str, Path]:
    destination_root = Path("target/profile-smoke-binaries")
    destination_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for allocator in ALLOCATORS:
        run_checked(cargo_build_args(allocator))
        built = Path("target/release/postgres-postgres")
        if not built.is_file():
            fail(f"cargo build did not produce {built}")
        destination = destination_root / f"postgres-postgres-{allocator}"
        shutil.copy2(built, destination)
        destination.chmod(0o755)
        result[allocator] = destination.resolve()
    return result


def run_verifier(
    *,
    binary: Path,
    database_url: str,
    import_value: str,
    rows: int,
    env: dict[str, str],
) -> tuple[dict[str, Any], str]:
    completed = subprocess.run(
        [
            str(binary),
            "verify",
            "--database-url",
            database_url,
            "--import-name",
            import_value,
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    report = paired.parse_verify_report(completed.stdout, rows)
    if completed.returncode != 0:
        fail(f"verifier exited {completed.returncode}: {completed.stderr[-2000:]}")
    return report, completed.stdout


def artifact_record(root: Path, path: Path) -> dict[str, str]:
    relative = path.relative_to(root).as_posix()
    return {"path": relative, "sha256": sha256_file(path), "kind": "raw"}


def build_manifest(
    *,
    allocator: str,
    run_id: str,
    subject_sha: str,
    cargo_lock_sha256: str,
    rustc_version: str,
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
    elapsed_seconds: float,
) -> dict[str, Any]:
    manifest = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": "benchmark",
        "run": {"id": run_id, "repetition": 1},
        "subject": {
            "workload_sha": subject_sha,
            "cargo_lock_sha256": cargo_lock_sha256,
            "rustc": rustc_version,
            "target": target,
            "build_profile": "release",
            "oxide_batch": {
                "name": oxide_subject["name"],
                "version": oxide_subject["version"],
                "source": oxide_subject["source"],
                "checksum": oxide_subject["checksum"],
            },
        },
        "allocator": allocator_record(allocator),
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
        "instrumentation": {"profiler_attached": False, "tools": []},
        "artifacts": artifacts,
        "correctness": {
            "verdict": "PASS",
            "verifier": "postgres-postgres verify",
            "expected_digest_sha256": verifier_report["expected_digest_sha256"],
            "actual_digest_sha256": verifier_report["actual_digest_sha256"],
        },
        "limitations": [
            "bounded diagnostic smoke only; timing is not claim-bearing allocator evidence",
            "no profiler is attached in smoke mode",
            "hosted-runner timing is observational and not stable-host absolute performance",
        ],
        "benchmark_timing": {
            "elapsed_seconds": elapsed_seconds,
            "rows_per_second": rows / elapsed_seconds,
        },
    }
    profile_manifest.validate_manifest(manifest)
    return manifest


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_bounds(args: argparse.Namespace) -> None:
    for name in ("rows", "chunk_size", "fetch_size", "page_size"):
        value = getattr(args, name)
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    if args.rows > MAX_ROWS:
        raise ValueError(f"rows must be <= {MAX_ROWS} for smoke mode")
    for name in ("chunk_size", "fetch_size", "page_size"):
        if getattr(args, name) > MAX_SIZE:
            raise ValueError(f"{name} must be <= {MAX_SIZE} for smoke mode")
    if args.seed < 0:
        raise ValueError("seed must be non-negative")


def run_variant(
    *,
    allocator: str,
    binary: Path,
    template_database: str,
    base_database_url: str,
    output_root: Path,
    token: str,
    environment: dict[str, Any],
    rustc_version: str,
    target: str,
    subject_sha: str,
    cargo_lock_sha256: str,
    oxide_subject: dict[str, Any],
    args: argparse.Namespace,
    env: dict[str, str],
) -> dict[str, Any]:
    sample_database = database_name(token, allocator)
    paired.compose_db("dropdb", sample_database)
    paired.compose_db("createdb", sample_database, template=template_database)
    database_url = paired.database_url(base_database_url, sample_database)
    import_value = import_name(allocator, args.reader)

    result_root = output_root / allocator
    benchmark_root = result_root / "benchmark"
    benchmark_root.mkdir(parents=True, exist_ok=True)

    try:
        command = [
            str(binary),
            "run",
            "--database-url",
            database_url,
            "--import-name",
            import_value,
            "--chunk-size",
            str(args.chunk_size),
            *paired.reader_args(args.reader, args.fetch_size, args.page_size),
        ]
        timing = paired.timed_process(command, env=env)
        verifier_report, verifier_stdout = run_verifier(
            binary=binary,
            database_url=database_url,
            import_value=import_value,
            rows=args.rows,
            env=env,
        )

        measurement = {
            "schema": "oxide-batch-profile-smoke-measurement/v1",
            "allocator": allocator,
            "reader_mode": args.reader,
            "rows": args.rows,
            "seed": args.seed,
            "chunk_size": args.chunk_size,
            "fetch_size": args.fetch_size if args.reader == "cursor" else None,
            "page_size": args.page_size if args.reader == "paging" else None,
            "binary_sha256": sha256_file(binary),
            "exit_status": timing["exit_status"],
            "elapsed_seconds": timing["elapsed_seconds"],
            "gnu_time_elapsed_seconds": timing["gnu_time_elapsed_seconds"],
            "user_cpu_seconds": timing["user_cpu_seconds"],
            "system_cpu_seconds": timing["system_cpu_seconds"],
            "max_rss_kib": timing["max_rss_kib"],
        }
        measurement_path = benchmark_root / "measurement.json"
        verifier_path = benchmark_root / "verifier.json"
        write_json(measurement_path, measurement)
        verifier_path.write_text(verifier_stdout, encoding="utf-8")

        artifacts = [
            artifact_record(result_root, measurement_path),
            artifact_record(result_root, verifier_path),
        ]
        manifest = build_manifest(
            allocator=allocator,
            run_id=f"smoke-{token}-{allocator}-{args.reader}",
            subject_sha=subject_sha,
            cargo_lock_sha256=cargo_lock_sha256,
            rustc_version=rustc_version,
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
            elapsed_seconds=float(timing["elapsed_seconds"]),
        )
        write_json(result_root / "manifest.json", manifest)
        return {
            "allocator": allocator,
            "manifest": str(result_root / "manifest.json"),
            "correctness": "PASS",
        }
    finally:
        paired.compose_db("dropdb", sample_database)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=127)
    parser.add_argument("--seed", type=int, default=266)
    parser.add_argument("--chunk-size", type=int, default=32)
    parser.add_argument("--reader", choices=("cursor", "paging"), default="paging")
    parser.add_argument("--fetch-size", type=int, default=17)
    parser.add_argument("--page-size", type=int, default=17)
    args = parser.parse_args()
    validate_bounds(args)
    prepare_output_root(args.output)

    env = os.environ.copy()
    env["RUST_LOG"] = "error"

    binaries = build_variants()
    environment, rustc_version, target = host_environment()
    subject_sha = workload_sha()
    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.6.0")
    validate_allocator_lock(lock_path)

    token = run_token()
    template_database = database_name(token, "template")
    paired.compose_db("dropdb", template_database)
    paired.compose_db("createdb", template_database, template="template0")
    template_url = paired.database_url(args.base_database_url, template_database)

    try:
        system_binary = binaries["system"]
        run_checked([str(system_binary), "migrate", "--database-url", template_url], env=env)
        run_checked(
            [
                str(system_binary),
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
        summaries = [
            run_variant(
                allocator=allocator,
                binary=binaries[allocator],
                template_database=template_database,
                base_database_url=args.base_database_url,
                output_root=args.output,
                token=token,
                environment=environment,
                rustc_version=rustc_version,
                target=target,
                subject_sha=subject_sha,
                cargo_lock_sha256=cargo_lock_sha256,
                oxide_subject=oxide_subject,
                args=args,
                env=env,
            )
            for allocator in ALLOCATORS
        ]
    finally:
        paired.compose_db("dropdb", template_database)

    print(json.dumps({"valid": True, "runs": summaries}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
