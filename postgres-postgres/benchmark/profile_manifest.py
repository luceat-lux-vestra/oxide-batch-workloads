#!/usr/bin/env python3
"""Validate campaign #266 profile-artifact manifests."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Any

import allocator_registry

SCHEMA_V1 = "oxide-batch-profile-manifest/v1"
SCHEMA_V2 = "oxide-batch-profile-manifest/v2"
SCHEMA = SCHEMA_V2
SUPPORTED_SCHEMAS = {SCHEMA_V1, SCHEMA_V2}
CAMPAIGN = 266
OXIDE_BATCH_VERSION = "0.6.0"
CRATES_IO_SOURCES = {
    "registry+https://github.com/rust-lang/crates.io-index",
    "sparse+https://index.crates.io/",
}
LANES = {"benchmark", "cpu", "heap", "timeline", "allocator", "os-process"}
ALLOCATOR_REGISTRY = allocator_registry.load_registry()
ALLOCATORS = {
    candidate["identity"]
    for candidate in allocator_registry.active_candidates(ALLOCATOR_REGISTRY)
}
READERS = {"cursor", "paging"}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """Raised when a profile manifest violates the campaign contract."""


def fail(message: str) -> None:
    raise ManifestError(message)


def require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{field} must be an object")
    return value


def require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        fail(f"{field} must be an array")
    return value


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(f"{field} must be a non-empty string")
    return value


def require_positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        fail(f"{field} must be a positive integer")
    return value


def require_sha(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    text = require_string(value, field)
    if not pattern.fullmatch(text):
        fail(f"{field} must be lowercase hexadecimal with the required length")
    return text


def validate_subject(subject: dict[str, Any], schema: str) -> None:
    require_sha(subject.get("workload_sha"), "subject.workload_sha", SHA40)
    require_sha(subject.get("cargo_lock_sha256"), "subject.cargo_lock_sha256", SHA64)
    require_string(subject.get("rustc"), "subject.rustc")
    if schema == SCHEMA_V2:
        require_string(subject.get("cargo"), "subject.cargo")
    require_string(subject.get("target"), "subject.target")
    if subject.get("build_profile") != "release":
        fail("subject.build_profile must be 'release' for profile evidence")

    oxide = require_dict(subject.get("oxide_batch"), "subject.oxide_batch")
    if oxide.get("name") != "oxide-batch":
        fail("subject.oxide_batch.name must be 'oxide-batch'")
    if oxide.get("version") != OXIDE_BATCH_VERSION:
        fail(f"subject.oxide_batch.version must be '{OXIDE_BATCH_VERSION}'")
    source = require_string(oxide.get("source"), "subject.oxide_batch.source")
    if source not in CRATES_IO_SOURCES:
        fail("subject.oxide_batch.source must identify canonical crates.io provenance")
    require_sha(oxide.get("checksum"), "subject.oxide_batch.checksum", SHA64)


def validate_allocator(allocator: dict[str, Any], target: str | None = None) -> None:
    identity = allocator.get("identity")
    if identity not in ALLOCATORS:
        fail(f"allocator.identity must be one of {sorted(ALLOCATORS)}")

    candidate = allocator_registry.candidate_by_id(ALLOCATOR_REGISTRY, identity)
    if target is not None:
        state = allocator_registry.target_state(candidate, target)
        if state != "qualified":
            fail(
                f"allocator {identity} is not qualified for target {target}: {state}"
            )

    wrapper = allocator.get("wrapper")
    expected_wrapper = candidate["wrapper"]
    if expected_wrapper is None:
        if wrapper is not None:
            fail("allocator.wrapper must be null for the system allocator")
    else:
        wrapper_obj = require_dict(wrapper, "allocator.wrapper")
        name = require_string(wrapper_obj.get("name"), "allocator.wrapper.name")
        version = require_string(wrapper_obj.get("version"), "allocator.wrapper.version")
        if (name, version) != (
            expected_wrapper["name"],
            expected_wrapper["version"],
        ):
            fail(
                f"allocator {identity} must use pinned wrapper "
                f"{expected_wrapper['name']} {expected_wrapper['version']}"
            )

    upstream = require_dict(allocator.get("upstream"), "allocator.upstream")
    expected_upstream = candidate["upstream"]
    status = upstream.get("status")
    if status != expected_upstream["status"]:
        fail(
            f"allocator {identity} upstream status must be "
            f"{expected_upstream['status']}"
        )

    if status == "observed":
        version = require_string(upstream.get("version"), "allocator.upstream.version")
        if version != expected_upstream["version"]:
            fail(
                f"allocator {identity} upstream version must be "
                f"{expected_upstream['version']}"
            )
        if upstream.get("reason") is not None:
            fail("allocator.upstream.reason must be absent when status is observed")
    else:
        if upstream.get("version") is not None:
            fail("allocator.upstream.version must be null when status is unavailable")
        require_string(upstream.get("reason"), "allocator.upstream.reason")


def validate_environment(environment: dict[str, Any]) -> None:
    for field in (
        "os",
        "kernel",
        "arch",
        "cpu_model",
        "postgres_configured_image",
        "postgres_server_version",
    ):
        require_string(environment.get(field), f"environment.{field}")
    require_positive_int(environment.get("logical_cpu_count"), "environment.logical_cpu_count")
    require_positive_int(environment.get("memory_total_bytes"), "environment.memory_total_bytes")


def validate_workload(workload: dict[str, Any]) -> None:
    reader = workload.get("reader_mode")
    if reader not in READERS:
        fail(f"workload.reader_mode must be one of {sorted(READERS)}")
    require_positive_int(workload.get("rows"), "workload.rows")
    seed = workload.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        fail("workload.seed must be an integer")
    require_positive_int(workload.get("chunk_size"), "workload.chunk_size")
    require_string(workload.get("dataset_identity"), "workload.dataset_identity")

    fetch_size = workload.get("fetch_size")
    page_size = workload.get("page_size")
    if reader == "cursor":
        require_positive_int(fetch_size, "workload.fetch_size")
        if page_size is not None:
            fail("workload.page_size must be null for cursor runs")
    else:
        require_positive_int(page_size, "workload.page_size")
        if fetch_size is not None:
            fail("workload.fetch_size must be null for paging runs")


def validate_instrumentation(instrumentation: dict[str, Any], lane: str) -> None:
    profiler_attached = instrumentation.get("profiler_attached")
    if not isinstance(profiler_attached, bool):
        fail("instrumentation.profiler_attached must be boolean")

    tools = require_list(instrumentation.get("tools"), "instrumentation.tools")
    for index, tool in enumerate(tools):
        tool_obj = require_dict(tool, f"instrumentation.tools[{index}]")
        require_string(tool_obj.get("name"), f"instrumentation.tools[{index}].name")
        require_string(tool_obj.get("version"), f"instrumentation.tools[{index}].version")
        require_string(tool_obj.get("role"), f"instrumentation.tools[{index}].role")

    if lane == "benchmark" and profiler_attached:
        fail("benchmark lane must not attach a profiler")
    if lane in {"cpu", "heap"}:
        if not profiler_attached:
            fail(f"{lane} lane requires profiler attachment")
        if not tools:
            fail(f"{lane} lane requires at least one profiler tool")


def validate_artifacts(artifacts: list[Any]) -> None:
    if not artifacts:
        fail("artifacts must retain at least one raw evidence artifact")

    seen: set[str] = set()
    has_raw = False
    for index, artifact in enumerate(artifacts):
        item = require_dict(artifact, f"artifacts[{index}]")
        path = require_string(item.get("path"), f"artifacts[{index}].path")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or path == "manifest.json":
            fail(f"artifacts[{index}].path must be a relative raw-artifact path")
        if path in seen:
            fail(f"duplicate artifact path: {path}")
        seen.add(path)
        require_sha(item.get("sha256"), f"artifacts[{index}].sha256", SHA64)
        kind = require_string(item.get("kind"), f"artifacts[{index}].kind")
        has_raw = has_raw or kind == "raw"

    if not has_raw:
        fail("artifacts must retain at least one artifact with kind 'raw'")


def validate_correctness(correctness: dict[str, Any]) -> None:
    if correctness.get("verdict") not in {"PASS", "FAIL"}:
        fail("correctness.verdict must be PASS or FAIL")
    require_string(correctness.get("verifier"), "correctness.verifier")
    expected = require_sha(
        correctness.get("expected_digest_sha256"),
        "correctness.expected_digest_sha256",
        SHA64,
    )
    actual = require_sha(
        correctness.get("actual_digest_sha256"),
        "correctness.actual_digest_sha256",
        SHA64,
    )
    if correctness.get("verdict") == "PASS" and expected != actual:
        fail("correctness PASS requires matching expected/actual digests")


def validate_manifest(document: Any) -> None:
    manifest = require_dict(document, "manifest")
    schema = manifest.get("schema")
    if schema not in SUPPORTED_SCHEMAS:
        fail(f"schema must be one of {sorted(SUPPORTED_SCHEMAS)}")
    if manifest.get("campaign") != CAMPAIGN:
        fail(f"campaign must be {CAMPAIGN}")

    lane = manifest.get("lane")
    if lane not in LANES:
        fail(f"lane must be one of {sorted(LANES)}")

    run = require_dict(manifest.get("run"), "run")
    require_string(run.get("id"), "run.id")
    require_positive_int(run.get("repetition"), "run.repetition")

    subject = require_dict(manifest.get("subject"), "subject")
    validate_subject(subject, schema)
    validate_allocator(
        require_dict(manifest.get("allocator"), "allocator"),
        require_string(subject.get("target"), "subject.target"),
    )
    validate_environment(require_dict(manifest.get("environment"), "environment"))
    validate_workload(require_dict(manifest.get("workload"), "workload"))
    validate_instrumentation(
        require_dict(manifest.get("instrumentation"), "instrumentation"),
        lane,
    )
    validate_artifacts(require_list(manifest.get("artifacts"), "artifacts"))
    validate_correctness(require_dict(manifest.get("correctness"), "correctness"))

    limitations = require_list(manifest.get("limitations"), "limitations")
    for index, limitation in enumerate(limitations):
        require_string(limitation, f"limitations[{index}]")

    has_benchmark_timing = "benchmark_timing" in manifest
    if lane == "benchmark":
        if not has_benchmark_timing:
            fail("benchmark lane requires benchmark_timing")
        timing = require_dict(manifest["benchmark_timing"], "benchmark_timing")
        elapsed = timing.get("elapsed_seconds")
        throughput = timing.get("rows_per_second")
        if (
            not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or not math.isfinite(elapsed)
            or elapsed <= 0
        ):
            fail("benchmark_timing.elapsed_seconds must be finite and positive")
        if (
            not isinstance(throughput, (int, float))
            or isinstance(throughput, bool)
            or not math.isfinite(throughput)
            or throughput <= 0
        ):
            fail("benchmark_timing.rows_per_second must be finite and positive")
    elif has_benchmark_timing:
        fail("profiler/telemetry lanes must not carry benchmark_timing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()

    try:
        document = json.loads(args.manifest.read_text(encoding="utf-8"))
        validate_manifest(document)
    except (OSError, json.JSONDecodeError, ManifestError) as exc:
        raise SystemExit(f"profile manifest invalid: {exc}") from exc

    summary = {
        "valid": True,
        "schema": document["schema"],
        "campaign": document["campaign"],
        "lane": document["lane"],
        "run_id": document["run"]["id"],
        "allocator": document["allocator"]["identity"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
