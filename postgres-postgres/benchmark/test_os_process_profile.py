#!/usr/bin/env python3
"""Unit contracts for the campaign #266 OS/process telemetry producer."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import os_process_profile


class OsProcessProfileTests(unittest.TestCase):
    @staticmethod
    def sample(
        *,
        timestamp: int,
        role: str | None = "clean-workload",
        rss: int | None = 1024,
    ) -> dict[str, object]:
        process: dict[str, object] | None = None
        if role is not None:
            process = {
                "pid": 123,
                "role": role,
                "rss_bytes": rss,
                "pss_bytes": 900 if rss is not None else None,
                "smaps_rollup_rss_bytes": rss,
                "private_clean_bytes": 1,
                "private_dirty_bytes": 2,
                "shared_clean_bytes": 3,
                "shared_dirty_bytes": 4,
                "swap_bytes": 0,
                "minor_faults": 10,
                "major_faults": 0,
                "cpu_time_ticks": 20,
                "voluntary_context_switches": 5,
                "nonvoluntary_context_switches": 1,
                "thread_count": 2,
                "fd_count": 8,
                "unavailable_reason": None,
                "smaps_rollup_unavailable_reason": None,
                "fd_count_unavailable_reason": None,
            }
        return {
            "schema": os_process_profile.SAMPLE_SCHEMA,
            "schema_version": os_process_profile.SAMPLE_SCHEMA_VERSION,
            "timestamp_unix_nanos_utc": timestamp,
            "campaign_id": "lanef-fixture",
            "sequence": os_process_profile.SEQUENCE,
            "sample_kind": "periodic",
            "sample_interval_millis": 50,
            "process": process,
            "postgres": {
                "total_connections": 3,
                "active_connections": 1,
                "database_size_bytes": 1000,
            },
            "oxide_batch": {},
            "filesystem": {
                "total_bytes": 10_000,
                "used_bytes": 2_000,
                "available_bytes": 8_000,
            },
        }

    def test_summary_preserves_raw_timeline_as_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "samples.jsonl"
            records = [
                self.sample(timestamp=100),
                self.sample(timestamp=200, rss=2048),
                self.sample(timestamp=300, role=None),
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            summary = os_process_profile.summarize_samples(path, "lanef-fixture")

        self.assertEqual(summary["schema"], os_process_profile.SUMMARY_SCHEMA)
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["process_sample_count"], 2)
        self.assertEqual(summary["observed_span_nanos"], 200)
        rss = summary["roles"]["clean-workload"]["rss_bytes"]
        self.assertEqual(rss["observed_count"], 2)
        self.assertEqual(rss["min"], 1024)
        self.assertEqual(rss["max"], 2048)
        self.assertEqual(summary["postgres"]["active_connections"]["max"], 1)

    def test_summary_rejects_timestamp_regression(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "samples.jsonl"
            path.write_text(
                json.dumps(self.sample(timestamp=200))
                + "\n"
                + json.dumps(self.sample(timestamp=100))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "timestamps regress"):
                os_process_profile.summarize_samples(path, "lanef-fixture")

    def test_summary_requires_process_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "samples.jsonl"
            path.write_text(
                json.dumps(self.sample(timestamp=100, role=None)) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "no process observations"):
                os_process_profile.summarize_samples(path, "lanef-fixture")

    def test_verifier_report_reuses_existing_correctness_contract(self) -> None:
        digest = "a" * 64
        report = {
            "source_digest": "source-fixture",
            "source_rows": os_process_profile.ROWS,
            "destination_rows": os_process_profile.ROWS,
            "row_counts_match": True,
            "expected_digest_sha256": digest,
            "actual_digest_sha256": digest,
            "digests_match": True,
            "total_mismatches": 0,
        }
        validated = os_process_profile.validate_verifier_report(report)
        self.assertEqual(validated["source_digest"], "source-fixture")

    def test_manifest_passes_shared_validator(self) -> None:
        digest = "1" * 64
        manifest = os_process_profile.build_manifest(
            run_id="os-process-fixture-system-paging",
            subject_sha="a" * 40,
            cargo_lock_sha256="b" * 64,
            rustc_version="rustc 1.100.0 (fixture)",
            cargo_version="cargo 1.100.0 (fixture)",
            target="aarch64-unknown-linux-gnu",
            oxide_subject={
                "name": "oxide-batch",
                "version": "0.6.0",
                "source": "sparse+https://index.crates.io/",
                "checksum": "c" * 64,
            },
            environment={
                "os": "Linux",
                "kernel": "7.0.0",
                "arch": "aarch64",
                "cpu_model": "fixture",
                "logical_cpu_count": 4,
                "memory_total_bytes": 8 * 1024 * 1024 * 1024,
                "postgres_configured_image": "postgres:18",
                "postgres_server_version": "postgres (PostgreSQL) 18.7",
            },
            artifacts=[
                {
                    "path": "memory/endurance/lanef-fixture/samples.jsonl",
                    "sha256": "d" * 64,
                    "kind": "raw",
                },
                {
                    "path": "memory/projection/summary.json",
                    "sha256": "e" * 64,
                    "kind": "projection",
                },
            ],
            verifier_report={
                "source_digest": "source-fixture",
                "expected_digest_sha256": digest,
                "actual_digest_sha256": digest,
            },
        )
        os_process_profile.profile_manifest.validate_manifest(manifest)
        self.assertEqual(manifest["lane"], "os-process")
        self.assertEqual(manifest["workload"]["rows"], os_process_profile.ROWS)
        self.assertFalse(manifest["instrumentation"]["profiler_attached"])
        self.assertNotIn("benchmark_timing", manifest)

    def test_claim_boundary_excludes_leak_and_benchmark_timing(self) -> None:
        digest = "1" * 64
        manifest = os_process_profile.build_manifest(
            run_id="fixture",
            subject_sha="a" * 40,
            cargo_lock_sha256="b" * 64,
            rustc_version="rustc fixture",
            cargo_version="cargo fixture",
            target="aarch64-unknown-linux-gnu",
            oxide_subject={
                "name": "oxide-batch",
                "version": "0.6.0",
                "source": "sparse+https://index.crates.io/",
                "checksum": "c" * 64,
            },
            environment={
                "os": "Linux",
                "kernel": "fixture",
                "arch": "aarch64",
                "cpu_model": "fixture",
                "logical_cpu_count": 4,
                "memory_total_bytes": 1,
                "postgres_configured_image": "postgres:18",
                "postgres_server_version": "postgres fixture",
            },
            artifacts=[{"path": "raw.json", "sha256": "d" * 64, "kind": "raw"}],
            verifier_report={
                "source_digest": "fixture",
                "expected_digest_sha256": digest,
                "actual_digest_sha256": digest,
            },
        )
        joined = " ".join(manifest["limitations"]).lower()
        self.assertIn("not the long-running/quiescence", joined)
        self.assertIn("not stable-host benchmark or leak evidence", joined)
        self.assertNotIn("benchmark_timing", manifest)


if __name__ == "__main__":
    unittest.main()
