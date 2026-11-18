#!/usr/bin/env python3
import argparse
import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("cpu_profile.py")
SPEC = importlib.util.spec_from_file_location("cpu_profile", MODULE_PATH)
assert SPEC and SPEC.loader
cpu_profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cpu_profile)


class CpuProfileTests(unittest.TestCase):
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            rows=cpu_profile.DEFAULT_ROWS,
            seed=cpu_profile.DEFAULT_SEED,
            chunk_size=cpu_profile.DEFAULT_CHUNK_SIZE,
            reader="paging",
            fetch_size=cpu_profile.DEFAULT_FETCH_SIZE,
            page_size=cpu_profile.DEFAULT_PAGE_SIZE,
            frequency_hz=cpu_profile.DEFAULT_FREQUENCY_HZ,
        )

    def test_perf_record_command_keeps_database_url_out_of_child_argv(self) -> None:
        workload = cpu_profile.workload_command(
            binary=pathlib.Path("/tmp/postgres-postgres"),
            import_value="cpu_profile_system_paging",
            reader="paging",
            chunk_size=1000,
            fetch_size=500,
            page_size=750,
        )
        command = cpu_profile.perf_record_command(
            perf="/usr/bin/perf",
            output=pathlib.Path("/tmp/perf.data"),
            frequency_hz=99,
            workload=workload,
        )
        self.assertEqual(command[:2], ["/usr/bin/perf", "record"])
        self.assertIn("--call-graph", command)
        self.assertIn("dwarf", command)
        self.assertIn("--", command)
        self.assertNotIn("--database-url", command)
        self.assertFalse(any("postgresql://" in item for item in command))

    def test_perf_projection_commands_reuse_same_raw_profile(self) -> None:
        profile = pathlib.Path("/tmp/perf.data")
        report, script = cpu_profile.perf_projection_commands("/usr/bin/perf", profile)
        self.assertEqual(report, ["/usr/bin/perf", "report", "--stdio", "--input", "/tmp/perf.data"])
        self.assertEqual(script, ["/usr/bin/perf", "script", "--input", "/tmp/perf.data"])

    def test_cpu_manifest_passes_shared_manifest_validator(self) -> None:
        manifest = cpu_profile.build_manifest(
            allocator="jemalloc",
            run_id="cpu-profile-fixture-jemalloc-paging",
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
            rows=100_000,
            seed=266,
            chunk_size=1000,
            fetch_size=500,
            page_size=750,
            dataset_identity="source-" + "d" * 64,
            artifacts=[
                {
                    "path": "cpu/perf.data",
                    "sha256": "e" * 64,
                    "kind": "raw",
                },
                {
                    "path": "cpu/perf-report.txt",
                    "sha256": "f" * 64,
                    "kind": "projection",
                },
            ],
            verifier_report={
                "expected_digest_sha256": "1" * 64,
                "actual_digest_sha256": "1" * 64,
            },
            perf_version="perf version fixture",
        )
        cpu_profile.profile_manifest.validate_manifest(manifest)
        self.assertEqual(manifest["lane"], "cpu")
        self.assertTrue(manifest["instrumentation"]["profiler_attached"])
        self.assertNotIn("benchmark_timing", manifest)
        self.assertEqual(manifest["instrumentation"]["tools"][0]["name"], "perf")

    def test_cpu_limitations_forbid_benchmark_timing_claims(self) -> None:
        limitations = cpu_profile.cpu_limitations()
        self.assertTrue(any("attribution evidence only" in item for item in limitations))
        self.assertTrue(any("off-CPU" in item for item in limitations))
        self.assertTrue(
            any(
                "later releases" in item and "permanent allocator decision" in item
                for item in limitations
            )
        )

    def test_validate_bounds_rejects_excessive_frequency(self) -> None:
        args = self.args()
        args.frequency_hz = cpu_profile.MAX_FREQUENCY_HZ + 1
        with self.assertRaisesRegex(ValueError, "frequency_hz"):
            cpu_profile.validate_bounds(args)

    def test_import_name_is_stable_and_safe(self) -> None:
        value = cpu_profile.cpu_import_name("tcmalloc", "cursor")
        self.assertEqual(value, "cpu_profile_tcmalloc_cursor")
        self.assertTrue(cpu_profile.paired.IMPORT_NAME.fullmatch(value))


if __name__ == "__main__":
    unittest.main()
