#!/usr/bin/env python3
import importlib.util
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("profile_smoke.py")
SPEC = importlib.util.spec_from_file_location("profile_smoke", MODULE_PATH)
assert SPEC and SPEC.loader
profile_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile_smoke)


class ProfileSmokeTests(unittest.TestCase):
    def test_allocator_build_args_select_exactly_one_variant(self) -> None:
        self.assertNotIn("--features", profile_smoke.cargo_build_args("system"))
        self.assertEqual(
            profile_smoke.cargo_build_args("jemalloc")[-2:],
            ["--features", "allocator-jemalloc"],
        )
        self.assertEqual(
            profile_smoke.cargo_build_args("mimalloc")[-2:],
            ["--features", "allocator-mimalloc"],
        )

    def test_allocator_records_match_manifest_contract(self) -> None:
        for allocator in profile_smoke.ALLOCATORS:
            profile_smoke.profile_manifest.validate_allocator(
                profile_smoke.allocator_record(allocator)
            )

    def test_parse_rustc_host(self) -> None:
        self.assertEqual(
            profile_smoke.parse_rustc_host(
                "rustc 1.95.0\nbinary: rustc\nhost: aarch64-unknown-linux-gnu\n"
            ),
            "aarch64-unknown-linux-gnu",
        )

    def test_parse_cpu_model_accepts_arm64_identity_fields(self) -> None:
        cpuinfo = """processor : 0
CPU implementer : 0x41
CPU architecture: 8
CPU variant : 0x0
CPU part : 0xd0c
CPU revision : 1
"""
        self.assertEqual(
            profile_smoke.parse_cpu_model(cpuinfo),
            (
                "CPU implementer=0x41; CPU architecture=8; CPU variant=0x0; "
                "CPU part=0xd0c; CPU revision=1"
            ),
        )

    def test_parse_memory_total_bytes(self) -> None:
        self.assertEqual(
            profile_smoke.parse_memory_total_bytes("MemTotal:        8192 kB\n"),
            8192 * 1024,
        )

    def test_allocator_lock_validation_requires_exact_sys_versions(self) -> None:
        lock = """[[package]]
name = "tikv-jemalloc-sys"
version = "0.7.1+5.3.1-0-g81034ce1f1373e37dc865038e1bc8eeecf559ce8"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "a"

[[package]]
name = "libmimalloc-sys"
version = "0.1.49"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "Cargo.lock"
            path.write_text(lock, encoding="utf-8")
            profile_smoke.validate_allocator_lock(
                path, "aarch64-unknown-linux-gnu"
            )

            path.write_text(
                lock.replace('version = "0.1.49"', 'version = "0.1.50"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "expected exactly one Cargo.lock package"):
                profile_smoke.validate_allocator_lock(
                    path, "aarch64-unknown-linux-gnu"
                )

    def test_prepare_output_root_rejects_mixed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "old.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "must be empty"):
                profile_smoke.prepare_output_root(root)

    def test_build_manifest_passes_canonical_validator(self) -> None:
        manifest = profile_smoke.build_manifest(
            allocator="jemalloc",
            run_id="smoke-fixture-jemalloc-paging",
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
            rows=127,
            seed=266,
            chunk_size=32,
            fetch_size=17,
            page_size=17,
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
            elapsed_seconds=1.25,
        )
        profile_smoke.profile_manifest.validate_manifest(manifest)
        self.assertEqual(manifest["schema"], profile_smoke.profile_manifest.SCHEMA_V2)
        self.assertEqual(manifest["subject"]["cargo"], "cargo 1.100.0 (fixture)")
        self.assertEqual(manifest["allocator"]["identity"], "jemalloc")
        self.assertFalse(manifest["instrumentation"]["profiler_attached"])
        self.assertIn("diagnostic smoke", manifest["limitations"][0])

    def test_validate_bounds_rejects_claim_sized_smoke(self) -> None:
        class Args:
            rows = profile_smoke.MAX_ROWS + 1
            seed = 266
            chunk_size = 32
            fetch_size = 17
            page_size = 17

        with self.assertRaisesRegex(ValueError, "rows must be <="):
            profile_smoke.validate_bounds(Args())


if __name__ == "__main__":
    unittest.main()
