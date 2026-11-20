#!/usr/bin/env python3

import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE / "allocator_benchmark_analysis.py"
SPEC = importlib.util.spec_from_file_location("allocator_benchmark_analysis", MODULE_PATH)
assert SPEC and SPEC.loader
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


class AllocatorBenchmarkAnalysisTests(unittest.TestCase):
    def samples(self, values):
        return [
            {
                "repetition": index,
                "order_position": ((index - 1) % 5) + 1,
                "elapsed_seconds": value,
            }
            for index, value in enumerate(values, start=1)
        ]

    def test_metric_statistics_retains_outlier_without_dropping_it(self):
        result = analysis.metric_statistics(
            self.samples([1, 2, 3, 4, 5, 6, 100]),
            "elapsed_seconds",
        )
        self.assertEqual(result["min"], 1.0)
        self.assertEqual(result["q1"], 2.5)
        self.assertEqual(result["median"], 4.0)
        self.assertEqual(result["q3"], 5.5)
        self.assertEqual(result["max"], 100.0)
        self.assertEqual(result["iqr"], 3.0)
        self.assertEqual(result["mad"], 2.0)
        self.assertEqual(result["outlier_count"], 1)
        self.assertEqual(result["outliers"][0]["repetition"], 7)
        self.assertEqual(result["outliers"][0]["value"], 100.0)
        self.assertEqual(result["outliers"][0]["direction"], "high")

    def test_metric_statistics_can_report_no_outliers(self):
        result = analysis.metric_statistics(
            self.samples([1, 2, 3, 4, 5, 6, 7]),
            "elapsed_seconds",
        )
        self.assertEqual(result["outlier_count"], 0)
        self.assertEqual(result["outliers"], [])

    def test_current_campaign_contract_is_frozen(self):
        self.assertEqual(analysis.CAMPAIGN, 323)
        self.assertEqual(analysis.OXIDE_BATCH_VERSION, "0.7.0")
        self.assertEqual(
            analysis.OXIDE_BATCH_CHECKSUM,
            "cb884d99629b92fc6740a187a2ebb65350f8924a57f0c8103bad26192f17652e",
        )
        self.assertEqual(analysis.TARGET, "aarch64-unknown-linux-gnu")
        self.assertEqual(
            analysis.ALLOCATORS,
            ("system", "jemalloc", "mimalloc", "snmalloc", "tcmalloc"),
        )
        self.assertEqual(analysis.CANONICAL_PARAMETERS["rows"], 1_000_000)
        self.assertEqual(analysis.CANONICAL_PARAMETERS["measured_runs"], 7)


if __name__ == "__main__":
    unittest.main()
