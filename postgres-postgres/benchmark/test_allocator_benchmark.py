#!/usr/bin/env python3
import argparse
import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("allocator_benchmark.py")
SPEC = importlib.util.spec_from_file_location("allocator_benchmark", MODULE_PATH)
assert SPEC and SPEC.loader
allocator_benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(allocator_benchmark)


class AllocatorBenchmarkTests(unittest.TestCase):
    def canonical_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            rows=allocator_benchmark.CANONICAL_ROWS,
            seed=allocator_benchmark.CANONICAL_SEED,
            chunk_size=allocator_benchmark.CANONICAL_CHUNK_SIZE,
            reader="both",
            fetch_size=allocator_benchmark.CANONICAL_FETCH_SIZE,
            page_size=allocator_benchmark.CANONICAL_PAGE_SIZE,
            warmups=allocator_benchmark.CANONICAL_WARMUPS,
            measured_runs=allocator_benchmark.CANONICAL_MEASURED_RUNS,
        )

    def test_first_cycle_balances_every_allocator_across_every_position(self) -> None:
        allocators = ("system", "jemalloc", "mimalloc", "snmalloc", "tcmalloc")
        orders = [
            allocator_benchmark.allocator_order(allocators, ordinal)
            for ordinal in range(len(allocators))
        ]
        for position in range(len(allocators)):
            self.assertEqual(
                {order[position] for order in orders},
                set(allocators),
            )

    def test_canonical_measured_prefix_is_position_balanced(self) -> None:
        allocators = ("system", "jemalloc", "mimalloc", "snmalloc", "tcmalloc")
        measured_orders = [
            allocator_benchmark.allocator_order(allocators, ordinal)
            for ordinal in range(allocator_benchmark.CANONICAL_MEASURED_RUNS)
        ]
        first_cycle = measured_orders[: len(allocators)]
        for position in range(len(allocators)):
            self.assertEqual(
                {order[position] for order in first_cycle},
                set(allocators),
            )

    def test_second_cycle_reverses_rotation_direction_deterministically(self) -> None:
        allocators = ("system", "jemalloc", "mimalloc")
        self.assertEqual(
            allocator_benchmark.allocator_order(allocators, 3),
            ("mimalloc", "jemalloc", "system"),
        )
        self.assertEqual(
            allocator_benchmark.allocator_order(allocators, 4),
            ("system", "mimalloc", "jemalloc"),
        )

    def test_canonical_geometry_requires_exact_contract(self) -> None:
        args = self.canonical_args()
        self.assertTrue(allocator_benchmark.is_canonical(args))
        args.rows -= 1
        self.assertFalse(allocator_benchmark.is_canonical(args))

    def test_noncanonical_limitations_fail_closed_to_diagnostic_scope(self) -> None:
        limitations = allocator_benchmark.benchmark_limitations(False)
        self.assertTrue(any("diagnostic only" in item for item in limitations))
        self.assertTrue(any("stable-host" in item for item in limitations))

    def test_summary_retains_distribution_not_single_number(self) -> None:
        samples = [
            {
                "elapsed_seconds": 2.0,
                "rows_per_second": 50.0,
                "user_cpu_seconds": 1.0,
                "system_cpu_seconds": 0.2,
                "max_rss_kib": 100,
            },
            {
                "elapsed_seconds": 4.0,
                "rows_per_second": 25.0,
                "user_cpu_seconds": 2.0,
                "system_cpu_seconds": 0.4,
                "max_rss_kib": 200,
            },
        ]
        summary = allocator_benchmark.summarize_samples(samples)
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["elapsed_seconds"]["median"], 3.0)
        self.assertEqual(summary["max_rss_kib"]["max"], 200.0)

    def test_sample_manifest_passes_canonical_manifest_validator(self) -> None:
        manifest = allocator_benchmark.build_sample_manifest(
            allocator="jemalloc",
            run_id="allocator-benchmark-fixture-paging-01-jemalloc",
            repetition=1,
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
                "kernel": "6.11.0",
                "arch": "aarch64",
                "cpu_model": "fixture-cpu",
                "logical_cpu_count": 4,
                "memory_total_bytes": 8 * 1024 * 1024 * 1024,
                "postgres_configured_image": "postgres:18",
                "postgres_server_version": "postgres (PostgreSQL) 18.0",
            },
            reader="paging",
            rows=allocator_benchmark.CANONICAL_ROWS,
            seed=allocator_benchmark.CANONICAL_SEED,
            chunk_size=allocator_benchmark.CANONICAL_CHUNK_SIZE,
            fetch_size=allocator_benchmark.CANONICAL_FETCH_SIZE,
            page_size=allocator_benchmark.CANONICAL_PAGE_SIZE,
            dataset_identity="source-" + "d" * 64,
            artifacts=[
                {
                    "path": "benchmark/measurement.json",
                    "sha256": "e" * 64,
                    "kind": "raw",
                }
            ],
            verifier_report={
                "expected_digest_sha256": "f" * 64,
                "actual_digest_sha256": "f" * 64,
            },
            elapsed_seconds=2.5,
            canonical=True,
        )
        allocator_benchmark.profile_manifest.validate_manifest(manifest)
        self.assertEqual(manifest["run"]["repetition"], 1)
        self.assertFalse(manifest["instrumentation"]["profiler_attached"])
        self.assertFalse(any("diagnostic only" in x for x in manifest["limitations"]))

    def test_validate_bounds_rejects_zero_measured_runs(self) -> None:
        args = self.canonical_args()
        args.measured_runs = 0
        with self.assertRaisesRegex(ValueError, "measured_runs"):
            allocator_benchmark.validate_bounds(args)

    def test_generated_database_names_are_safe_and_distinct(self) -> None:
        first = allocator_benchmark.sample_database_name(
            "1234567890", "paging", "measured", 1, "system"
        )
        second = allocator_benchmark.sample_database_name(
            "1234567890", "paging", "measured", 1, "jemalloc"
        )
        self.assertNotEqual(first, second)
        self.assertTrue(allocator_benchmark.paired.DATABASE_NAME.fullmatch(first))
        self.assertTrue(allocator_benchmark.paired.DATABASE_NAME.fullmatch(second))


if __name__ == "__main__":
    unittest.main()
