#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
import unittest

import allocator_memory_quiescence as memory_campaign


class AllocatorMemoryQuiescenceTests(unittest.TestCase):
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            iterations=memory_campaign.DEFAULT_ITERATIONS,
            sample_interval_ms=memory_campaign.DEFAULT_SAMPLE_INTERVAL_MS,
            post_terminal_quiescence_ms=(
                memory_campaign.DEFAULT_POST_TERMINAL_QUIESCENCE_MS
            ),
        )

    def test_default_geometry_is_balanced_and_avoids_crash_slots(self) -> None:
        args = self.args()
        memory_campaign.validate_geometry(args)
        sequences = range(1, args.iterations + 1)
        self.assertEqual(sum(sequence % 2 == 1 for sequence in sequences), 5)
        self.assertEqual(sum(sequence % 2 == 0 for sequence in sequences), 5)
        self.assertFalse(any(sequence % 50 in {24, 49} for sequence in sequences))

    def test_geometry_requires_even_bounded_iterations_and_explicit_quiescence(self) -> None:
        args = self.args()
        args.iterations = 23
        with self.assertRaisesRegex(ValueError, "between 2 and 22"):
            memory_campaign.validate_geometry(args)

        args = self.args()
        args.iterations = 9
        with self.assertRaisesRegex(ValueError, "must be even"):
            memory_campaign.validate_geometry(args)

        args = self.args()
        args.post_terminal_quiescence_ms = 0
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            memory_campaign.validate_geometry(args)

    def test_campaign_rejects_noncanonical_target(self) -> None:
        self.assertEqual(
            [candidate["identity"] for candidate in memory_campaign.candidates_for_target(
                memory_campaign.CANONICAL_TARGET
            )],
            ["system", "jemalloc", "mimalloc", "snmalloc", "tcmalloc"],
        )
        with self.assertRaisesRegex(RuntimeError, "requires exact target"):
            memory_campaign.candidates_for_target("x86_64-unknown-linux-gnu")

    def test_build_args_use_native_telemetry_feature_only_when_wired(self) -> None:
        registry = memory_campaign.profile_smoke.ALLOCATOR_REGISTRY
        expected = {
            "system": None,
            "jemalloc": "allocator-jemalloc-telemetry",
            "mimalloc": "allocator-mimalloc-telemetry",
            "snmalloc": "allocator-snmalloc-telemetry",
            "tcmalloc": "allocator-tcmalloc",
        }
        for allocator, feature in expected.items():
            with self.subTest(allocator=allocator):
                candidate = memory_campaign.allocator_registry.candidate_by_id(
                    registry, allocator
                )
                command = memory_campaign.build_args(candidate)
                self.assertIn("--locked", command)
                self.assertIn("--release", command)
                self.assertIn("--no-default-features", command)
                self.assertIn("postgres-postgres-endurancectl", command)
                if feature is None:
                    self.assertNotIn("--features", command)
                else:
                    index = command.index("--features")
                    self.assertEqual(command[index + 1], feature)

    @staticmethod
    def sample(
        *,
        sequence: int,
        timestamp: int,
        kind: str,
        process: dict[str, object] | None,
    ) -> dict[str, object]:
        return {
            "schema": memory_campaign.SAMPLE_SCHEMA,
            "schema_version": memory_campaign.SAMPLE_SCHEMA_VERSION,
            "timestamp_unix_nanos_utc": timestamp,
            "campaign_id": "allocmem-fixture-system",
            "sequence": sequence,
            "sample_kind": kind,
            "sample_interval_millis": 50,
            "process": process,
            "postgres": {
                "total_connections": 2,
                "active_connections": 1,
                "database_size_bytes": 1000 + sequence,
            },
            "oxide_batch": {
                "job_instances": sequence,
                "job_executions": sequence,
                "step_executions": sequence,
                "job_instance_table_bytes": 100,
                "job_execution_table_bytes": 200,
                "step_execution_table_bytes": 300,
            },
            "filesystem": {
                "total_bytes": 10_000,
                "used_bytes": 2_000,
                "available_bytes": 8_000,
            },
        }

    @staticmethod
    def workload_process(pid: int, rss: int) -> dict[str, object]:
        return {
            "pid": pid,
            "role": "clean-workload",
            "rss_bytes": rss,
            "pss_bytes": rss - 100,
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

    def write_two_sequence_fixture(self, path: pathlib.Path) -> None:
        records = [
            self.sample(
                sequence=1,
                timestamp=100,
                kind="process-role-start",
                process=self.workload_process(101, 1000),
            ),
            self.sample(
                sequence=1,
                timestamp=110,
                kind="periodic",
                process=self.workload_process(101, 1200),
            ),
            self.sample(
                sequence=1,
                timestamp=120,
                kind="terminal-success",
                process=None,
            ),
            self.sample(
                sequence=1,
                timestamp=130,
                kind="quiescence-start",
                process=None,
            ),
            self.sample(
                sequence=1,
                timestamp=140,
                kind="quiescence-periodic",
                process=None,
            ),
            self.sample(
                sequence=1,
                timestamp=150,
                kind="quiescence-end",
                process=None,
            ),
            self.sample(
                sequence=2,
                timestamp=200,
                kind="process-role-start",
                process=self.workload_process(202, 2000),
            ),
            self.sample(
                sequence=2,
                timestamp=210,
                kind="terminal-success",
                process=None,
            ),
            self.sample(
                sequence=2,
                timestamp=220,
                kind="quiescence-start",
                process=None,
            ),
            self.sample(
                sequence=2,
                timestamp=230,
                kind="quiescence-end",
                process=None,
            ),
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_summary_retains_per_sequence_resource_and_quiescence_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "samples.jsonl"
            self.write_two_sequence_fixture(path)
            summary = memory_campaign.summarize_samples(
                path,
                expected_campaign_id="allocmem-fixture-system",
                iterations=2,
            )

        self.assertEqual(summary["sample_count"], 10)
        self.assertEqual(len(summary["sequences"]), 2)
        first = summary["sequences"][0]
        self.assertEqual(first["reader"], "cursor")
        self.assertEqual(first["quiescence_periodic_count"], 1)
        self.assertEqual(first["workload_process_ids"], [101])
        self.assertEqual(
            first["roles"]["clean-workload"]["rss_bytes"]["max"],
            1200,
        )
        self.assertEqual(
            first["quiescence_end"]["postgres"]["database_size_bytes"],
            1001,
        )

    def test_summary_rejects_process_during_quiescence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "samples.jsonl"
            self.write_two_sequence_fixture(path)
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            for record in records:
                if (
                    record["sequence"] == 1
                    and record["sample_kind"] == "quiescence-periodic"
                ):
                    record["process"] = self.workload_process(101, 900)
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "must not retain a process"):
                memory_campaign.summarize_samples(
                    path,
                    expected_campaign_id="allocmem-fixture-system",
                    iterations=2,
                )

    def test_summary_rejects_missing_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "samples.jsonl"
            records = [
                self.sample(
                    sequence=1,
                    timestamp=100,
                    kind="terminal-success",
                    process=self.workload_process(101, 1000),
                ),
                self.sample(
                    sequence=1,
                    timestamp=110,
                    kind="quiescence-start",
                    process=None,
                ),
                self.sample(
                    sequence=1,
                    timestamp=120,
                    kind="quiescence-end",
                    process=None,
                ),
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "sequence coverage mismatch"):
                memory_campaign.summarize_samples(
                    path,
                    expected_campaign_id="allocmem-fixture-system",
                    iterations=2,
                )

    def test_campaign_id_is_safe_and_bounded(self) -> None:
        value = memory_campaign.campaign_id("local_1234567890" * 8, "jemalloc")
        self.assertLessEqual(len(value), 48)
        self.assertTrue(value.startswith("allocmem-local-"))

    def test_redaction_keeps_database_urls_out_of_retained_logs(self) -> None:
        secret = "postgresql://user:password@localhost/database"
        self.assertNotIn(
            secret,
            memory_campaign.redact_secret(f"failure: {secret}", secret),
        )


if __name__ == "__main__":
    unittest.main()
