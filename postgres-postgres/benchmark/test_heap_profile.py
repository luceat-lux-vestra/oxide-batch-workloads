#!/usr/bin/env python3
import argparse
import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("heap_profile.py")
SPEC = importlib.util.spec_from_file_location("heap_profile", MODULE_PATH)
assert SPEC and SPEC.loader
heap_profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(heap_profile)


class HeapProfileTests(unittest.TestCase):
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            rows=heap_profile.DEFAULT_ROWS,
            seed=heap_profile.DEFAULT_SEED,
            chunk_size=heap_profile.DEFAULT_CHUNK_SIZE,
            reader="paging",
            fetch_size=heap_profile.DEFAULT_FETCH_SIZE,
            page_size=heap_profile.DEFAULT_PAGE_SIZE,
        )

    def test_heaptrack_scope_is_fail_closed_to_system_allocator(self) -> None:
        heap_profile.validate_allocator_scope("system")
        for allocator in ("jemalloc", "mimalloc", "snmalloc", "tcmalloc"):
            with self.assertRaisesRegex(ValueError, "system allocator"):
                heap_profile.validate_allocator_scope(allocator)

    def test_profiler_dependency_guard_rejects_allocator_replacement(self) -> None:
        heap_profile.validate_profiler_allocator_boundary(
            [
                "libunwind.so.8 => /lib/libunwind.so.8 (0x1)",
                "libc.so.6 => /lib/libc.so.6 (0x2)",
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "replacement allocator"):
            heap_profile.validate_profiler_allocator_boundary(
                ["libtcmalloc.so.4 => /lib/libtcmalloc.so.4 (0x3)"]
            )

    def test_release_debug_build_environment_is_explicit_and_isolated(self) -> None:
        env = heap_profile.heap_build_environment({"RUST_LOG": "error"})
        self.assertEqual(env["CARGO_PROFILE_RELEASE_DEBUG"], "1")
        self.assertTrue(env["CARGO_TARGET_DIR"].endswith("target/heap-profile-build"))

        with self.assertRaisesRegex(RuntimeError, "conflicting"):
            heap_profile.heap_build_environment(
                {"CARGO_PROFILE_RELEASE_DEBUG": "2"}
            )

    def test_profiled_command_keeps_database_url_out_of_argv(self) -> None:
        workload = heap_profile.workload_command(
            binary=pathlib.Path("/tmp/postgres-postgres-system"),
            import_value="heap_profile_system_paging",
            reader="paging",
            chunk_size=1000,
            fetch_size=500,
            page_size=750,
        )
        command = heap_profile.heaptrack_record_command(
            heaptrack="/usr/bin/heaptrack",
            output_base=pathlib.Path("/tmp/heaptrack"),
            workload=workload,
        )
        self.assertEqual(command[:2], ["/usr/bin/heaptrack", "--record-only"])
        self.assertIn("--output", command)
        self.assertNotIn("--database-url", command)
        self.assertFalse(any("postgresql://" in item for item in command))

    def test_projection_contract_retains_histogram_stacks_and_massif(self) -> None:
        command = heap_profile.projection_command(
            heaptrack_print="/usr/bin/heaptrack_print",
            trace=pathlib.Path("/tmp/heaptrack.zst"),
            histogram=pathlib.Path("/tmp/hist.tsv"),
            allocation_stacks=pathlib.Path("/tmp/alloc.folded"),
            massif=pathlib.Path("/tmp/heap.massif"),
        )
        self.assertIn("--print-histogram", command)
        self.assertIn("--print-flamegraph", command)
        self.assertIn("--print-massif", command)
        self.assertIn("allocations", command)

        temporary = heap_profile.temporary_stack_command(
            heaptrack_print="/usr/bin/heaptrack_print",
            trace=pathlib.Path("/tmp/heaptrack.zst"),
            output=pathlib.Path("/tmp/temp.folded"),
        )
        self.assertIn("temporary", temporary)

    def test_heap_summary_derives_exact_event_totals(self) -> None:
        report = """
calls to allocation functions: 5 (10/s)
temporary memory allocations: 2 (4/s)
peak heap memory consumption: 64B
"""
        histogram = "8\t2\n16\t3\n"
        massif = """
snapshot=0
mem_heap_B=0
snapshot=1
mem_heap_B=48
snapshot=2
mem_heap_B=64
"""
        summary = heap_profile.build_heap_summary(
            report_text=report,
            histogram_text=histogram,
            massif_text=massif,
            allocation_stacks_text="postgres_postgres::processor::fingerprint 5\n",
        )
        self.assertEqual(summary["allocation_count"], 5)
        self.assertEqual(summary["cumulative_allocated_bytes"], 64)
        self.assertEqual(summary["peak_live_bytes"], 64)
        self.assertEqual(summary["temporary_allocation_count"], 2)
        self.assertEqual(summary["temporary_allocation_ratio"], 0.4)
        self.assertEqual(summary["size_histogram_bucket_count"], 2)
        self.assertEqual(summary["largest_allocation_bytes"], 16)
        self.assertEqual(summary["schema"], "oxide-batch-heap-profile-summary/v2")
        self.assertEqual(summary["allocation_stack_line_count"], 1)
        self.assertEqual(summary["allocation_stack_readable_line_count"], 1)
        self.assertEqual(summary["allocation_stack_readable_weight"], 5)
        self.assertEqual(summary["allocation_stack_attribution"], "readable")

    def test_heap_summary_rejects_histogram_report_count_mismatch(self) -> None:
        report = """
calls to allocation functions: 6 (10/s)
temporary memory allocations: 2 (4/s)
"""
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            heap_profile.build_heap_summary(
                report_text=report,
                histogram_text="8\t2\n16\t3\n",
                massif_text="mem_heap_B=64\n",
                allocation_stacks_text="postgres_postgres::job::map_source_row 5\n",
            )

    def test_folded_stack_gate_rejects_unknown_only_attribution(self) -> None:
        summary = heap_profile.folded_stack_summary("?? 739985\n")
        self.assertEqual(summary["line_count"], 1)
        self.assertEqual(summary["total_weight"], 739985)
        self.assertEqual(summary["readable_line_count"], 0)
        self.assertTrue(summary["unknown_only"])

        with self.assertRaisesRegex(RuntimeError, "not readable"):
            heap_profile.require_readable_stack_attribution("?? 739985\n")

    def test_folded_stack_gate_accepts_readable_symbolized_stack(self) -> None:
        text = (
            "postgres_postgres::processor::fingerprint;"
            "oxide_batch::chunk_runtime::run_attempt 42\n"
            "??;postgres_postgres::job::map_source_row 8\n"
        )
        summary = heap_profile.require_readable_stack_attribution(text)
        self.assertEqual(summary["line_count"], 2)
        self.assertEqual(summary["total_weight"], 50)
        self.assertEqual(summary["readable_line_count"], 2)
        self.assertEqual(summary["readable_weight"], 50)
        self.assertFalse(summary["unknown_only"])

    def test_folded_stack_gate_requires_known_probe_symbol(self) -> None:
        good = f"main;{heap_profile.PROBE_FUNCTION} 32\n"
        summary = heap_profile.require_readable_stack_attribution(
            good,
            required_symbol=heap_profile.PROBE_FUNCTION,
        )
        self.assertEqual(summary["total_weight"], 32)

        with self.assertRaisesRegex(RuntimeError, "required symbol"):
            heap_profile.require_readable_stack_attribution(
                "main;malloc_user 32\n",
                required_symbol=heap_profile.PROBE_FUNCTION,
            )

    def test_probe_source_contains_named_noinline_malloc_site(self) -> None:
        self.assertIn(heap_profile.PROBE_FUNCTION, heap_profile.PROBE_SOURCE)
        self.assertIn("__attribute__((noinline))", heap_profile.PROBE_SOURCE)
        self.assertIn("malloc(size)", heap_profile.PROBE_SOURCE)

    def test_heap_manifest_passes_shared_validator(self) -> None:
        manifest = heap_profile.build_manifest(
            run_id="heap-profile-fixture-system-paging",
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
            rows=25_000,
            seed=266,
            chunk_size=1000,
            fetch_size=500,
            page_size=750,
            dataset_identity="source-" + "d" * 64,
            artifacts=[
                {
                    "path": "heap/heaptrack.zst",
                    "sha256": "e" * 64,
                    "kind": "raw",
                },
                {
                    "path": "heap/summary.json",
                    "sha256": "f" * 64,
                    "kind": "projection",
                },
            ],
            verifier_report={
                "expected_digest_sha256": "1" * 64,
                "actual_digest_sha256": "1" * 64,
            },
            heaptrack_version="1.5.0+dfsg1-2ubuntu3",
        )
        heap_profile.profile_manifest.validate_manifest(manifest)
        self.assertEqual(manifest["lane"], "heap")
        self.assertTrue(manifest["instrumentation"]["profiler_attached"])
        self.assertNotIn("benchmark_timing", manifest)
        self.assertEqual(manifest["allocator"]["identity"], "system")
        self.assertEqual(
            manifest["subject"]["build_flags"],
            ["CARGO_PROFILE_RELEASE_DEBUG=1"],
        )

    def test_limitations_preserve_heap_claim_boundary(self) -> None:
        limitations = heap_profile.heap_limitations()
        self.assertTrue(any("attribution evidence only" in item for item in limitations))
        self.assertTrue(any("system allocator" in item for item in limitations))
        self.assertTrue(any("CARGO_PROFILE_RELEASE_DEBUG=1" in item for item in limitations))
        self.assertTrue(
            any(
                "later releases" in item and "permanent allocator decision" in item
                for item in limitations
            )
        )

    def test_validate_bounds_is_bounded(self) -> None:
        args = self.args()
        args.rows = heap_profile.MAX_ROWS + 1
        with self.assertRaisesRegex(ValueError, "rows"):
            heap_profile.validate_bounds(args)


if __name__ == "__main__":
    unittest.main()
