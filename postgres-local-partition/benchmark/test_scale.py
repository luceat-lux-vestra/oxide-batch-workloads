#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).with_name("scale.py")
SPEC = importlib.util.spec_from_file_location("scale", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
scale = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scale)


class ScaleHarnessTests(unittest.TestCase):
    def test_dense_worker_points_parse(self) -> None:
        self.assertEqual(scale.parse_worker_points("1,2,4,8,16,32,64"), scale.DENSE_WORKER_POINTS)
        with self.assertRaises(Exception):
            scale.parse_worker_points("2,4")
        with self.assertRaises(Exception):
            scale.parse_worker_points("1,3")

    def test_cyclic_order_covers_each_position(self) -> None:
        points = scale.DENSE_WORKER_POINTS
        orders = [scale.cyclic_order(points, index) for index in range(len(points))]
        for position in range(len(points)):
            self.assertEqual({order[position] for order in orders}, set(points))

    def test_distribution_uses_interpolated_p95(self) -> None:
        result = scale.distribution([1, 2, 3, 4, 5])
        self.assertEqual(result["median"], 3.0)
        self.assertAlmostEqual(result["p95"], 4.8)

    def test_statement_classification_is_conservative(self) -> None:
        self.assertEqual(scale.statement_class("SELECT * FROM pg_stat_activity"), "observer")
        self.assertEqual(
            scale.statement_class("INSERT INTO app_business.local_partition_projection SELECT 1"),
            "business_or_verifier",
        )
        self.assertEqual(scale.statement_class("UPDATE batch_step_execution SET x=1"), "framework_or_runtime")

    def test_session_observer_report_is_fail_closed(self) -> None:
        valid = {
            "samples": 1,
            "peaks": {
                scale.FRAMEWORK_APPLICATION_NAME: {"total": 0, "active": 0, "lock_wait": 0},
                scale.BUSINESS_APPLICATION_NAME: {"total": 0, "active": 0, "lock_wait": 0},
            },
        }
        scale.validate_session_report(valid)
        with self.assertRaises(Exception):
            scale.validate_session_report({"samples": 0, "peaks": valid["peaks"]})
        with self.assertRaises(Exception):
            scale.validate_session_report({"samples": 1, "peaks": {}})


if __name__ == "__main__":
    unittest.main()
