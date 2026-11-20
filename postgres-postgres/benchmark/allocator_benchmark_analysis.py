#!/usr/bin/env python3
"""Audit retained campaign #323 canonical allocator benchmark evidence."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

import paired
import profile_manifest
import profile_smoke

CAMPAIGN = 323
OXIDE_BATCH_VERSION = "0.7.0"
OXIDE_BATCH_CHECKSUM = "cb884d99629b92fc6740a187a2ebb65350f8924a57f0c8103bad26192f17652e"
TARGET = "aarch64-unknown-linux-gnu"
ALLOCATORS = ("system", "jemalloc", "mimalloc", "snmalloc", "tcmalloc")
READERS = ("cursor", "paging")
CANONICAL_PARAMETERS = {
    "reader": "both",
    "rows": 1_000_000,
    "seed": 20260904,
    "chunk_size": 1000,
    "fetch_size": 500,
    "page_size": 750,
    "warmups": 2,
    "measured_runs": 7,
}
METRICS = (
    "elapsed_seconds",
    "rows_per_second",
    "user_cpu_seconds",
    "system_cpu_seconds",
    "max_rss_kib",
)


def fail(message: str) -> None:
    raise RuntimeError(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read JSON {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected JSON object: {path}")
    return value


def prepare_output_root(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        fail(f"output directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def metric_statistics(
    samples: list[dict[str, Any]],
    metric: str,
) -> dict[str, Any]:
    if not samples:
        raise ValueError("cannot analyze empty sample set")
    ordered = sorted(float(sample[metric]) for sample in samples)
    median = statistics.median(ordered)
    q1 = paired.percentile(ordered, 0.25)
    q3 = paired.percentile(ordered, 0.75)
    iqr = q3 - q1
    mad = statistics.median(abs(value - median) for value in ordered)
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    outliers = []
    for sample in sorted(samples, key=lambda item: int(item["repetition"])):
        value = float(sample[metric])
        if value < lower or value > upper:
            outliers.append(
                {
                    "repetition": int(sample["repetition"]),
                    "order_position": int(sample["order_position"]),
                    "value": value,
                    "direction": "low" if value < lower else "high",
                }
            )
    return {
        "min": min(ordered),
        "q1": q1,
        "median": median,
        "q3": q3,
        "max": max(ordered),
        "p95": paired.percentile(ordered, 0.95),
        "iqr": iqr,
        "mad": mad,
        "tukey_1_5_iqr_lower_fence": lower,
        "tukey_1_5_iqr_upper_fence": upper,
        "outlier_count": len(outliers),
        "outliers": outliers,
    }


def validate_summary(summary: dict[str, Any], expected_source_sha: str) -> str:
    if summary.get("schema") != "oxide-batch-allocator-benchmark-summary/v1":
        fail(f"unexpected summary schema: {summary.get('schema')!r}")
    if summary.get("campaign") != CAMPAIGN:
        fail(f"unexpected campaign: {summary.get('campaign')!r}")
    if summary.get("canonical_geometry") is not True:
        fail("source evidence is not canonical geometry")
    if summary.get("claim_scope") != "same-host-relative-observational":
        fail(f"unexpected claim scope: {summary.get('claim_scope')!r}")
    if summary.get("parameters") != CANONICAL_PARAMETERS:
        fail(f"canonical parameter drift: {summary.get('parameters')!r}")
    if set(summary.get("allocators", [])) != set(ALLOCATORS):
        fail(f"allocator set drift: {summary.get('allocators')!r}")

    subject = summary.get("subject")
    if not isinstance(subject, dict):
        fail("summary subject is missing")
    if subject.get("workload_sha") != expected_source_sha:
        fail(
            "source workload SHA mismatch: "
            f"{subject.get('workload_sha')!r} != {expected_source_sha!r}"
        )
    if subject.get("target") != TARGET:
        fail(f"unexpected target: {subject.get('target')!r}")
    oxide = subject.get("oxide_batch")
    if not isinstance(oxide, dict):
        fail("summary oxide-batch subject is missing")
    if oxide.get("version") != OXIDE_BATCH_VERSION:
        fail(f"unexpected OxideBatch version: {oxide.get('version')!r}")
    if oxide.get("checksum") != OXIDE_BATCH_CHECKSUM:
        fail(f"unexpected OxideBatch checksum: {oxide.get('checksum')!r}")

    dataset_identity = summary.get("dataset_identity")
    if not isinstance(dataset_identity, str) or len(dataset_identity) != 64:
        fail(f"invalid dataset identity: {dataset_identity!r}")
    return dataset_identity


def expected_order_positions(summary: dict[str, Any]) -> dict[tuple[str, int, str], int]:
    measured_orders = summary.get("orders", {}).get("measured")
    if not isinstance(measured_orders, list) or len(measured_orders) != 14:
        fail("canonical summary must retain 14 measured order records")
    positions: dict[tuple[str, int, str], int] = {}
    per_reader = {reader: set() for reader in READERS}
    for entry in measured_orders:
        if not isinstance(entry, dict):
            fail("measured order entry must be an object")
        reader = entry.get("reader")
        repetition = entry.get("repetition")
        order = entry.get("order")
        if reader not in READERS or not isinstance(repetition, int):
            fail(f"invalid measured order identity: {entry!r}")
        if not isinstance(order, list) or set(order) != set(ALLOCATORS) or len(order) != 5:
            fail(f"invalid allocator order: {entry!r}")
        if repetition < 1 or repetition > 7 or repetition in per_reader[reader]:
            fail(f"duplicate/out-of-range measured order repetition: {entry!r}")
        per_reader[reader].add(repetition)
        for position, allocator in enumerate(order, start=1):
            positions[(reader, repetition, allocator)] = position
    for reader in READERS:
        if per_reader[reader] != set(range(1, 8)):
            fail(f"incomplete measured order repetitions for {reader}")
    return positions


def validate_warmup_correctness(summary: dict[str, Any], dataset_identity: str) -> int:
    rows = summary.get("warmup_correctness")
    if not isinstance(rows, list) or len(rows) != 20:
        fail("canonical summary must retain 20 warmup correctness records")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            fail("warmup correctness row must be an object")
        key = (row.get("reader"), row.get("repetition"), row.get("allocator"))
        if key in seen:
            fail(f"duplicate warmup correctness identity: {key!r}")
        seen.add(key)
        if row.get("reader") not in READERS:
            fail(f"invalid warmup reader: {row!r}")
        if row.get("allocator") not in ALLOCATORS:
            fail(f"invalid warmup allocator: {row!r}")
        if row.get("repetition") not in {1, 2}:
            fail(f"invalid warmup repetition: {row!r}")
        if row.get("verdict") != "PASS":
            fail(f"warmup correctness did not pass: {row!r}")
        if row.get("source_digest") != dataset_identity:
            fail(f"warmup dataset identity drift: {row!r}")
    return len(rows)


def audit_sample(
    *,
    manifest_path: Path,
    evidence_root: Path,
    expected_source_sha: str,
    dataset_identity: str,
    order_positions: dict[tuple[str, int, str], int],
) -> tuple[str, str, dict[str, Any], int]:
    relative = manifest_path.relative_to(evidence_root)
    if len(relative.parts) != 4:
        fail(f"unexpected measured manifest path: {relative}")
    allocator, reader, measured_dir, filename = relative.parts
    if filename != "manifest.json" or allocator not in ALLOCATORS or reader not in READERS:
        fail(f"unexpected measured manifest path: {relative}")
    if not measured_dir.startswith("measured-"):
        fail(f"unexpected measured directory: {relative}")
    try:
        repetition = int(measured_dir.split("-", 1)[1])
    except ValueError:
        fail(f"invalid measured repetition path: {relative}")
    if repetition < 1 or repetition > 7:
        fail(f"measured repetition out of range: {relative}")

    manifest = load_json(manifest_path)
    profile_manifest.validate_manifest(manifest)
    if manifest.get("campaign") != CAMPAIGN or manifest.get("lane") != "benchmark":
        fail(f"manifest campaign/lane mismatch: {relative}")

    run = manifest["run"]
    if run.get("repetition") != repetition:
        fail(f"manifest repetition mismatch: {relative}")

    subject = manifest["subject"]
    if subject.get("workload_sha") != expected_source_sha:
        fail(f"manifest source SHA mismatch: {relative}")
    if subject.get("target") != TARGET:
        fail(f"manifest target mismatch: {relative}")
    oxide = subject["oxide_batch"]
    if (
        oxide.get("version") != OXIDE_BATCH_VERSION
        or oxide.get("checksum") != OXIDE_BATCH_CHECKSUM
    ):
        fail(f"manifest OxideBatch subject mismatch: {relative}")

    if manifest["allocator"].get("identity") != allocator:
        fail(f"manifest allocator mismatch: {relative}")
    workload = manifest["workload"]
    if workload.get("reader_mode") != reader:
        fail(f"manifest reader mismatch: {relative}")
    if workload.get("rows") != CANONICAL_PARAMETERS["rows"]:
        fail(f"manifest row geometry mismatch: {relative}")
    if workload.get("seed") != CANONICAL_PARAMETERS["seed"]:
        fail(f"manifest seed mismatch: {relative}")
    if workload.get("chunk_size") != CANONICAL_PARAMETERS["chunk_size"]:
        fail(f"manifest chunk geometry mismatch: {relative}")
    if reader == "cursor":
        if workload.get("fetch_size") != CANONICAL_PARAMETERS["fetch_size"]:
            fail(f"manifest fetch geometry mismatch: {relative}")
        if workload.get("page_size") is not None:
            fail(f"cursor manifest unexpectedly carries page size: {relative}")
    else:
        if workload.get("page_size") != CANONICAL_PARAMETERS["page_size"]:
            fail(f"manifest page geometry mismatch: {relative}")
        if workload.get("fetch_size") is not None:
            fail(f"paging manifest unexpectedly carries fetch size: {relative}")
    if workload.get("dataset_identity") != dataset_identity:
        fail(f"manifest dataset identity mismatch: {relative}")

    correctness = manifest["correctness"]
    if correctness.get("verdict") != "PASS":
        fail(f"manifest correctness did not pass: {relative}")
    if correctness.get("expected_digest_sha256") != correctness.get("actual_digest_sha256"):
        fail(f"manifest correctness digest mismatch: {relative}")

    sample_root = manifest_path.parent
    artifact_count = 0
    for artifact in manifest["artifacts"]:
        artifact_path = sample_root / artifact["path"]
        if not artifact_path.is_file():
            fail(f"missing retained artifact: {artifact_path}")
        actual_sha = profile_smoke.sha256_file(artifact_path)
        if actual_sha != artifact["sha256"]:
            fail(f"artifact SHA mismatch: {artifact_path}")
        artifact_count += 1

    measurement_path = sample_root / "benchmark" / "measurement.json"
    verifier_path = sample_root / "benchmark" / "verifier.json"
    measurement = load_json(measurement_path)
    verifier = paired.parse_verify_report(
        verifier_path.read_text(encoding="utf-8"),
        CANONICAL_PARAMETERS["rows"],
    )

    if verifier.get("source_digest") != dataset_identity:
        fail(f"verifier dataset identity mismatch: {relative}")
    if verifier.get("expected_digest_sha256") != correctness.get("expected_digest_sha256"):
        fail(f"verifier/manifest expected digest mismatch: {relative}")
    if verifier.get("actual_digest_sha256") != correctness.get("actual_digest_sha256"):
        fail(f"verifier/manifest actual digest mismatch: {relative}")

    expected_position = order_positions[(reader, repetition, allocator)]
    expected_measurement = {
        "allocator": allocator,
        "reader_mode": reader,
        "phase": "measured",
        "repetition": repetition,
        "order_position": expected_position,
        "rows": CANONICAL_PARAMETERS["rows"],
        "seed": CANONICAL_PARAMETERS["seed"],
        "chunk_size": CANONICAL_PARAMETERS["chunk_size"],
        "fetch_size": CANONICAL_PARAMETERS["fetch_size"] if reader == "cursor" else None,
        "page_size": CANONICAL_PARAMETERS["page_size"] if reader == "paging" else None,
        "exit_status": 0,
    }
    for key, expected in expected_measurement.items():
        if measurement.get(key) != expected:
            fail(
                f"measurement {key} mismatch for {relative}: "
                f"{measurement.get(key)!r} != {expected!r}"
            )

    for metric in METRICS:
        value = measurement.get(metric)
        if not isinstance(value, (int, float)):
            fail(f"measurement {metric} is not numeric: {relative}")
        if metric in {"elapsed_seconds", "rows_per_second", "max_rss_kib"} and value <= 0:
            fail(f"measurement {metric} must be positive: {relative}")
        if metric in {"user_cpu_seconds", "system_cpu_seconds"} and value < 0:
            fail(f"measurement {metric} must be non-negative: {relative}")

    timing = manifest["benchmark_timing"]
    if float(timing["elapsed_seconds"]) != float(measurement["elapsed_seconds"]):
        fail(f"manifest/measurement elapsed mismatch: {relative}")
    if float(timing["rows_per_second"]) != float(measurement["rows_per_second"]):
        fail(f"manifest/measurement throughput mismatch: {relative}")

    return reader, allocator, measurement, artifact_count


def analyze(
    evidence_root: Path,
    expected_source_sha: str,
    output_root: Path,
) -> dict[str, Any]:
    summary_path = evidence_root / "summary.json"
    if not summary_path.is_file():
        fail(f"missing source summary: {summary_path}")
    summary = load_json(summary_path)
    dataset_identity = validate_summary(summary, expected_source_sha)
    order_positions = expected_order_positions(summary)
    warmup_count = validate_warmup_correctness(summary, dataset_identity)

    manifests = sorted(evidence_root.glob("*/*/measured-*/manifest.json"))
    if len(manifests) != 70:
        fail(f"expected 70 measured manifests, observed {len(manifests)}")

    samples: dict[str, dict[str, list[dict[str, Any]]]] = {
        reader: {allocator: [] for allocator in ALLOCATORS}
        for reader in READERS
    }
    artifact_count = 0
    seen = set()
    for manifest_path in manifests:
        reader, allocator, measurement, retained = audit_sample(
            manifest_path=manifest_path,
            evidence_root=evidence_root,
            expected_source_sha=expected_source_sha,
            dataset_identity=dataset_identity,
            order_positions=order_positions,
        )
        key = (reader, allocator, int(measurement["repetition"]))
        if key in seen:
            fail(f"duplicate measured sample identity: {key!r}")
        seen.add(key)
        samples[reader][allocator].append(measurement)
        artifact_count += retained

    statistics_by_reader: dict[str, Any] = {}
    total_outliers = 0
    for reader in READERS:
        statistics_by_reader[reader] = {}
        for allocator in ALLOCATORS:
            allocator_samples = sorted(
                samples[reader][allocator],
                key=lambda item: int(item["repetition"]),
            )
            if len(allocator_samples) != 7:
                fail(
                    f"expected 7 measured samples for {reader}/{allocator}, "
                    f"observed {len(allocator_samples)}"
                )
            summary_count = (
                summary.get("readers", {})
                .get(reader, {})
                .get(allocator, {})
                .get("sample_count")
            )
            if summary_count != 7:
                fail(f"source summary sample count mismatch for {reader}/{allocator}")
            metrics = {
                metric: metric_statistics(allocator_samples, metric)
                for metric in METRICS
            }
            total_outliers += sum(
                metric_summary["outlier_count"]
                for metric_summary in metrics.values()
            )
            statistics_by_reader[reader][allocator] = {
                "sample_count": 7,
                "samples": [
                    {
                        "repetition": int(sample["repetition"]),
                        "order_position": int(sample["order_position"]),
                        **{metric: sample[metric] for metric in METRICS},
                    }
                    for sample in allocator_samples
                ],
                "metrics": metrics,
            }

    prepare_output_root(output_root)
    analysis = {
        "schema": "oxide-batch-allocator-canonical-analysis/v1",
        "campaign": CAMPAIGN,
        "claim_scope": "canonical-distribution-and-outlier-audit",
        "source": {
            "evidence_path": str(evidence_root.resolve()),
            "repository_sha": expected_source_sha,
            "summary_sha256": profile_smoke.sha256_file(summary_path),
            "dataset_identity": dataset_identity,
            "subject": summary["subject"],
            "parameters": summary["parameters"],
        },
        "analyzer": {
            "repository_sha": profile_smoke.workload_sha(),
            "outlier_policy": "Tukey 1.5*IQR; outliers are flagged and never dropped",
            "dispersion": ["IQR", "MAD"],
        },
        "validation": {
            "measured_manifest_count": len(manifests),
            "measured_correctness_pass_count": len(manifests),
            "warmup_correctness_pass_count": warmup_count,
            "retained_artifact_hashes_verified": artifact_count,
            "all_expected_samples_present": len(seen) == 70,
        },
        "readers": statistics_by_reader,
        "outlier_observation_count_across_metrics": total_outliers,
        "limitations": [
            "outlier flags are descriptive and no sample is removed from any distribution",
            "cursor and paging distributions remain separate",
            "canonical timing is same-host relative evidence and absolute values remain host-specific",
            "this analysis does not select a default allocator",
            "permanent allocator recommendation remains blocked on the separate memory/quiescence phase",
        ],
    }
    profile_smoke.write_json(output_root / "analysis.json", analysis)
    return analysis


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not profile_smoke.SHA40.fullmatch(args.expected_source_sha):
        fail("expected source SHA must be a lowercase 40-character SHA")
    analysis = analyze(args.evidence, args.expected_source_sha, args.output)
    print(
        json.dumps(
            {
                "valid": True,
                "schema": analysis["schema"],
                "campaign": analysis["campaign"],
                "measured_manifest_count": analysis["validation"]["measured_manifest_count"],
                "outlier_observation_count_across_metrics": analysis[
                    "outlier_observation_count_across_metrics"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
