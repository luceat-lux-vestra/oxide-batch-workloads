#!/usr/bin/env python3
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).with_name("perfetto_heap_profile.py")
SPEC = importlib.util.spec_from_file_location("perfetto_heap_profile", MODULE_PATH)
assert SPEC and SPEC.loader
perfetto_heap_profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(perfetto_heap_profile)


class PerfettoHeapProfileTests(unittest.TestCase):
    def test_import_name_is_perfetto_specific(self) -> None:
        self.assertEqual(
            perfetto_heap_profile.perfetto_import_name("paging"),
            "perfetto_heap_system_paging",
        )
        self.assertEqual(
            perfetto_heap_profile.perfetto_import_name("cursor"),
            "perfetto_heap_system_cursor",
        )

    def test_symbolizer_identity_requires_parseable_version(self) -> None:
        completed = subprocess.CompletedProcess(
            ["llvm-symbolizer", "--version"],
            0,
            stdout="llvm-symbolizer\nLLVM version 18.1.3\nOptimized build.\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp:
            binary = pathlib.Path(temp) / "llvm-symbolizer"
            binary.write_bytes(b"symbolizer")
            with mock.patch.object(
                perfetto_heap_profile.shutil,
                "which",
                return_value=str(binary),
            ), mock.patch.object(
                perfetto_heap_profile.subprocess,
                "run",
                return_value=completed,
            ):
                record = perfetto_heap_profile.symbolizer_identity()
        self.assertTrue(record["available"])
        self.assertEqual(record["version"], "18.1.3")
        self.assertEqual(record["version_exit_status"], 0)
        self.assertEqual(
            record["sha256"],
            "b3a21fd307aa739dba687119cdd0711fa9f1f0afcbf78b01bd93767b5e2b4b61",
        )

    def test_profiler_command_uses_host_mode_and_full_workload(self) -> None:
        command = perfetto_heap_profile.profiler_command(
                heap_profile_script=pathlib.Path("/tools/heap_profile"),
                tracebox=pathlib.Path("/tools/tracebox"),
                preload=pathlib.Path("/tools/libheapprofd_glibc_preload.so"),
                traceconv_compat=pathlib.Path("/tmp/traceconv-compat.py"),
                output=pathlib.Path("/tmp/profile"),
                workload=[
                    "/tmp/postgres-postgres",
                    "run",
                    "--import-name",
                    "perfetto_heap_system_paging",
                    "--chunk-size",
                    "1000",
                    "--reader",
                    "paging",
                    "--page-size",
                    "750",
                ],
            )
        self.assertEqual(
            command[:3],
            [perfetto_heap_profile.sys.executable, "/tools/heap_profile", "host"],
        )
        self.assertEqual(command[command.index("--interval") + 1], "1")
        self.assertIn("--block-client", command)
        self.assertEqual(
            command[command.index("--traceconv-binary") + 1],
            "/tmp/traceconv-compat.py",
        )
        separator = command.index("--")
        self.assertEqual(
            command[separator + 1 :],
            [
                "/tmp/postgres-postgres",
                "run",
                "--import-name",
                "perfetto_heap_system_paging",
                "--chunk-size",
                "1000",
                "--reader",
                "paging",
                "--page-size",
                "750",
            ],
        )

    def test_stack_summary_requires_same_callsite_workload_and_oxide_frames(self) -> None:
        query = perfetto_heap_profile.stack_summary_query()
        self.assertIn("heap_profile_allocation", query)
        self.assertIn("stack_profile_callsite", query)
        self.assertIn("stack_profile_frame", query)
        self.assertIn("postgres_postgres", query)
        self.assertIn("oxide_batch", query)
        self.assertIn("cross_boundary_callsite_count", query)
        self.assertIn("s.depth < 256", query)

    def test_stack_symbol_projection_is_allocation_reachable(self) -> None:
        query = perfetto_heap_profile.stack_symbols_query()
        self.assertIn("SELECT DISTINCT callsite_id", query)
        self.assertIn("WHERE size > 0", query)
        self.assertIn("ORDER BY s.root_id, s.depth", query)

    def test_supported_workload_attribution_is_fail_closed(self) -> None:
        good = dict(
            profiler_exit_status=0,
            raw_trace_bytes=2000,
            symbol_packets_bytes=300,
            symbolized_trace_bytes=2300,
            pprof_count=1,
            allocation_count=100,
            allocation_bytes=100000,
            positive_callsite_count=10,
            frame_rows=40,
            named_frame_rows=20,
            rust_style_callsite_count=8,
            oxide_batch_callsite_count=4,
            workload_callsite_count=5,
            cross_boundary_callsite_count=2,
            heapprofd_error_count=0,
            symbol_projection_rows=20,
        )
        self.assertTrue(
            perfetto_heap_profile.supported_workload_attribution(**good)
        )
        for field, bad in (
            ("profiler_exit_status", 1),
            ("raw_trace_bytes", 0),
            ("symbol_packets_bytes", 0),
            ("symbolized_trace_bytes", 0),
            ("pprof_count", 0),
            ("allocation_count", 0),
            ("allocation_bytes", 0),
            ("positive_callsite_count", 0),
            ("frame_rows", 0),
            ("named_frame_rows", 0),
            ("rust_style_callsite_count", 0),
            ("oxide_batch_callsite_count", 0),
            ("workload_callsite_count", 0),
            ("cross_boundary_callsite_count", 0),
            ("heapprofd_error_count", 1),
            ("symbol_projection_rows", 0),
        ):
            case = dict(good)
            case[field] = bad
            self.assertFalse(
                perfetto_heap_profile.supported_workload_attribution(**case),
                field,
            )

    def test_build_summary_records_cross_boundary_attribution(self) -> None:
        summary = perfetto_heap_profile.build_summary(
            profiler_exit_status=0,
            raw_trace_bytes=2061,
            symbol_packets_bytes=332,
            symbolized_trace_bytes=2393,
            pprof_count=1,
            allocation_row={
                "positive_allocation_count": "123",
                "positive_allocation_bytes": "456789",
                "positive_callsite_count": "12",
            },
            stack_row={
                "frame_rows": "80",
                "named_frame_rows": "60",
                "rust_style_callsite_count": "10",
                "oxide_batch_callsite_count": "6",
                "workload_callsite_count": "7",
                "cross_boundary_callsite_count": "4",
            },
            loss_row={"heapprofd_error_count": "0"},
            symbol_projection_rows=60,
        )
        self.assertTrue(summary["supported"])
        self.assertEqual(
            summary["allocation_stack_attribution"],
            "readable-rust-workload-to-oxide-batch",
        )
        self.assertEqual(summary["cross_boundary_callsite_count"], 4)
        self.assertEqual(summary["timing_claim"], "attribution-only")

    def test_manifest_is_heap_lane_without_benchmark_timing(self) -> None:
        digest = "a" * 64
        manifest = perfetto_heap_profile.build_manifest(
            run_id="perfetto-heap-profile-test-system-paging",
            subject_sha="b" * 40,
            cargo_lock_sha256="c" * 64,
            rustc_version="rustc 1.100.0",
            cargo_version="cargo 1.100.0",
            target="aarch64-unknown-linux-gnu",
            oxide_subject={
                "name": "oxide-batch",
                "version": "0.6.0",
                "source": "registry+https://github.com/rust-lang/crates.io-index",
                "checksum": "d" * 64,
            },
            environment={
                "os": "Ubuntu 24.04",
                "kernel": "7.0.0-test",
                "arch": "aarch64",
                "cpu_model": "test arm64",
                "logical_cpu_count": 4,
                "memory_total_bytes": 8 * 1024 * 1024 * 1024,
                "postgres_configured_image": "postgres:18",
                "postgres_server_version": "18.0",
            },
            reader="paging",
            rows=25000,
            seed=266,
            chunk_size=1000,
            fetch_size=500,
            page_size=750,
            dataset_identity="dataset-digest",
            artifacts=[
                {
                    "path": "heap/profile/raw-trace",
                    "sha256": "e" * 64,
                    "kind": "raw",
                }
            ],
            verifier_report={
                "expected_digest_sha256": digest,
                "actual_digest_sha256": digest,
            },
            symbolizer_version="18.1.3",
        )
        self.assertEqual(manifest["lane"], "heap")
        self.assertTrue(manifest["instrumentation"]["profiler_attached"])
        self.assertNotIn("benchmark_timing", manifest)
        self.assertEqual(manifest["allocator"]["identity"], "system")
        self.assertEqual(
            [tool["name"] for tool in manifest["instrumentation"]["tools"]],
            [
                "perfetto-heapprofd-host",
                "trace_processor_shell",
                "llvm-symbolizer",
            ],
        )


if __name__ == "__main__":
    unittest.main()
