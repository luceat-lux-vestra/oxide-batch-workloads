#!/usr/bin/env python3
import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("cpu_profile_gperftools.py")
SPEC = importlib.util.spec_from_file_location("cpu_profile_gperftools", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class GperftoolsCpuProfileTests(unittest.TestCase):
    def test_allocator_boundary_accepts_profiler_only_dependencies(self) -> None:
        module.validate_allocator_boundary(
            [
                "linux-vdso.so.1 (0x0000)",
                "libstdc++.so.6 => /lib/libstdc++.so.6 (0x0001)",
                "libc.so.6 => /lib/libc.so.6 (0x0002)",
            ]
        )

    def test_allocator_boundary_rejects_tcmalloc_dependency(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "alter allocator semantics"):
            module.validate_allocator_boundary(
                [
                    "libtcmalloc_minimal.so.4 => /lib/libtcmalloc_minimal.so.4 (0x1)",
                ]
            )

    def test_profiled_environment_uses_env_not_workload_argv(self) -> None:
        env = module.profiled_environment(
            base={"RUST_LOG": "error"},
            database_url="postgresql://user:secret@example/db",
            libprofiler=pathlib.Path("/usr/lib/aarch64-linux-gnu/libprofiler.so"),
            profile_path=pathlib.Path("/tmp/profile.prof"),
            frequency_hz=99,
        )
        self.assertEqual(env["DATABASE_URL"], "postgresql://user:secret@example/db")
        self.assertEqual(
            env["LD_PRELOAD"], "/usr/lib/aarch64-linux-gnu/libprofiler.so"
        )
        self.assertEqual(env["CPUPROFILE"], "/tmp/profile.prof")
        self.assertEqual(env["CPUPROFILE_FREQUENCY"], "99")

        workload = module.cpu_profile.workload_command(
            binary=pathlib.Path("/tmp/postgres-postgres"),
            import_value="cpu_profile_system_paging",
            reader="paging",
            chunk_size=1000,
            fetch_size=500,
            page_size=750,
        )
        self.assertNotIn("--database-url", workload)
        self.assertFalse(any("postgresql://" in item for item in workload))

    def test_profiled_environment_rejects_existing_ld_preload(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "pre-existing LD_PRELOAD"):
            module.profiled_environment(
                base={"LD_PRELOAD": "/tmp/other.so"},
                database_url="postgresql://example/db",
                libprofiler=pathlib.Path("/tmp/libprofiler.so"),
                profile_path=pathlib.Path("/tmp/profile.prof"),
                frequency_hz=99,
            )

    def test_projection_commands_use_retained_binary_and_raw_profile(self) -> None:
        text_command, callgrind_command = module.projection_commands(
            pprof="/usr/bin/google-pprof",
            binary=pathlib.Path("/tmp/postgres-postgres-system"),
            profile=pathlib.Path("/tmp/profile.prof"),
        )
        self.assertEqual(
            text_command,
            [
                "/usr/bin/google-pprof",
                "--text",
                "/tmp/postgres-postgres-system",
                "/tmp/profile.prof",
            ],
        )
        self.assertEqual(
            callgrind_command,
            [
                "/usr/bin/google-pprof",
                "--callgrind",
                "/tmp/postgres-postgres-system",
                "/tmp/profile.prof",
            ],
        )

    def test_cpu_manifest_passes_shared_contract(self) -> None:
        manifest = module.build_manifest(
            allocator="system",
            run_id="cpu-gperftools-fixture-system-paging",
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
            reader="paging",
            rows=100_000,
            seed=266,
            chunk_size=1000,
            fetch_size=500,
            page_size=750,
            dataset_identity="source-" + "d" * 64,
            artifacts=[
                {
                    "path": "cpu/gperftools.prof",
                    "sha256": "e" * 64,
                    "kind": "raw",
                },
                {
                    "path": "cpu/pprof-text.txt",
                    "sha256": "f" * 64,
                    "kind": "projection",
                },
            ],
            verifier_report={
                "expected_digest_sha256": "1" * 64,
                "actual_digest_sha256": "1" * 64,
            },
            package_version="2.15-3build1",
        )
        module.profile_manifest.validate_manifest(manifest)
        self.assertEqual(manifest["lane"], "cpu")
        self.assertTrue(manifest["instrumentation"]["profiler_attached"])
        self.assertNotIn("benchmark_timing", manifest)
        self.assertEqual(
            manifest["instrumentation"]["tools"][0]["name"],
            "gperftools-libprofiler",
        )

    def test_limitations_preserve_cpu_lane_claim_boundary(self) -> None:
        limitations = module.profiler_limitations()
        self.assertTrue(any("attribution evidence only" in item for item in limitations))
        self.assertTrue(any("ITIMER_PROF/SIGPROF" in item for item in limitations))
        self.assertTrue(any("libtcmalloc" in item for item in limitations))
        self.assertTrue(any("post-M7" in item for item in limitations))


if __name__ == "__main__":
    unittest.main()
