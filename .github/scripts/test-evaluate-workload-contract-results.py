#!/usr/bin/env python3

import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("evaluate-workload-contract-results.py")
SPEC = importlib.util.spec_from_file_location("evaluate_workload_contract_results", MODULE_PATH)
assert SPEC and SPEC.loader
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


class EvaluateWorkloadContractResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.results_dir = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_result(self, contract: str, workload: str, status: str, suffix: str = "0") -> None:
        target = self.results_dir / f"{contract}-{workload}-{suffix}.json"
        target.write_text(
            f'{{"contract":"{contract}","workload":"{workload}","status":"{status}"}}',
            encoding="utf-8",
        )

    def load(self, contract: str = "ci") -> list[dict[str, str]]:
        return evaluator.load_results(self.results_dir, contract)

    def assert_rejected(self, expected: str, **kwargs) -> None:
        with self.assertRaisesRegex(evaluator.EvaluationError, expected):
            evaluator.evaluate(**kwargs)

    def test_accepts_complete_successful_result_set(self) -> None:
        self.write_result("ci", "alpha", "success")
        self.write_result("ci", "beta", "success")
        evaluator.evaluate(
            contract="ci",
            expected_workloads=["alpha", "beta"],
            results=self.load(),
            discovery_result="success",
            fanout_result="success",
        )

    def test_rejects_discovery_failure(self) -> None:
        self.assert_rejected(
            "discovery did not succeed",
            contract="ci",
            expected_workloads=["alpha"],
            results=[],
            discovery_result="failure",
            fanout_result="success",
        )

    def test_rejects_missing_expected_result(self) -> None:
        self.write_result("ci", "alpha", "success")
        self.assert_rejected(
            "missing shard result",
            contract="ci",
            expected_workloads=["alpha", "beta"],
            results=self.load(),
            discovery_result="success",
            fanout_result="success",
        )

    def test_rejects_non_success_status(self) -> None:
        self.write_result("ci", "alpha", "skipped")
        self.assert_rejected(
            "non-success shard result",
            contract="ci",
            expected_workloads=["alpha"],
            results=self.load(),
            discovery_result="success",
            fanout_result="success",
        )

    def test_rejects_duplicate_shard(self) -> None:
        self.write_result("ci", "alpha", "success", suffix="1")
        self.write_result("ci", "alpha", "success", suffix="2")
        self.assert_rejected(
            "duplicate shard result",
            contract="ci",
            expected_workloads=["alpha"],
            results=self.load(),
            discovery_result="success",
            fanout_result="success",
        )

    def test_rejects_extra_shard(self) -> None:
        self.write_result("ci", "alpha", "success")
        self.write_result("ci", "beta", "success")
        self.assert_rejected(
            "unexpected shard result",
            contract="ci",
            expected_workloads=["alpha"],
            results=self.load(),
            discovery_result="success",
            fanout_result="success",
        )

    def test_rejects_fanout_cancelled(self) -> None:
        self.write_result("ci", "alpha", "success")
        self.assert_rejected(
            "fan-out job did not succeed: cancelled",
            contract="ci",
            expected_workloads=["alpha"],
            results=self.load(),
            discovery_result="success",
            fanout_result="cancelled",
        )

    def test_rejects_zero_workloads_for_ci(self) -> None:
        self.assert_rejected(
            "empty for ci contract",
            contract="ci",
            expected_workloads=[],
            results=[],
            discovery_result="success",
            fanout_result="success",
        )

    def test_accepts_empty_msrv_when_explicitly_not_applicable(self) -> None:
        evaluator.evaluate(
            contract="msrv",
            expected_workloads=[],
            results=[],
            discovery_result="success",
            fanout_result="skipped",
        )


if __name__ == "__main__":
    unittest.main()
