#!/usr/bin/env python3
import importlib.util
import json
import math
import pathlib
import tempfile
import textwrap
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("paired.py")
SPEC = importlib.util.spec_from_file_location("paired", MODULE_PATH)
assert SPEC and SPEC.loader
paired = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(paired)


class PairedHarnessTests(unittest.TestCase):
    def test_candidate_order_alternates(self):
        self.assertEqual(paired.candidate_order(0), ("oxide", "raw"))
        self.assertEqual(paired.candidate_order(1), ("raw", "oxide"))
        self.assertEqual(paired.candidate_order(2), ("oxide", "raw"))

    def test_recovery_kill_chunk_targets_durable_prefix_near_half(self):
        self.assertEqual(paired.recovery_kill_chunk(1_000_000, 1_000), 501)
        durable = (501 - 1) * 1_000
        self.assertEqual(durable, 500_000)
        with self.assertRaises(ValueError):
            paired.recovery_kill_chunk(300, 100)

    def test_writer_metrics_preserve_pinned_writer_bounds(self):
        metrics = paired.writer_metrics(1_000, 1_000)
        self.assertEqual(metrics["committed_chunks"], 1)
        self.assertEqual(metrics["writer_statements"], 4)
        self.assertEqual(metrics["bound_parameters_total"], 7_000)
        self.assertEqual(metrics["max_parameters_per_statement"], 2_000)
        self.assertEqual(metrics["rows_per_full_statement"], 285)
        self.assertEqual(metrics["max_bound_parameters_per_full_statement"], 1_995)

    def test_distribution_includes_p95(self):
        summary = paired.distribution([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertTrue(math.isclose(summary["p95"], 4.8))
        self.assertEqual(summary["median"], 3.0)

    def test_summary_uses_measured_pairs_and_paired_ratios(self):
        pairs = []
        for index, oxide_elapsed in enumerate((2.0, 4.0), 1):
            oxide = {
                "elapsed_seconds": oxide_elapsed,
                "rows_per_second": 100.0 / oxide_elapsed,
                "max_rss_kib": 100 + index,
                "user_cpu_seconds": 1.0,
                "system_cpu_seconds": 0.5,
            }
            raw = {
                "elapsed_seconds": oxide_elapsed / 2.0,
                "rows_per_second": 100.0 / (oxide_elapsed / 2.0),
                "max_rss_kib": 50 + index,
                "user_cpu_seconds": 0.5,
                "system_cpu_seconds": 0.25,
            }
            pairs.append(
                {
                    "candidates": {"oxide": oxide, "raw": raw},
                    "ratios": paired.paired_ratios(oxide, raw),
                }
            )
        summary = paired.summarize_pairs(pairs)
        self.assertEqual(summary["measured_pairs"], 2)
        self.assertEqual(summary["paired_ratios"]["elapsed_raw_to_oxide"]["median"], 0.5)
        self.assertEqual(summary["paired_ratios"]["throughput_raw_to_oxide"]["median"], 2.0)

    def test_parse_verify_report_rejects_mismatch(self):
        report = {
            "import_name": "x",
            "source_digest": "a" * 64,
            "source_rows": 10,
            "destination_rows": 9,
            "row_counts_match": False,
            "expected_digest_sha256": "b" * 64,
            "actual_digest_sha256": "c" * 64,
            "digests_match": False,
            "total_mismatches": 1,
            "mismatches": [],
            "mismatches_truncated": False,
        }
        with self.assertRaises(RuntimeError):
            paired.parse_verify_report(json.dumps(report), 10)

    def test_database_url_replaces_only_database_path_and_rejects_unsafe_name(self):
        url = paired.database_url(
            "postgresql://user:pass@localhost:5434/postgres_postgres_workload",
            "bench_c_m_01_o",
        )
        self.assertEqual(url, "postgresql://user:pass@localhost:5434/bench_c_m_01_o")
        with self.assertRaises(ValueError):
            paired.database_url(url, "bad-name;drop")

    def test_lock_package_records_source_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "Cargo.lock"
            path.write_text(
                textwrap.dedent(
                    """
                    version = 4

                    [[package]]
                    name = "oxide-batch"
                    version = "0.6.0"
                    source = "registry+https://github.com/rust-lang/crates.io-index"
                    checksum = "deadbeef"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            package = paired.lock_package(path, "oxide-batch", "0.6.0")
            self.assertEqual(package["source"], "registry+https://github.com/rust-lang/crates.io-index")
            self.assertEqual(package["checksum"], "deadbeef")

    def test_sample_database_names_are_safe_and_distinct(self):
        names = {
            paired.sample_database_name(mode, kind, ordinal, candidate)
            for mode in paired.READER_MODES
            for kind in ("warmup", "measured", "recovery")
            for ordinal in (1, 2)
            for candidate in paired.CANDIDATES
        }
        self.assertEqual(len(names), 24)
        self.assertTrue(all(paired.DATABASE_NAME.fullmatch(name) for name in names))

    def test_workflow_canonical_defaults_and_security_shape(self):
        path = MODULE_PATH.parents[2] / ".github" / "workflows" / "benchmark-postgres-postgres-paired.yml"
        text = path.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("push:", text)
        for value in ('default: "1000000"', 'default: "20260904"', 'default: "1000"', 'default: "500"', 'default: "750"', 'default: "2"', 'default: "7"'):
            self.assertIn(value, text)
        self.assertIn("runs-on: ubuntu-24.04", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("continue-on-error: true", text)
        self.assertIn("if: always()", text)
        self.assertIn("retention-days: 30", text)


if __name__ == "__main__":
    unittest.main()
