#!/usr/bin/env python3
"""Run one campaign #266 CPU-attribution profile with gperftools libprofiler."""

from __future__ import annotations

import argparse
import json
import os
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
PROFILER_PACKAGES = ("google-perftools", "libgoogle-perftools-dev")


def fail(message: str) -> None:
    raise RuntimeError(message)


def package_versions() -> dict[str, str]:
    command = [
        "dpkg-query",
        "-W",
        "-f=${Package}=${Version}\n",
        *PROFILER_PACKAGES,
    ]
    completed = profile_smoke.run_checked(command)
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
        fail(f"missing gperftools package versions: {missing}")
    return versions


def find_libprofiler() -> Path:
    completed = profile_smoke.run_checked(["ldconfig", "-p"])
    candidates: list[Path] = []
    for line in completed.stdout.splitlines():
        if "libprofiler.so" not in line or "=>" not in line:
            continue
        path = Path(line.rsplit("=>", 1)[1].strip())
        if path.is_file():
            candidates.append(path.resolve())
    if not candidates:
        fail("ldconfig did not expose an installed libprofiler.so")
    candidates.sort(key=lambda path: (".so." in path.name, len(str(path)), str(path)))
    return candidates[0]


def find_google_pprof() -> str:
    path = shutil.which("google-pprof")
    if path is None:
        fail("google-pprof is unavailable; install the google-perftools package")
    return path


def dynamic_dependencies(library: Path) -> list[str]:
    completed = profile_smoke.run_checked(["ldd", str(library)])
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        fail(f"ldd returned no dependencies for {library}")
    return lines


def validate_allocator_boundary(dependencies: list[str]) -> None:
    offenders = [line for line in dependencies if "tcmalloc" in line.lower()]
    if offenders:
        fail(
            "libprofiler dynamically depends on tcmalloc and would alter allocator semantics: "
            + "; ".join(offenders)
        )


def capability_document() -> dict[str, Any]:
    versions = package_versions()
    libprofiler = find_libprofiler()
    pprof = find_google_pprof()
    dependencies = dynamic_dependencies(libprofiler)
    validate_allocator_boundary(dependencies)
    return {
        "schema": "oxide-batch-gperftools-cpu-capability/v1",
        "profiler": "gperftools-libprofiler",
        "packages": versions,
        "libprofiler_path": str(libprofiler),
        "libprofiler_sha256": profile_smoke.sha256_file(libprofiler),
        "dynamic_dependencies": dependencies,
        "allocator_semantics_preserved": True,
        "pprof_path": pprof,
        "pprof_package_version": versions["google-perftools"],
        "sampling_clock": "ITIMER_PROF/SIGPROF",
        "supported": True,
    }


def profiled_environment(
    *,
    base: dict[str, str],
    database_url: str,
    libprofiler: Path,
    profile_path: Path,
    frequency_hz: int,
) -> dict[str, str]:
    if base.get("LD_PRELOAD"):
        fail("refusing to overwrite a pre-existing LD_PRELOAD")
    env = base.copy()
    env["DATABASE_URL"] = database_url
    env["LD_PRELOAD"] = str(libprofiler)
    env["CPUPROFILE"] = str(profile_path)
    env["CPUPROFILE_FREQUENCY"] = str(frequency_hz)
    return env


def projection_commands(
    *,
    pprof: str,
    binary: Path,
    profile: Path,
) -> tuple[list[str], list[str]]:
    return (
        [pprof, "--text", str(binary), str(profile)],
        [pprof, "--callgrind", str(binary), str(profile)],
    )


