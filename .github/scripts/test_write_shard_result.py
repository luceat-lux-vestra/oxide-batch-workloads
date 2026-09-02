#!/usr/bin/env python3

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("write-shard-result.py")
SPEC = importlib.util.spec_from_file_location("write_shard_result", MODULE_PATH)
assert SPEC and SPEC.loader
writer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(writer)


class BuildResultTests(unittest.TestCase):
    def test_successful_ci_exit_code(self) -> None:
        result = writer.build_result(workload="csv-postgres", stage="ci", exit_code=0)
        self.assertEqual(result, {"workload": "csv-postgres", "stage": "ci", "status": "success", "outcome": "validated", "exit_code": 0})

    def test_failed_exit_code(self) -> None:
        result = writer.build_result(workload="csv-postgres", stage="ci", exit_code=1)
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["outcome"], "validated")

    def test_msrv_not_applicable(self) -> None:
        result = writer.build_result(workload="fixture-heterogeneous", stage="msrv", not_applicable=True, reason="no MSRV policy")
        self.assertEqual(
            result,
            {"workload": "fixture-heterogeneous", "stage": "msrv", "status": "success", "outcome": "not-applicable", "reason": "no MSRV policy"},
        )

    def test_not_applicable_rejected_for_ci_stage(self) -> None:
        with self.assertRaisesRegex(ValueError, "only valid for --stage msrv"):
            writer.build_result(workload="alpha", stage="ci", not_applicable=True, reason="whatever")

    def test_not_applicable_requires_non_empty_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "reason is required"):
            writer.build_result(workload="alpha", stage="msrv", not_applicable=True, reason="")

    def test_not_applicable_requires_non_empty_reason_when_none(self) -> None:
        with self.assertRaisesRegex(ValueError, "reason is required"):
            writer.build_result(workload="alpha", stage="msrv", not_applicable=True, reason=None)

    def test_exit_code_and_not_applicable_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            writer.build_result(workload="alpha", stage="msrv", exit_code=0, not_applicable=True, reason="x")

    def test_requires_one_of_exit_code_or_not_applicable(self) -> None:
        with self.assertRaisesRegex(ValueError, "one of --exit-code or --not-applicable is required"):
            writer.build_result(workload="alpha", stage="ci")


if __name__ == "__main__":
    unittest.main()
