#!/usr/bin/env python3
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).with_name("perfetto_heap_probe.py")
SPEC = importlib.util.spec_from_file_location("perfetto_heap_probe", MODULE_PATH)
assert SPEC and SPEC.loader
perfetto_heap_probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(perfetto_heap_probe)


class PerfettoHeapProbeTests(unittest.TestCase):
    def test_perfetto_provenance_is_explicitly_pinned(self) -> None:
        self.assertEqual(perfetto_heap_probe.PERFETTO_RELEASE, "v59.1")
        self.assertEqual(
            perfetto_heap_probe.PERFETTO_COMMIT,
            "1e9c61e9844affd7dccd3d42d9ea9f3a3d10cf6d",
        )
        self.assertEqual(
            perfetto_heap_probe.HEAP_PROFILE_BLOB_SHA1,
            "1d5b1cc8d9bd38749c6d200a8d5aac9b616b84ed",
        )
        self.assertEqual(perfetto_heap_probe.PREBUILT_ROLL, "v58.2")
        self.assertEqual(
            perfetto_heap_probe.ARM64_PREBUILTS["tracebox"]["sha256"],
            "2d21883e9c55818f851fd8c391afb9b366032b3110076b4fd41f31f30b5849fa",
        )
        self.assertEqual(
            perfetto_heap_probe.ARM64_PREBUILTS["preload"]["sha256"],
            "4ca10d32455973389d0f772f1c9ad8616b61eb46edc3b995a2825dee9fab2662",
        )
        self.assertEqual(
            perfetto_heap_probe.ARM64_PREBUILTS["trace_processor"]["sha256"],
            "0e6e0c5452c505c8d46fe472fd196a0d17d963460727e2ce2013b02aa1309555",
        )

    def test_git_blob_sha1_matches_git_object_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "hello.txt"
            path.write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                perfetto_heap_probe.git_blob_sha1(path),
                "ce013625030ba8dba906f756967f9e9ca394464a",
            )

    def test_llvm_symbolizer_record_is_fail_closed(self) -> None:
        with mock.patch.object(
            perfetto_heap_probe.shutil,
            "which",
            return_value=None,
        ):
            record = perfetto_heap_probe.llvm_symbolizer_record()
        self.assertFalse(record["available"])
        self.assertIsNone(record["path"])
        self.assertIsNone(record["version"])

        completed = __import__("subprocess").CompletedProcess(
            ["llvm-symbolizer", "--version"],
            0,
            stdout="llvm-symbolizer version 18.1.3\n",
            stderr="",
        )
        with mock.patch.object(
            perfetto_heap_probe.shutil,
            "which",
            return_value="/usr/bin/llvm-symbolizer",
        ), mock.patch.object(
            perfetto_heap_probe.subprocess,
            "run",
            return_value=completed,
        ):
            record = perfetto_heap_probe.llvm_symbolizer_record()
        self.assertTrue(record["available"])
        self.assertEqual(record["path"], "/usr/bin/llvm-symbolizer")
        self.assertEqual(record["version"], "llvm-symbolizer version 18.1.3")
        self.assertEqual(record["version_exit_status"], 0)

    def test_replacement_allocator_dependency_guard(self) -> None:
        safe = [
            "libc.so.6 => /lib/aarch64-linux-gnu/libc.so.6 (0x1)",
            "libdl.so.2 => /lib/aarch64-linux-gnu/libdl.so.2 (0x2)",
        ]
        self.assertEqual(
            perfetto_heap_probe.replacement_allocator_dependencies(safe),
            [],
        )

        unsafe = safe + [
            "libjemalloc.so.2 => /lib/aarch64-linux-gnu/libjemalloc.so.2 (0x3)"
        ]
        self.assertEqual(
            perfetto_heap_probe.replacement_allocator_dependencies(unsafe),
            [unsafe[-1]],
        )

    def test_probe_source_has_known_noinline_malloc_site(self) -> None:
        self.assertIn(
            perfetto_heap_probe.PROBE_FUNCTION,
            perfetto_heap_probe.PROBE_SOURCE,
        )
        self.assertIn("__attribute__((noinline))", perfetto_heap_probe.PROBE_SOURCE)
        self.assertIn("malloc(size)", perfetto_heap_probe.PROBE_SOURCE)
        self.assertIn("free(ptr)", perfetto_heap_probe.PROBE_SOURCE)

    def test_profiler_command_is_fully_pinned_and_high_accuracy(self) -> None:
        command = perfetto_heap_probe.profiler_command(
            heap_profile_script=pathlib.Path("/tools/heap_profile"),
            tracebox=pathlib.Path("/tools/tracebox"),
            preload=pathlib.Path("/tools/libheapprofd_glibc_preload.so"),
            traceconv_compat=pathlib.Path("/tmp/traceconv-compat.py"),
            output=pathlib.Path("/tmp/profile"),
            debuggee=pathlib.Path("/tmp/probe"),
        )
        self.assertEqual(command[2], "host")
        self.assertIn("--interval", command)
        self.assertEqual(
            command[command.index("--interval") + 1],
            "1",
        )
        self.assertIn("--block-client", command)
        self.assertEqual(
            command[command.index("--tracebox-binary") + 1],
            "/tools/tracebox",
        )
        self.assertEqual(
            command[command.index("--preload-library") + 1],
            "/tools/libheapprofd_glibc_preload.so",
        )
        self.assertEqual(
            command[command.index("--traceconv-binary") + 1],
            "/tmp/traceconv-compat.py",
        )
        self.assertEqual(command[-2:], ["--", "/tmp/probe"])
        self.assertFalse(any("postgresql://" in item for item in command))

    def test_traceconv_compat_shim_translates_v58_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            fake_tp = root / "trace_processor_shell"
            fake_tp.write_text(
                """#!/usr/bin/env python3
import pathlib
import sys

args = sys.argv[1:]
if args[:2] == ["util", "symbolize"]:
    sys.stdout.buffer.write(b"symbol-packets")
    raise SystemExit(0)
if len(args) >= 5 and args[:2] == ["convert", "profile"]:
    output_dir = pathlib.Path(args[args.index("--output-dir") + 1])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "heap_dump.fake.pb").write_bytes(b"pprof")
    raise SystemExit(0)
raise SystemExit(9)
""",
                encoding="utf-8",
            )
            fake_tp.chmod(0o755)

            shim, evidence = perfetto_heap_probe.write_traceconv_compat_shim(
                root,
                fake_tp,
            )
            self.assertTrue(shim.is_file())
            self.assertTrue(shim.stat().st_mode & 0o111)
            self.assertGreater(evidence["bytes"], 0)
            self.assertEqual(evidence["trace_processor"], str(fake_tp))

            trace = root / "trace.pb"
            trace.write_bytes(b"trace")
            symbolized = __import__("subprocess").run(
                [str(shim), "symbolize", str(trace)],
                check=False,
                capture_output=True,
            )
            self.assertEqual(symbolized.returncode, 0)
            self.assertEqual(symbolized.stdout, b"symbol-packets")

            profile = __import__("subprocess").run(
                [str(shim), "profile", "--no-annotations", str(trace)],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(profile.returncode, 0, profile.stderr)
            output_dir = pathlib.Path(profile.stdout.strip())
            self.assertIn("heap_profile-", output_dir.name)
            self.assertEqual(
                (output_dir / "heap_dump.fake.pb").read_bytes(),
                b"pprof",
            )

    def test_trace_query_command_uses_csv_subcommand_interface(self) -> None:
        command = perfetto_heap_probe.trace_query_command(
            pathlib.Path("/tools/trace_processor_shell"),
            pathlib.Path("/tmp/raw-trace"),
            "SELECT 1 AS value;",
        )
        self.assertEqual(
            command,
            [
                "/tools/trace_processor_shell",
                "query",
                "/tmp/raw-trace",
                "SELECT 1 AS value;",
            ],
        )

    def test_single_csv_row_parser_is_strict(self) -> None:
        row = perfetto_heap_probe.parse_single_csv_row(
            '"positive_allocation_count","positive_allocation_bytes"\n'
            "33,132000\n"
        )
        self.assertEqual(
            perfetto_heap_probe.int_field(row, "positive_allocation_count"),
            33,
        )
        self.assertEqual(
            perfetto_heap_probe.int_field(row, "positive_allocation_bytes"),
            132000,
        )

        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            perfetto_heap_probe.parse_single_csv_row("value\n1\n2\n")

    def test_stack_query_requires_allocation_reachable_known_symbol(self) -> None:
        query = perfetto_heap_probe.stack_readability_query()
        self.assertIn("WITH RECURSIVE", query)
        self.assertIn("heap_profile_allocation", query)
        self.assertIn("stack_profile_callsite", query)
        self.assertIn("stack_profile_frame", query)
        self.assertIn(perfetto_heap_probe.PROBE_FUNCTION, query)
        self.assertIn("known_symbol_rows", query)

    def test_data_loss_query_covers_heapprofd_failure_stats(self) -> None:
        query = perfetto_heap_probe.data_loss_query()
        for stat in (
            "heapprofd_buffer_corrupted",
            "heapprofd_hit_guardrail",
            "heapprofd_buffer_overran",
            "heapprofd_client_error",
            "heapprofd_malformed_packet",
            "heapprofd_missing_packet",
            "heapprofd_non_finalized_profile",
            "heapprofd_sampling_interval_adjusted",
        ):
            self.assertIn(stat, query)

    def test_supported_capability_is_fail_closed(self) -> None:
        good = dict(
            host_ok=True,
            provenance_ok=True,
            dependency_ok=True,
            compile_ok=True,
            symbolizer_ok=True,
            profiler_exit_status=0,
            raw_trace_bytes=1024,
            pprof_count=1,
            allocation_count=33,
            allocation_bytes=132000,
            named_frame_rows=5,
            known_symbol_rows=1,
            heapprofd_error_count=0,
        )
        self.assertTrue(perfetto_heap_probe.supported_capability(**good))

        for field, bad_value in (
            ("host_ok", False),
            ("provenance_ok", False),
            ("dependency_ok", False),
            ("compile_ok", False),
            ("symbolizer_ok", False),
            ("profiler_exit_status", 1),
            ("raw_trace_bytes", 0),
            ("pprof_count", 0),
            ("allocation_count", 0),
            ("allocation_bytes", 0),
            ("named_frame_rows", 0),
            ("known_symbol_rows", 0),
            ("heapprofd_error_count", 1),
        ):
            case = dict(good)
            case[field] = bad_value
            self.assertFalse(
                perfetto_heap_probe.supported_capability(**case),
                field,
            )


if __name__ == "__main__":
    unittest.main()