def profiler_limitations() -> list[str]:
    return [
        "CPU profiler runtime is attribution evidence only and must not be compared with benchmark-lane timing",
        "gperftools libprofiler uses ITIMER_PROF/SIGPROF on-CPU sampling and can underrepresent database, network, lock, sleep, and other off-CPU wait time",
        "libprofiler is injected with LD_PRELOAD; the producer rejects any dynamic libtcmalloc dependency so the selected allocator is not silently replaced",
        "hosted-runner attribution validates orchestration and profile readability, not stable-host absolute performance",
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
    package_version: str,
) -> dict[str, Any]:
    manifest = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": "cpu",
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
            "profiler_attached": True,
            "tools": [
                {
                    "name": "gperftools-libprofiler",
                    "version": package_version,
                    "role": "CPU sampling profiler (ITIMER_PROF/SIGPROF)",
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
        "limitations": profiler_limitations(),
    }
    profile_manifest.validate_manifest(manifest)
    return manifest


def run_profile(args: argparse.Namespace) -> dict[str, Any]:
    cpu_profile.validate_bounds(args)
    profile_smoke.prepare_output_root(args.output)

    env = os.environ.copy()
    env["RUST_LOG"] = "error"

    capability = capability_document()
    environment, rustc_version, cargo_version, target = profile_smoke.host_environment()
    cpu_profile.qualified_allocator(args.allocator, target)
    binary = cpu_profile.build_variant(args.allocator, target)

    subject_sha = profile_smoke.workload_sha()
    lock_path = Path("Cargo.lock")
    cargo_lock_sha256 = profile_smoke.sha256_file(lock_path)
    oxide_subject = paired.lock_package(lock_path, "oxide-batch", "0.6.0")
    profile_smoke.validate_allocator_lock(lock_path, target)

    token = profile_smoke.run_token()
    template_database = profile_smoke.database_name(token, "gperf_template")
    sample_database = profile_smoke.database_name(token, f"gperf_{args.allocator}")
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
        import_value = cpu_profile.cpu_import_name(args.allocator, args.reader)

        cpu_root = args.output / "cpu"
        correctness_root = args.output / "correctness"
        binary_root = args.output / "binary"
        cpu_root.mkdir(parents=True, exist_ok=True)
        correctness_root.mkdir(parents=True, exist_ok=True)
        binary_root.mkdir(parents=True, exist_ok=True)

        retained_binary = binary_root / f"postgres-postgres-{args.allocator}"
        shutil.copy2(binary, retained_binary)
        retained_binary.chmod(0o755)

        profile_path = cpu_root / "gperftools.prof"
        stdout_path = cpu_root / "workload.stdout.txt"
        stderr_path = cpu_root / "workload.stderr.txt"
        report_path = cpu_root / "pprof-text.txt"
        callgrind_path = cpu_root / "pprof-callgrind.txt"

        libprofiler = Path(capability["libprofiler_path"])
        profiled_env = profiled_environment(
            base=env,
            database_url=database_url,
            libprofiler=libprofiler,
            profile_path=profile_path,
            frequency_hz=args.frequency_hz,
        )
        workload = cpu_profile.workload_command(
            binary=retained_binary,
            import_value=import_value,
            reader=args.reader,
            chunk_size=args.chunk_size,
            fetch_size=args.fetch_size,
            page_size=args.page_size,
        )
        completed = subprocess.run(
            workload,
            check=False,
            text=True,
            capture_output=True,
            env=profiled_env,
        )
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            fail(
                f"profiled workload exited {completed.returncode}: "
                f"{completed.stderr[-2000:]}"
            )
        if not profile_path.is_file() or profile_path.stat().st_size <= 0:
            fail("gperftools produced no non-empty CPU profile")

        text_command, callgrind_command = projection_commands(
            pprof=capability["pprof_path"],
            binary=retained_binary,
            profile=profile_path,
        )
        text_report = subprocess.run(
            text_command,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        if text_report.returncode != 0:
            fail(
                f"google-pprof --text exited {text_report.returncode}: "
                f"{text_report.stderr[-2000:]}"
            )
        report_path.write_text(text_report.stdout, encoding="utf-8")
        if not text_report.stdout.strip():
            fail("google-pprof text projection was empty")

        callgrind = subprocess.run(
            callgrind_command,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        if callgrind.returncode != 0:
            fail(
                f"google-pprof --callgrind exited {callgrind.returncode}: "
                f"{callgrind.stderr[-2000:]}"
            )
        callgrind_path.write_text(callgrind.stdout, encoding="utf-8")
        if not callgrind.stdout.strip():
            fail("google-pprof callgrind projection was empty")

        verifier_report, verifier_stdout = profile_smoke.run_verifier(
            binary=retained_binary,
            database_url=database_url,
            import_value=import_value,
            rows=args.rows,
            env=env,
        )
        verifier_path = correctness_root / "verifier.json"
        verifier_path.write_text(verifier_stdout, encoding="utf-8")

        run_metadata = {
            "schema": "oxide-batch-gperftools-cpu-profile-run/v1",
            "profiler": {
                "name": "gperftools-libprofiler",
                "packages": capability["packages"],
                "libprofiler_path": capability["libprofiler_path"],
                "libprofiler_sha256": capability["libprofiler_sha256"],
                "dynamic_dependencies": capability["dynamic_dependencies"],
                "pprof_package_version": capability["pprof_package_version"],
            },
            "sampling_clock": capability["sampling_clock"],
            "sample_frequency_hz": args.frequency_hz,
            "allocator": args.allocator,
            "reader_mode": args.reader,
            "rows": args.rows,
            "seed": args.seed,
            "child_exit_status": completed.returncode,
            "profiled_binary_sha256": profile_smoke.sha256_file(retained_binary),
            "timing_claim": "attribution-only",
            "perf_events_required": False,
        }
        metadata_path = cpu_root / "run.json"
        profile_smoke.write_json(metadata_path, run_metadata)

        capability_path = cpu_root / "capability.json"
        profile_smoke.write_json(capability_path, capability)

        artifacts = [
            profile_smoke.artifact_record(args.output, profile_path),
            profile_smoke.artifact_record(args.output, stdout_path),
            profile_smoke.artifact_record(args.output, stderr_path),
            {
                **profile_smoke.artifact_record(args.output, report_path),
                "kind": "projection",
            },
            {
                **profile_smoke.artifact_record(args.output, callgrind_path),
                "kind": "projection",
            },
            profile_smoke.artifact_record(args.output, metadata_path),
            profile_smoke.artifact_record(args.output, capability_path),
            profile_smoke.artifact_record(args.output, verifier_path),
            profile_smoke.artifact_record(args.output, retained_binary),
        ]
        package_version = capability["packages"]["libgoogle-perftools-dev"]
        manifest = build_manifest(
            allocator=args.allocator,
            run_id=f"cpu-gperftools-{token}-{args.allocator}-{args.reader}",
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
            package_version=package_version,
        )
        profile_smoke.write_json(args.output / "manifest.json", manifest)
        return manifest
    finally:
        paired.compose_db("dropdb", sample_database)
        paired.compose_db("dropdb", template_database)


def write_probe(path: Path) -> int:
    document = capability_document()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(document, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument("--base-database-url")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allocator",
        choices=tuple(
            candidate["identity"]
            for candidate in allocator_registry.active_candidates(ALLOCATOR_REGISTRY)
        ),
        default="system",
    )
    parser.add_argument("--reader", choices=("cursor", "paging"), default="paging")
    parser.add_argument("--rows", type=int, default=cpu_profile.DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=cpu_profile.DEFAULT_SEED)
    parser.add_argument("--chunk-size", type=int, default=cpu_profile.DEFAULT_CHUNK_SIZE)
    parser.add_argument("--fetch-size", type=int, default=cpu_profile.DEFAULT_FETCH_SIZE)
    parser.add_argument("--page-size", type=int, default=cpu_profile.DEFAULT_PAGE_SIZE)
    parser.add_argument("--frequency-hz", type=int, default=cpu_profile.DEFAULT_FREQUENCY_HZ)
    args = parser.parse_args()

    if args.probe_output is not None:
        if args.base_database_url is not None or args.output is not None:
            parser.error("--probe-output cannot be combined with profile execution arguments")
        return write_probe(args.probe_output)

    if args.base_database_url is None or args.output is None:
        parser.error("--base-database-url and --output are required for profile execution")

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
