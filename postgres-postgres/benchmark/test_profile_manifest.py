#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("profile_manifest.py")
SPEC = importlib.util.spec_from_file_location("profile_manifest", MODULE_PATH)
assert SPEC and SPEC.loader
profile_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile_manifest)


def valid_manifest(*, lane: str = "benchmark", allocator: str = "system") -> dict:
    wrapper = None
    upstream = {
        "status": "unavailable",
        "version": None,
        "reason": "system allocator version is host/toolchain-owned",
    }
    if allocator == "jemalloc":
        wrapper = {"name": "tikv-jemallocator", "version": "0.7.0"}
        upstream = {"status": "observed", "version": "5.3.1", "reason": None}
    elif allocator == "mimalloc":
        wrapper = {"name": "mimalloc", "version": "0.1.52"}
        upstream = {
            "status": "unavailable",
            "version": None,
            "reason": "exact upstream revision was not observed by this fixture",
        }

    document = {
        "schema": profile_manifest.SCHEMA,
        "campaign": profile_manifest.CAMPAIGN,
        "lane": lane,
        "run": {"id": "fixture-001", "repetition": 1},
        "subject": {
            "workload_sha": "a" * 40,
            "cargo_lock_sha256": "b" * 64,
            "rustc": "rustc 1.95.0",
            "target": "aarch64-unknown-linux-gnu",
            "build_profile": "release",
            "oxide_batch": {
                "name": "oxide-batch",
                "version": "0.6.0",
                "source": "registry+https://github.com/rust-lang/crates.io-index",
                "checksum": "c" * 64,
            },
        },
        "allocator": {
            "identity": allocator,
            "wrapper": wrapper,
            "upstream": upstream,
        },
        "environment": {
            "os": "Linux",
            "kernel": "6.11.0",
            "arch": "aarch64",
            "cpu_model": "fixture-cpu",
            "logical_cpu_count": 4,
            "memory_total_bytes": 8 * 1024 * 1024 * 1024,
            "postgres_configured_image": "postgres:18",
            "postgres_server_version": "PostgreSQL 18.0",
        },
        "workload": {
            "reader_mode": "paging",
            "rows": 1000,
            "seed": 266,
            "chunk_size": 100,
            "fetch_size": None,
            "page_size": 64,
            "dataset_identity": "fixture-seed-266-rows-1000",
        },
        "instrumentation": {
            "profiler_attached": lane != "benchmark",
            "tools": []
            if lane == "benchmark"
            else [{"name": "fixture-profiler", "version": "1.0", "role": "attribution"}],
        },
        "artifacts": [
            {
                "path": f"{'benchmark' if lane == 'benchmark' else lane}/raw.json",
                "sha256": "d" * 64,
                "kind": "raw",
            }
        ],
        "correctness": {
            "verdict": "PASS",
            "verifier": "postgres-postgres verify",
            "expected_digest_sha256": "e" * 64,
            "actual_digest_sha256": "e" * 64,
        },
        "limitations": [],
    }
    if lane == "benchmark":
        document["benchmark_timing"] = {
            "elapsed_seconds": 1.25,
            "rows_per_second": 800.0,
        }
    return document


