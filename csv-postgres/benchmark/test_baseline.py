#!/usr/bin/env python3
import importlib.util
import math
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("baseline.py")
SPEC = importlib.util.spec_from_file_location("baseline", MODULE_PATH)
assert SPEC and SPEC.loader
baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baseline)


class BaselineTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertTrue(math.isclose(baseline.percentile(values, 0.95), 4.8))

    def test_summary_uses_measured_runs_only(self):
        measured = [
            {"elapsed_seconds": 2.0, "rows_per_second": 50.0, "max_rss_kib": 100},
            {"elapsed_seconds": 1.0, "rows_per_second": 100.0, "max_rss_kib": 120},
            {"elapsed_seconds": 4.0, "rows_per_second": 25.0, "max_rss_kib": 110},
        ]
        summary = baseline.summarize(measured)
        self.assertEqual(summary["measured_runs"], 3)
        self.assertEqual(summary["elapsed_seconds"]["median"], 2.0)
        self.assertEqual(summary["rows_per_second"]["median"], 50.0)
        self.assertEqual(summary["max_rss_kib"]["median"], 110)

    def test_parse_time_file_requires_complete_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "time.txt"
            path.write_text(
                "elapsed=1.25\nuser=0.80\nsystem=0.10\nmax_rss_kib=12345\n\n",
                encoding="utf-8",
            )
            parsed = baseline.parse_time_file(path)
            self.assertEqual(parsed["elapsed"], 1.25)
            self.assertEqual(parsed["max_rss_kib"], 12345)

            path.write_text("elapsed=1.0\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                baseline.parse_time_file(path)

    def test_parse_verify_report_preserves_digest_evidence(self):
        digest = "a" * 64
        report = baseline.parse_verify_report(
            '{"source_rows":100,"db_row_count":100,"row_counts_match":true,'
            f'"source_digest_sha256":"{digest}","db_digest_sha256":"{digest}",'
            '"digests_match":true}'
        )
        self.assertEqual(report["source_rows"], 100)
        self.assertEqual(report["source_digest_sha256"], digest)

    def test_parse_verify_report_rejects_contradictory_success(self):
        with self.assertRaises(RuntimeError):
            baseline.parse_verify_report(
                '{"source_rows":100,"db_row_count":99,"row_counts_match":true,'
                '"source_digest_sha256":"a","db_digest_sha256":"a",'
                '"digests_match":true}'
            )

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "input.txt"
            path.write_text("abc", encoding="utf-8")
            self.assertEqual(
                baseline.sha256_file(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )


if __name__ == "__main__":
    unittest.main()
