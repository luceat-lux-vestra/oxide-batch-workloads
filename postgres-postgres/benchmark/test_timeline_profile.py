#!/usr/bin/env python3
import argparse
import importlib.util
import json
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("timeline_profile.py")
SPEC = importlib.util.spec_from_file_location("timeline_profile", MODULE_PATH)
assert SPEC and SPEC.loader
timeline_profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(timeline_profile)


class TimelineProfileTests(unittest.TestCase):
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            rows=timeline_profile.DEFAULT_ROWS,
            seed=timeline_profile.DEFAULT_SEED,
            chunk_size=timeline_profile.DEFAULT_CHUNK_SIZE,
            reader="paging",
            fetch_size=timeline_profile.DEFAULT_FETCH_SIZE,
            page_size=timeline_profile.DEFAULT_PAGE_SIZE,
        )

    def test_workload_command_keeps_database_url_out_of_argv(self) -> None:
        command = timeline_profile.workload_command(
            binary=pathlib.Path("/tmp/postgres-postgres-system"),
            import_value="timeline_profile_system_paging",
            reader="paging",
            chunk_size=200,
            fetch_size=128,
            page_size=128,
        )
        self.assertEqual(command[1], "run")
        self.assertNotIn("--database-url", command)
        self.assertFalse(any("postgresql://" in item for item in command))

    def test_import_name_is_stable_and_safe(self) -> None:
        value = timeline_profile.timeline_import_name("cursor")
        self.assertEqual(value, "timeline_profile_system_cursor")
        self.assertTrue(timeline_profile.paired.IMPORT_NAME.fullmatch(value))

    def test_bounds_reserve_headroom_below_recorder_cap(self) -> None:
        args = self.args()
        timeline_profile.validate_bounds(args)

        args.rows = 10_000
        args.chunk_size = 1
        with self.assertRaisesRegex(ValueError, "item-listener budget"):
            timeline_profile.validate_bounds(args)

    def test_timeline_evidence_requires_complete_monotonic_boundaries(self) -> None:
        records = [
            self.record(0, 10, "lifecycle", "chunk.started", "event"),
            self.record(1, 11, "item-listener", "item.read", "before"),
            self.record(2, 12, "item-listener", "item.read", "after"),
            self.record(3, 13, "item-listener", "item.process", "before"),
            self.record(4, 14, "item-listener", "item.process", "after"),
            self.record(5, 15, "item-listener", "item.write", "before"),
            self.record(6, 16, "item-listener", "item.write", "after"),
            self.record(7, 17, "lifecycle", "chunk.committed", "event"),
        ]
        summary = {
            "schema": timeline_profile.TIMELINE_SUMMARY_SCHEMA,
            "framework_telemetry_schema_version": 1,
            "clock": "process-monotonic-std-instant",
            "claim": "phase-boundary-attribution-only",
            "max_records": 50_000,
            "observed_records": len(records),
            "retained_records": len(records),
            "dropped_records": 0,
            "limitations": ["fixture"],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            events = root / "events.jsonl"
            summary_path = root / "summary.json"
            events.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
            observed = timeline_profile.validate_timeline_evidence(events, summary_path)

        self.assertEqual(observed["retained_records"], len(records))
        self.assertEqual(observed["dropped_records"], 0)

    def test_timeline_evidence_rejects_dropped_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            events = root / "events.jsonl"
            summary_path = root / "summary.json"
            events.write_text(
                json.dumps(self.record(0, 1, "lifecycle", "chunk.committed", "event"))
                + "\n",
                encoding="utf-8",
            )
            summary_path.write_text(
                json.dumps(
                    {
                        "schema": timeline_profile.TIMELINE_SUMMARY_SCHEMA,
                        "framework_telemetry_schema_version": 1,
                        "clock": "process-monotonic-std-instant",
                        "claim": "phase-boundary-attribution-only",
                        "max_records": 50_000,
                        "observed_records": 2,
                        "retained_records": 1,
                        "dropped_records": 1,
                        "limitations": ["fixture"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "dropped records"):
                timeline_profile.validate_timeline_evidence(events, summary_path)

    def test_timeline_manifest_passes_shared_validator(self) -> None:
        manifest = timeline_profile.build_manifest(
            run_id="timeline-fixture-system-paging",
            subject_sha="a" * 40,
            cargo_lock_sha256="b" * 64,
            rustc_version="rustc 1.100.0 (fixture)",
            cargo_version="cargo 1.100.0 (fixture)",
            target="aarch64-unknown-linux-gnu",
            oxide_subject={
                "name": "oxide-batch",
                "version": "0.7.0",
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
            reader="paging",
            rows=2_000,
            seed=266,
            chunk_size=200,
            fetch_size=128,
            page_size=128,
            dataset_identity="source-" + "d" * 64,
            artifacts=[
                {
                    "path": "timeline/events.jsonl",
                    "sha256": "e" * 64,
                    "kind": "raw",
                },
                {
                    "path": "timeline/summary.json",
                    "sha256": "f" * 64,
                    "kind": "projection",
                },
            ],
            verifier_report={
                "expected_digest_sha256": "1" * 64,
                "actual_digest_sha256": "1" * 64,
            },
        )
        timeline_profile.profile_manifest.validate_manifest(manifest)
        self.assertEqual(manifest["lane"], "timeline")
        self.assertEqual(manifest["allocator"]["identity"], "system")
        self.assertFalse(manifest["instrumentation"]["profiler_attached"])
        self.assertNotIn("benchmark_timing", manifest)
        self.assertEqual(
            manifest["instrumentation"]["tools"][0]["name"],
            "oxide-batch-native-timeline-recorder",
        )

    def test_limitations_preserve_lane_d_claim_boundary(self) -> None:
        limitations = timeline_profile.timeline_limitations()
        self.assertTrue(any("attribution evidence only" in item for item in limitations))
        self.assertTrue(any("not isolated PostgreSQL wait" in item for item in limitations))
        self.assertTrue(any("chunk.committed" in item for item in limitations))
        self.assertTrue(any("queue/backpressure" in item for item in limitations))
        self.assertTrue(
            any(
                "later releases" in item and "permanent allocator decision" in item
                for item in limitations
            )
        )

    @staticmethod
    def record(
        sequence: int,
        elapsed_ns: int,
        source: str,
        phase: str,
        transition: str,
    ) -> dict[str, object]:
        return {
            "schema": timeline_profile.TIMELINE_SCHEMA,
            "sequence": sequence,
            "elapsed_ns": elapsed_ns,
            "source": source,
            "phase": phase,
            "transition": transition,
            "chunk_sequence": 0,
        }


if __name__ == "__main__":
    unittest.main()
