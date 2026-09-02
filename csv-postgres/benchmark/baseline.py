#!/usr/bin/env python3
"""Repeatable CSV -> PostgreSQL OxideBatch baseline campaign.

Times only the release binary's `run` command. Reset and verification remain
outside the timed interval and every measured run must verify successfully.
The output is observational evidence: this script intentionally contains no
performance threshold.
"""

from __future__ import annotations

import argparse
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
from typing import Any

TIME_FORMAT = "elapsed=%e\nuser=%U\nsystem=%S\nmax_rss_kib=%M\n"


def run_checked(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, env=env, capture_output=True)


def read_first_matching(path: Path, prefix: str) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def host_provenance() -> dict[str, Any]:
    cpu_model = read_first_matching(Path("/proc/cpuinfo"), "model name")
    mem_total = read_first_matching(Path("/proc/meminfo"), "MemTotal")

    def optional(command: list[str]) -> str | None:
        try:
            return run_checked(command).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    compose = ["docker", "compose"]
    postgres_version = optional(compose + ["exec", "-T", "postgres", "postgres", "--version"])
    image_id = optional(compose + ["images", "-q", "postgres"])

    return {
        "platform": platform.platform(),
        "uname": " ".join(platform.uname()),
        "logical_cpu_count": os.cpu_count(),
        "cpu_model": cpu_model,
        "memory_total": mem_total,
        "rustc": optional(["rustc", "--version"]),
        "cargo": optional(["cargo", "--version"]),
        "postgres": postgres_version,
        "postgres_image_id": image_id,
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


def parse_time_file(path: Path) -> dict[str, float | int]:
    values: dict[str, float | int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        if key == "max_rss_kib":
            values[key] = int(value)
        else:
            values[key] = float(value)
    expected = {"elapsed", "user", "system", "max_rss_kib"}
    if set(values) != expected:
        raise RuntimeError(f"unexpected /usr/bin/time output keys: {sorted(values)}")
    return values


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


def summarize(measured: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = sorted(float(run["elapsed_seconds"]) for run in measured)
    throughput = sorted(float(run["rows_per_second"]) for run in measured)
    rss = sorted(int(run["max_rss_kib"]) for run in measured)
    return {
        "measured_runs": len(measured),
        "elapsed_seconds": {
            "min": min(elapsed),
            "median": statistics.median(elapsed),
            "max": max(elapsed),
            "p95": percentile(elapsed, 0.95),
        },
        "rows_per_second": {
            "min": min(throughput),
            "median": statistics.median(throughput),
            "max": max(throughput),
            "p95": percentile(throughput, 0.95),
        },
        "max_rss_kib": {
            "min": min(rss),
            "median": statistics.median(rss),
            "max": max(rss),
        },
    }


def timed_import(
    *,
    binary: Path,
    database_url: str,
    input_path: Path,
    import_name: str,
    chunk_size: int,
    rows: int,
    env: dict[str, str],
) -> dict[str, Any]:
    run_checked([str(binary), "reset", "--database-url", database_url], env=env)
    with tempfile.NamedTemporaryFile(prefix="oxide-batch-time-", delete=False) as handle:
        time_path = Path(handle.name)
    try:
        command = [
            "/usr/bin/time",
            "-f",
            TIME_FORMAT,
            "-o",
            str(time_path),
            str(binary),
            "run",
            "--database-url",
            database_url,
            "--input",
            str(input_path),
            "--import-name",
            import_name,
            "--chunk-size",
            str(chunk_size),
        ]
        started_at = time.time()
        subprocess.run(command, check=True, text=True, env=env)
        finished_at = time.time()
        metrics = parse_time_file(time_path)
        run_checked(
            [str(binary), "verify", "--database-url", database_url, "--input", str(input_path)],
            env=env,
        )
    finally:
        time_path.unlink(missing_ok=True)

    elapsed = float(metrics["elapsed"])
    if elapsed <= 0.0:
        raise RuntimeError("timed import reported non-positive elapsed time")
    return {
        "import_name": import_name,
        "started_at_unix": started_at,
        "finished_at_unix": finished_at,
        "elapsed_seconds": elapsed,
        "user_cpu_seconds": float(metrics["user"]),
        "system_cpu_seconds": float(metrics["system"]),
        "max_rss_kib": int(metrics["max_rss_kib"]),
        "rows_per_second": rows / elapsed,
        "verification": "passed",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--chunk-size", type=int, required=True)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--measured-runs", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in ("rows", "chunk_size", "measured_runs"):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be > 0")
    if args.warmups < 0:
        raise SystemExit("--warmups must be >= 0")
    if not args.binary.is_file():
        raise SystemExit(f"release binary not found: {args.binary}")
    if not args.input.is_file():
        raise SystemExit(f"input not found: {args.input}")
    if not Path("/usr/bin/time").is_file():
        raise SystemExit("/usr/bin/time is required for resource measurements")


def main() -> int:
    args = parse_args()
    validate_args(args)

    manifest_path = args.input.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        raise SystemExit(f"input manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("rows", -1)) != args.rows or int(manifest.get("seed", -1)) != args.seed:
        raise SystemExit("input manifest rows/seed do not match benchmark configuration")

    env = os.environ.copy()
    env.setdefault("RUST_LOG", "error")

    report: dict[str, Any] = {
        "schema_version": 1,
        "campaign": "oxide-batch-csv-postgres-baseline",
        "claim_class": "observational-baseline",
        "performance_threshold": None,
        "configuration": {
            "rows": args.rows,
            "seed": args.seed,
            "chunk_size": args.chunk_size,
            "warmups": args.warmups,
            "measured_runs": args.measured_runs,
            "binary": str(args.binary),
            "input": str(args.input),
            "input_manifest": manifest,
        },
        "environment": host_provenance(),
        "warmups": [],
        "measured": [],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for index in range(args.warmups):
            result = timed_import(
                binary=args.binary,
                database_url=args.database_url,
                input_path=args.input,
                import_name=f"baseline-warmup-{index + 1}",
                chunk_size=args.chunk_size,
                rows=args.rows,
                env=env,
            )
            report["warmups"].append(result)

        for index in range(args.measured_runs):
            result = timed_import(
                binary=args.binary,
                database_url=args.database_url,
                input_path=args.input,
                import_name=f"baseline-measured-{index + 1}",
                chunk_size=args.chunk_size,
                rows=args.rows,
                env=env,
            )
            report["measured"].append(result)

        report["summary"] = summarize(report["measured"])
        report["status"] = "passed"
        return_code = 0
    except Exception as exc:  # preserve partial evidence before failing the job
        report["status"] = "failed"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        return_code = 1
    finally:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(report.get("summary", {}), indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