class ProfileManifestTests(unittest.TestCase):
    def test_valid_benchmark_system_manifest(self) -> None:
        profile_manifest.validate_manifest(valid_manifest())

    def test_valid_cpu_jemalloc_manifest(self) -> None:
        profile_manifest.validate_manifest(
            valid_manifest(lane="cpu", allocator="jemalloc")
        )

    def test_valid_heap_mimalloc_manifest_with_unavailable_upstream_revision(self) -> None:
        profile_manifest.validate_manifest(
            valid_manifest(lane="heap", allocator="mimalloc")
        )

    def test_benchmark_rejects_profiler_attachment(self) -> None:
        document = valid_manifest()
        document["instrumentation"]["profiler_attached"] = True
        with self.assertRaisesRegex(profile_manifest.ManifestError, "must not attach"):
            profile_manifest.validate_manifest(document)

    def test_cpu_lane_requires_profiler_attachment(self) -> None:
        document = valid_manifest(lane="cpu")
        document["instrumentation"]["profiler_attached"] = False
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "requires profiler attachment"
        ):
            profile_manifest.validate_manifest(document)

    def test_profile_lane_rejects_benchmark_timing(self) -> None:
        document = valid_manifest(lane="cpu")
        document["benchmark_timing"] = {
            "elapsed_seconds": 2.0,
            "rows_per_second": 500.0,
        }
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "must not carry benchmark_timing"
        ):
            profile_manifest.validate_manifest(document)

    def test_benchmark_rejects_non_finite_timing(self) -> None:
        for field, value in (
            ("elapsed_seconds", float("nan")),
            ("rows_per_second", float("inf")),
        ):
            with self.subTest(field=field):
                document = valid_manifest()
                document["benchmark_timing"][field] = value
                with self.assertRaisesRegex(
                    profile_manifest.ManifestError, "finite and positive"
                ):
                    profile_manifest.validate_manifest(document)

    def test_accepts_sparse_crates_io_subject_provenance(self) -> None:
        document = valid_manifest()
        document["subject"]["oxide_batch"]["source"] = "sparse+https://index.crates.io/"
        profile_manifest.validate_manifest(document)

    def test_rejects_non_canonical_registry_subject_provenance(self) -> None:
        document = valid_manifest()
        document["subject"]["oxide_batch"]["source"] = "registry+https://example.test/index"
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "canonical crates.io provenance"
        ):
            profile_manifest.validate_manifest(document)

    def test_rejects_non_published_oxide_batch_subject(self) -> None:
        document = valid_manifest()
        document["subject"]["oxide_batch"]["version"] = "0.6.1"
        with self.assertRaisesRegex(profile_manifest.ManifestError, "must be '0.6.0'"):
            profile_manifest.validate_manifest(document)

    def test_replacement_allocator_requires_wrapper_identity(self) -> None:
        document = valid_manifest(allocator="jemalloc")
        document["allocator"]["wrapper"] = None
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "allocator.wrapper must be an object"
        ):
            profile_manifest.validate_manifest(document)

    def test_allocator_identity_requires_pinned_wrapper(self) -> None:
        document = valid_manifest(allocator="jemalloc")
        document["allocator"]["wrapper"]["name"] = "mimalloc"
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "must use pinned wrapper"
        ):
            profile_manifest.validate_manifest(document)

    def test_unavailable_upstream_requires_reason(self) -> None:
        document = valid_manifest(allocator="mimalloc")
        document["allocator"]["upstream"]["reason"] = ""
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "must be a non-empty string"
        ):
            profile_manifest.validate_manifest(document)

    def test_reader_specific_size_fields_do_not_mix(self) -> None:
        document = valid_manifest()
        document["workload"]["fetch_size"] = 32
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "fetch_size must be null"
        ):
            profile_manifest.validate_manifest(document)

    def test_rejects_empty_artifact_set(self) -> None:
        document = valid_manifest()
        document["artifacts"] = []
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "at least one raw evidence artifact"
        ):
            profile_manifest.validate_manifest(document)

    def test_rejects_artifact_set_without_raw_evidence(self) -> None:
        document = valid_manifest()
        document["artifacts"][0]["kind"] = "summary"
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "at least one artifact with kind 'raw'"
        ):
            profile_manifest.validate_manifest(document)

    def test_rejects_unsafe_artifact_path(self) -> None:
        document = valid_manifest()
        document["artifacts"][0]["path"] = "../outside.json"
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "relative raw-artifact path"
        ):
            profile_manifest.validate_manifest(document)

    def test_pass_requires_matching_digests(self) -> None:
        document = valid_manifest()
        document["correctness"]["actual_digest_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            profile_manifest.ManifestError, "PASS requires matching"
        ):
            profile_manifest.validate_manifest(document)

    def test_cli_emits_compact_validity_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "manifest.json"
            path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            result = subprocess.run(
                ["python3", str(MODULE_PATH), str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
            summary = json.loads(result.stdout)
            self.assertTrue(summary["valid"])
            self.assertEqual(summary["lane"], "benchmark")
            self.assertEqual(summary["allocator"], "system")


if __name__ == "__main__":
    unittest.main()
