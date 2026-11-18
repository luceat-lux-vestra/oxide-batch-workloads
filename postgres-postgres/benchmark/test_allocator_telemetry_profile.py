#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("allocator_telemetry_profile.py")
SPEC = importlib.util.spec_from_file_location("allocator_telemetry_profile", MODULE_PATH)
assert SPEC and SPEC.loader
allocator_profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(allocator_profile)


class AllocatorTelemetryProfileTests(unittest.TestCase):
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            rows=allocator_profile.DEFAULT_ROWS,
            seed=allocator_profile.DEFAULT_SEED,
            chunk_size=allocator_profile.DEFAULT_CHUNK_SIZE,
            reader="paging",
            fetch_size=allocator_profile.DEFAULT_FETCH_SIZE,
            page_size=allocator_profile.DEFAULT_PAGE_SIZE,
        )

    def test_wired_allocator_set_is_exact(self) -> None:
        self.assertEqual(
            set(allocator_profile.WIRED_ALLOCATORS),
            {"jemalloc", "mimalloc", "snmalloc"},
        )

    def test_telemetry_build_args_select_dedicated_feature(self) -> None:
        expected = {
            "jemalloc": "allocator-jemalloc-telemetry",
            "mimalloc": "allocator-mimalloc-telemetry",
            "snmalloc": "allocator-snmalloc-telemetry",
        }
        for allocator, feature in expected.items():
            with self.subTest(allocator=allocator):
                command = allocator_profile.telemetry_build_args(allocator)
                self.assertEqual(command[-2:], ["--features", feature])
                self.assertIn("--locked", command)
                self.assertIn("--release", command)
                self.assertIn("--no-default-features", command)

    def test_unwired_allocator_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not wired"):
            allocator_profile.telemetry_build_args("system")

    def test_workload_command_keeps_database_url_out_of_argv(self) -> None:
        command = allocator_profile.workload_command(
            binary=pathlib.Path("/tmp/postgres-postgres-jemalloc-telemetry"),
            import_value="allocator_telemetry_jemalloc_paging",
            reader="paging",
            chunk_size=200,
            fetch_size=128,
            page_size=128,
        )
        self.assertEqual(command[1], "run")
        self.assertNotIn("--database-url", command)
        self.assertFalse(any("postgresql://" in item for item in command))

    def test_import_name_is_stable_and_safe(self) -> None:
        self.assertEqual(
            allocator_profile.telemetry_import_name("jemalloc", "paging"),
            "allocator_telemetry_jemalloc_paging",
        )

    def test_bounds_reject_unbounded_rows(self) -> None:
        args = self.args()
        args.rows = allocator_profile.MAX_ROWS + 1
        with self.assertRaisesRegex(ValueError, "rows must be <="):
            allocator_profile.validate_bounds(args)

    def write_snapshot(self, value: dict) -> pathlib.Path:
        root = pathlib.Path(self.tempdir.name)
        path = root / "snapshot.json"
        allocator_profile.profile_smoke.write_json(path, value)
        return path

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_validates_jemalloc_native_counters(self) -> None:
        payload = {
            "schema": allocator_profile.TELEMETRY_SCHEMA,
            "allocator": "jemalloc",
            "capture": "post-successful-workload-run",
            "native_api": "jemalloc mallctl stats.*",
            "native_payload": {
                "stats.allocated": 1,
                "stats.active": 2,
                "stats.metadata": 3,
                "stats.resident": 4,
                "stats.mapped": 5,
                "stats.retained": 6,
            },
            "limitations": ["native"],
        }
        observed = allocator_profile.validate_native_snapshot(
            self.write_snapshot(payload), "jemalloc"
        )
        self.assertEqual(observed["native_payload"]["stats.allocated"], 1)

    def test_validates_mimalloc_native_json_without_normalizing_schema(self) -> None:
        payload = {
            "schema": allocator_profile.TELEMETRY_SCHEMA,
            "allocator": "mimalloc",
            "capture": "post-successful-workload-run",
            "native_api": "mimalloc::MiMalloc::stats_json / mi_stats_get_json",
            "native_payload": {"mimalloc": {"reserved": 123}},
            "limitations": ["native"],
        }
        allocator_profile.validate_native_snapshot(
            self.write_snapshot(payload), "mimalloc"
        )

    def test_validates_snmalloc_reservation_semantics(self) -> None:
        payload = {
            "schema": allocator_profile.TELEMETRY_SCHEMA,
            "allocator": "snmalloc",
            "capture": "post-successful-workload-run",
            "native_api": "snmalloc_rs::SnMalloc::memory_stats",
            "native_payload": {
                "current_memory_usage": 10,
                "peak_memory_usage": 20,
            },
            "limitations": ["native"],
        }
        allocator_profile.validate_native_snapshot(
            self.write_snapshot(payload), "snmalloc"
        )

    def test_rejects_snapshot_allocator_mismatch(self) -> None:
        payload = {
            "schema": allocator_profile.TELEMETRY_SCHEMA,
            "allocator": "snmalloc",
            "capture": "post-successful-workload-run",
            "native_api": "snmalloc_rs::SnMalloc::memory_stats",
            "native_payload": {
                "current_memory_usage": 10,
                "peak_memory_usage": 20,
            },
            "limitations": ["native"],
        }
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            allocator_profile.validate_native_snapshot(
                self.write_snapshot(payload), "jemalloc"
            )

    def test_allocator_manifest_has_no_benchmark_timing(self) -> None:
        snapshot = {
            "native_api": "jemalloc mallctl stats.*",
        }
        manifest = allocator_profile.build_manifest(
            allocator="jemalloc",
            run_id="fixture-allocator-001",
            subject_sha="a" * 40,
            cargo_lock_sha256="b" * 64,
            rustc_version="rustc 1.100.0 (fixture)",
            cargo_version="cargo 1.100.0 (fixture)",
            target="aarch64-unknown-linux-gnu",
            oxide_subject={
                "name": "oxide-batch",
                "version": "0.6.0",
                "source": "registry+https://github.com/rust-lang/crates.io-index",
                "checksum": "c" * 64,
            },
            environment={
                "os": "Linux",
                "kernel": "6.11.0",
                "arch": "aarch64",
                "cpu_model": "fixture-cpu",
                "logical_cpu_count": 4,
                "memory_total_bytes": 8 * 1024 * 1024 * 1024,
                "postgres_configured_image": "postgres:18",
                "postgres_server_version": "PostgreSQL 18.0",
            },
            reader="paging",
            rows=2_000,
            seed=266,
            chunk_size=200,
            fetch_size=128,
            page_size=128,
            dataset_identity="fixture-dataset",
            artifacts=[
                {
                    "path": "allocator/snapshot.json",
                    "sha256": "d" * 64,
                    "kind": "raw",
                }
            ],
            verifier_report={
                "expected_digest_sha256": "e" * 64,
                "actual_digest_sha256": "e" * 64,
            },
            snapshot=snapshot,
        )
        self.assertEqual(manifest["lane"], "allocator")
        self.assertFalse(manifest["instrumentation"]["profiler_attached"])
        self.assertNotIn("benchmark_timing", manifest)
        self.assertTrue(
            any("not benchmark timing" in item for item in manifest["limitations"])
        )


if __name__ == "__main__":
    unittest.main()
