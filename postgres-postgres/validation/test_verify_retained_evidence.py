#!/usr/bin/env python3
"""Negative (and one positive) controls for
validation/verify-retained-evidence.py -- campaign #63 PR 4 / proof
obligation P9's "add negative tests for the workload-specific verifier
where useful".

Each negative test builds a complete, otherwise-valid synthetic
manifest+artifact-record fixture (not the real committed evidence: this
suite must keep passing even if the real retained numbers change), corrupts
exactly one relationship a real producer could lie about, and asserts the
canonical verifier actually raises a violation for it -- proving the
verifier recomputes that relationship rather than trusting the
producer-authored field. The one positive control proves the verifier does
not always fail closed regardless of input (a verifier that unconditionally
reports a violation would trivially "pass" every negative test here without
actually checking anything).
"""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("verify-retained-evidence.py")
SPEC = importlib.util.spec_from_file_location("postgres_postgres_retained_evidence_verifier", MODULE_PATH)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

SOURCE_DIGEST = "a" * 64


def base_artifact(reader_mode: str, size_key: str, size_value: int) -> dict:
    return {
        "scenario": f"{reader_mode}_bounded_resource_run",
        "reader_mode": reader_mode,
        "dataset": {
            "rows": 200_000,
            "seed": 20260904,
            "id_offset": 0,
            "source_digest_sha256": SOURCE_DIGEST,
        },
        "chunk_size": 1000,
        size_key: size_value,
        "import_name": f"evidence_{reader_mode}_run",
        "writer_config": {
            "mode": "PostgresBatchMode::MultiRowValues",
            "columns_per_row": 7,
            "max_bound_params_per_statement": 7000,
            "note": "irrelevant to these tests",
        },
        "run": {
            "job_execution_status": "COMPLETED",
            "chunks_committed": 200,
            "committed_read": 200_000,
            "committed_written": 200_000,
            "peak_rss_kib": 8000,
            "runtime_seconds": 3.0,
        },
        "verify": {
            "process_exit_code": 0,
            "source_rows": 200_000,
            "destination_rows": 200_000,
            "row_counts_match": True,
            "digests_match": True,
            "total_mismatches": 0,
            "mismatches_truncated": False,
            "peak_rss_kib": 4500,
            "runtime_seconds": 0.4,
        },
    }


def base_record(reader_mode: str, path: str, size_key: str, size_value: int) -> tuple[dict, dict]:
    manifest_record = {
        "scenario": f"{reader_mode}_bounded_resource_run",
        "artifact": {"path": path, "sha256": "b" * 64, "size_bytes": 0},
        "input": {
            "identity": {"kind": "generated-postgres-rows", "sha256": SOURCE_DIGEST},
            "reproduction": {"generator": "src/generator.rs", "rows": 200_000, "seed": 20260904, "id_offset": 0},
        },
        "parameters": {"chunk_size": 1000, size_key: size_value, "import_name": f"evidence_{reader_mode}_run"},
        "failure_point": None,
    }
    return manifest_record, base_artifact(reader_mode, size_key, size_value)


class VerifyRetainedEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "validation").mkdir()
        self.cursor_record, self.cursor_artifact = base_record(
            "cursor", "validation/cursor-run.json", "fetch_size", 500
        )
        self.paging_record, self.paging_artifact = base_record(
            "paging", "validation/paging-run.json", "page_size", 750
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_manifest(self) -> Path:
        manifest_path = self.root / "validation" / "evidence-manifest.json"
        manifest_path.write_text(
            json.dumps({"records": [self.cursor_record, self.paging_record]}), encoding="utf-8"
        )
        (self.root / "validation" / "cursor-run.json").write_text(
            json.dumps(self.cursor_artifact), encoding="utf-8"
        )
        (self.root / "validation" / "paging-run.json").write_text(
            json.dumps(self.paging_artifact), encoding="utf-8"
        )
        return manifest_path

    def assert_violation_containing(self, needle: str) -> None:
        violations = verifier.verify(self.write_manifest())
        self.assertTrue(
            any(needle in violation for violation in violations),
            f"expected a violation containing {needle!r}, got: {violations}",
        )

    def test_baseline_fixture_has_zero_violations(self) -> None:
        """Positive control: an unmutated, internally consistent fixture
        must pass, proving the negative tests below fail because of the
        specific corruption each introduces, not because the verifier
        rejects everything unconditionally."""
        self.assertEqual(verifier.verify(self.write_manifest()), [])

    def test_forged_row_counts_match_is_caught(self) -> None:
        self.cursor_artifact["verify"]["source_rows"] = 200_000
        self.cursor_artifact["verify"]["destination_rows"] = 199_999
        self.cursor_artifact["verify"]["row_counts_match"] = True  # the lie
        self.assert_violation_containing("row_counts_match")

    def test_nonzero_total_mismatches_is_caught_even_with_exit_code_zero(self) -> None:
        self.cursor_artifact["verify"]["total_mismatches"] = 3
        self.cursor_artifact["verify"]["process_exit_code"] = 0  # the lie
        self.assert_violation_containing("total_mismatches")

    def test_truncated_mismatches_on_a_claimed_clean_run_is_caught(self) -> None:
        self.paging_artifact["verify"]["mismatches_truncated"] = True
        self.assert_violation_containing("mismatches_truncated")

    def test_forged_digests_match_is_caught(self) -> None:
        self.paging_artifact["verify"]["digests_match"] = False
        self.assert_violation_containing("digests_match")

    def test_dataset_row_count_below_the_material_floor_is_caught(self) -> None:
        for artifact, record in ((self.cursor_artifact, self.cursor_record), (self.paging_artifact, self.paging_record)):
            artifact["dataset"]["rows"] = 2_000
            record["input"]["reproduction"]["rows"] = 2_000
        self.assert_violation_containing("materially larger")

    def test_mismatched_source_digest_between_cursor_and_paging_is_caught(self) -> None:
        self.paging_artifact["dataset"]["source_digest_sha256"] = "c" * 64
        self.assert_violation_containing("source_digest_sha256")

    def test_cursor_artifact_carrying_page_size_is_caught(self) -> None:
        self.cursor_artifact["page_size"] = 750
        self.assert_violation_containing("fetch_size and not page_size")

    def test_wrong_chunks_committed_is_caught(self) -> None:
        self.cursor_artifact["run"]["chunks_committed"] = 199
        self.assert_violation_containing("chunks_committed")

    def test_writer_bound_param_arithmetic_mismatch_is_caught(self) -> None:
        self.paging_artifact["writer_config"]["max_bound_params_per_statement"] = 9999
        self.assert_violation_containing("max_bound_params_per_statement")

    def test_writer_bound_exceeding_postgres_limit_is_caught(self) -> None:
        self.cursor_artifact["chunk_size"] = 20_000
        self.cursor_record["parameters"]["chunk_size"] = 20_000
        self.paging_artifact["chunk_size"] = 20_000
        self.paging_record["parameters"]["chunk_size"] = 20_000
        self.cursor_artifact["run"]["chunks_committed"] = 10
        self.paging_artifact["run"]["chunks_committed"] = 10
        self.cursor_artifact["writer_config"]["columns_per_row"] = 7
        self.cursor_artifact["writer_config"]["max_bound_params_per_statement"] = 140_000
        self.assert_violation_containing("bind-parameter limit")

    def test_missing_required_scenario_is_caught(self) -> None:
        manifest_path = self.root / "validation" / "evidence-manifest.json"
        manifest_path.write_text(json.dumps({"records": [self.cursor_record]}), encoding="utf-8")
        (self.root / "validation" / "cursor-run.json").write_text(
            json.dumps(self.cursor_artifact), encoding="utf-8"
        )
        violations = verifier.verify(manifest_path)
        self.assertTrue(any("exactly" in v for v in violations), violations)


if __name__ == "__main__":
    unittest.main()
