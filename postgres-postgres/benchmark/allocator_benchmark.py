#!/usr/bin/env python3
"""Run campaign #323 allocator benchmarks with strict claim boundaries."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import allocator_registry
import paired
import profile_manifest
import profile_smoke

ALLOCATOR_REGISTRY = allocator_registry.load_registry()
CANONICAL_ROWS = 1_000_000
CANONICAL_SEED = 20260904
CANONICAL_CHUNK_SIZE = 1000
CANONICAL_FETCH_SIZE = 500
CANONICAL_PAGE_SIZE = 750
CANONICAL_WARMUPS = 2
CANONICAL_MEASURED_RUNS = 7
MAX_ROWS = 10_000_000
MAX_SIZE = 1_000_000
MAX_WARMUPS = 10
MAX_MEASURED_RUNS = 20
READERS = ("cursor", "paging")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
    raise RuntimeError(message)


def allocator_ids_for_target(target: str) -> tuple[str, ...]:
    candidates = allocator_registry.qualified_active_candidates(
        ALLOCATOR_REGISTRY, target
    )
    if not candidates:
        fail(f"no active allocators are qualified for target {target}")
    return tuple(candidate["identity"] for candidate in candidates)


def allocator_order(allocators: tuple[str, ...], ordinal: int) -> tuple[str, ...]:
    if not allocators:
        raise ValueError("allocator order requires at least one allocator")
    if ordinal < 0:
        raise ValueError("allocator order ordinal must be non-negative")
    size = len(allocators)
    cycle, offset = divmod(ordinal, size)
    rotated = allocators[offset:] + allocators[:offset]
    if cycle % 2:
        rotated = tuple(reversed(rotated))
    return rotated


def reader_modes(reader: str) -> tuple[str, ...]:
    if reader == "both":
        return READERS
    if reader in READERS:
        return (reader,)
    raise ValueError(f"unknown reader selection: {reader}")


def is_canonical(args: argparse.Namespace) -> bool:
    return (
        args.reader == "both"
        and args.rows == CANONICAL_ROWS
        and args.seed == CANONICAL_SEED
        and args.chunk_size == CANONICAL_CHUNK_SIZE
        and args.fetch_size == CANONICAL_FETCH_SIZE
        and args.page_size == CANONICAL_PAGE_SIZE
        and args.warmups == CANONICAL_WARMUPS
        and args.measured_runs == CANONICAL_MEASURED_RUNS
    )


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
    if args.warmups < 0 or args.warmups > MAX_WARMUPS:
        raise ValueError(f"warmups must be between 0 and {MAX_WARMUPS}")
    if args.measured_runs <= 0 or args.measured_runs > MAX_MEASURED_RUNS:
        raise ValueError(
            f"measured_runs must be between 1 and {MAX_MEASURED_RUNS}"
        )
    reader_modes(args.reader)


def sample_database_name(
    token: str,
    reader: str,
    phase: str,
    repetition: int,
    allocator: str,
) -> str:
    value = (
        f"allocbench_{token[-8:]}_{reader[0]}_{phase[0]}"
        f"{repetition:02d}_{allocator}"
    )[:63]
    if not paired.DATABASE_NAME.fullmatch(value):
        fail(f"unsafe generated benchmark database name: {value!r}")
    return value


def sample_import_name(
    reader: str,
    phase: str,
    repetition: int,
    allocator: str,
) -> str:
    value = f"allocator_benchmark_{reader}_{phase}_{repetition:02d}_{allocator}"
    if not paired.IMPORT_NAME.fullmatch(value):
        fail(f"unsafe generated benchmark import name: {value!r}")
    return value


def benchmark_limitations(canonical: bool) -> list[str]:
    limitations = [
        "no profiler is attached in benchmark lane",
        "benchmark timing is same-host relative evidence; absolute values are host-specific and must remain bound to retained host provenance",
        "OxideBatch 0.7.0 evidence must not be generalized to later releases or a permanent allocator decision",
    ]
    if not canonical:
        limitations.append(
            "non-canonical benchmark geometry; diagnostic only and not campaign acceptance evidence"
        )
    return limitations


def build_sample_manifest(
    *,
    allocator: str,
    run_id: str,
    repetition: int,
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
    elapsed_seconds: float,
    canonical: bool,
) -> dict[str, Any]:
    manifest = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": "benchmark",
        "run": {"id": run_id, "repetition": repetition},
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
        "instrumentation": {"profiler_attached": False, "tools": []},
        "artifacts": artifacts,
        "correctness": {
            "verdict": "PASS",
            "verifier": "postgres-postgres verify",
            "expected_digest_sha256": verifier_report["expected_digest_sha256"],
            "actual_digest_sha256": verifier_report["actual_digest_sha256"],
        },
        "limitations": benchmark_limitations(canonical),
        "benchmark_timing": {
            "elapsed_seconds": elapsed_seconds,
            "rows_per_second": rows / elapsed_seconds,
        },
    }
    profile_manifest.validate_manifest(manifest)
    return manifest


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise ValueError("cannot summarize empty benchmark sample set")
    return {
        "sample_count": len(samples),
        "elapsed_seconds": paired.distribution(
            [sample["elapsed_seconds"] for sample in samples]
        ),
        "rows_per_second": paired.distribution(
            [sample["rows_per_second"] for sample in samples]
        ),
        "user_cpu_seconds": paired.distribution(
            [sample["user_cpu_seconds"] for sample in samples]
        ),
        "system_cpu_seconds": paired.distribution(
            [sample["system_cpu_seconds"] for sample in samples]
        ),
        "max_rss_kib": paired.distribution(
            [sample["max_rss_kib"] for sample in samples]
        ),
    }


def run_sample(
    *,
    allocator: str,
    binary: Path,
    reader: str,
    phase: str,
    repetition: int,
    order_position: int,
    template_database: str,
    base_database_url: str,
    token: str,
    args: argparse.Namespace,
    env: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    database = sample_database_name(token, reader, phase, repetition, allocator)
    import_value = sample_import_name(reader, phase, repetition, allocator)
    paired.compose_db("dropdb", database)
    paired.compose_db("createdb", database, template=template_database)
    database_url = paired.database_url(base_database_url, database)

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
            *paired.reader_args(reader, args.fetch_size, args.page_size),
        ]
        timing = paired.timed_process(command, env=env)
        verifier_report, verifier_stdout = profile_smoke.run_verifier(
            binary=binary,
            database_url=database_url,
            import_value=import_value,
            rows=args.rows,
            env=env,
        )
        measurement = {
            "schema": "oxide-batch-allocator-benchmark-measurement/v1",
            "allocator": allocator,
            "reader_mode": reader,
            "phase": phase,
            "repetition": repetition,
            "order_position": order_position,
            "rows": args.rows,
            "seed": args.seed,
            "chunk_size": args.chunk_size,
            "fetch_size": args.fetch_size if reader == "cursor" else None,
            "page_size": args.page_size if reader == "paging" else None,
            "binary_sha256": profile_smoke.sha256_file(binary),
            "exit_status": timing["exit_status"],
            "elapsed_seconds": timing["elapsed_seconds"],
            "gnu_time_elapsed_seconds": timing["gnu_time_elapsed_seconds"],
            "user_cpu_seconds": timing["user_cpu_seconds"],
            "system_cpu_seconds": timing["system_cpu_seconds"],
            "max_rss_kib": timing["max_rss_kib"],
            "rows_per_second": args.rows / float(timing["elapsed_seconds"]),
        }
        return measurement, verifier_report, verifier_stdout
    finally:
        paired.compose_db("dropdb", database)


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    validate_bounds(args)
    profile_smoke.prepare_output_root(args.output)

    env = os.environ.copy()
    env["RUST_LOG"] = "error"

    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    allocators = allocator_ids_for_target(target)
    binaries = profile_smoke.build_variants(target)
    subject_sha = profile_smoke.workload_sha()

    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = profile_smoke.sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.7.0")
    profile_smoke.validate_allocator_lock(lock_path, target)

    token = profile_smoke.run_token()
    template_database = profile_smoke.database_name(token, "benchmark_template")
    paired.compose_db("dropdb", template_database)
    paired.compose_db("createdb", template_database, template="template0")
    template_url = paired.database_url(args.base_database_url, template_database)

    canonical = is_canonical(args)
    orders: dict[str, list[dict[str, Any]]] = {"warmup": [], "measured": []}
    warmup_correctness: list[dict[str, Any]] = []
    measured: dict[str, dict[str, list[dict[str, Any]]]] = {
        reader: {allocator: [] for allocator in allocators}
        for reader in reader_modes(args.reader)
    }
    dataset_digests: set[str] = set()

    try:
        system_binary = binaries["system"]
        profile_smoke.run_checked(
            [str(system_binary), "migrate", "--database-url", template_url],
            env=env,
        )
        profile_smoke.run_checked(
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

        for reader in reader_modes(args.reader):
            warmup_ordinal = 0
            measured_ordinal = 0

            for repetition in range(1, args.warmups + 1):
                order = allocator_order(allocators, warmup_ordinal)
                warmup_ordinal += 1
                orders["warmup"].append(
                    {"reader": reader, "repetition": repetition, "order": list(order)}
                )
                for position, allocator in enumerate(order, start=1):
                    _, verifier, _ = run_sample(
                        allocator=allocator,
                        binary=binaries[allocator],
                        reader=reader,
                        phase="warmup",
                        repetition=repetition,
                        order_position=position,
                        template_database=template_database,
                        base_database_url=args.base_database_url,
                        token=token,
                        args=args,
                        env=env,
                    )
                    dataset_digests.add(verifier["source_digest"])
                    warmup_correctness.append(
                        {
                            "reader": reader,
                            "repetition": repetition,
                            "allocator": allocator,
                            "verdict": "PASS",
                            "source_digest": verifier["source_digest"],
                        }
                    )

            for repetition in range(1, args.measured_runs + 1):
                order = allocator_order(allocators, measured_ordinal)
                measured_ordinal += 1
                orders["measured"].append(
                    {"reader": reader, "repetition": repetition, "order": list(order)}
                )
                for position, allocator in enumerate(order, start=1):
                    measurement, verifier, verifier_stdout = run_sample(
                        allocator=allocator,
                        binary=binaries[allocator],
                        reader=reader,
                        phase="measured",
                        repetition=repetition,
                        order_position=position,
                        template_database=template_database,
                        base_database_url=args.base_database_url,
                        token=token,
                        args=args,
                        env=env,
                    )
                    dataset_digests.add(verifier["source_digest"])

                    sample_root = (
                        args.output
                        / allocator
                        / reader
                        / f"measured-{repetition:02d}"
                    )
                    benchmark_root = sample_root / "benchmark"
                    benchmark_root.mkdir(parents=True, exist_ok=True)
                    measurement_path = benchmark_root / "measurement.json"
                    verifier_path = benchmark_root / "verifier.json"
                    profile_smoke.write_json(measurement_path, measurement)
                    verifier_path.write_text(verifier_stdout, encoding="utf-8")

                    artifacts = [
                        profile_smoke.artifact_record(sample_root, measurement_path),
                        profile_smoke.artifact_record(sample_root, verifier_path),
                    ]
                    manifest = build_sample_manifest(
                        allocator=allocator,
                        run_id=(
                            f"allocator-benchmark-{token}-{reader}-"
                            f"{repetition:02d}-{allocator}"
                        ),
                        repetition=repetition,
                        subject_sha=subject_sha,
                        cargo_lock_sha256=cargo_lock_sha256,
                        rustc_version=rustc_version,
                        cargo_version=cargo_version,
                        target=target,
                        oxide_subject=oxide_subject,
                        environment=environment,
                        reader=reader,
                        rows=args.rows,
                        seed=args.seed,
                        chunk_size=args.chunk_size,
                        fetch_size=args.fetch_size,
                        page_size=args.page_size,
                        dataset_identity=verifier["source_digest"],
                        artifacts=artifacts,
                        verifier_report=verifier,
                        elapsed_seconds=float(measurement["elapsed_seconds"]),
                        canonical=canonical,
                    )
                    profile_smoke.write_json(sample_root / "manifest.json", manifest)
                    measured[reader][allocator].append(measurement)
    finally:
        paired.compose_db("dropdb", template_database)

    if len(dataset_digests) != 1:
        fail(
            "allocator benchmark samples did not observe one canonical source digest: "
            f"{sorted(dataset_digests)}"
        )

    summary = {
        "schema": "oxide-batch-allocator-benchmark-summary/v1",
        "campaign": profile_manifest.CAMPAIGN,
        "subject": {
            "workload_sha": subject_sha,
            "cargo_lock_sha256": cargo_lock_sha256,
            "rustc": rustc_version,
            "cargo": cargo_version,
            "target": target,
            "oxide_batch": oxide_subject,
        },
        "environment": environment,
        "canonical_geometry": canonical,
        "claim_scope": (
            "same-host-relative-observational"
            if canonical
            else "diagnostic-noncanonical"
        ),
        "parameters": {
            "reader": args.reader,
            "rows": args.rows,
            "seed": args.seed,
            "chunk_size": args.chunk_size,
            "fetch_size": args.fetch_size,
            "page_size": args.page_size,
            "warmups": args.warmups,
            "measured_runs": args.measured_runs,
        },
        "allocators": list(allocators),
        "orders": orders,
        "warmup_correctness": warmup_correctness,
        "dataset_identity": next(iter(dataset_digests)),
        "readers": {
            reader: {
                allocator: summarize_samples(samples)
                for allocator, samples in allocator_samples.items()
            }
            for reader, allocator_samples in measured.items()
        },
        "limitations": benchmark_limitations(canonical),
    }
    profile_smoke.write_json(args.output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=CANONICAL_ROWS)
    parser.add_argument("--seed", type=int, default=CANONICAL_SEED)
    parser.add_argument("--chunk-size", type=int, default=CANONICAL_CHUNK_SIZE)
    parser.add_argument("--reader", choices=("cursor", "paging", "both"), default="both")
    parser.add_argument("--fetch-size", type=int, default=CANONICAL_FETCH_SIZE)
    parser.add_argument("--page-size", type=int, default=CANONICAL_PAGE_SIZE)
    parser.add_argument("--warmups", type=int, default=CANONICAL_WARMUPS)
    parser.add_argument("--measured-runs", type=int, default=CANONICAL_MEASURED_RUNS)
    args = parser.parse_args()

    summary = run_benchmark(args)
    print(
        json.dumps(
            {
                "valid": True,
                "schema": summary["schema"],
                "canonical_geometry": summary["canonical_geometry"],
                "claim_scope": summary["claim_scope"],
                "allocators": summary["allocators"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
