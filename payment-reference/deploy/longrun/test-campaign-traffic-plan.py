#!/usr/bin/env python3
"""#318 two-logical-day Merchant traffic-plan contract and negative controls."""
from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("campaign_traffic_plan", HERE / "campaign-traffic-plan.py")
assert spec is not None and spec.loader is not None
traffic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(traffic)


class TrafficPlanTests(unittest.TestCase):
    def test_exact_100_per_day_and_five_qualified_fault_classes(self):
        for sequence in (1, 2, 10, 30):
            with self.subTest(day=sequence):
                result = traffic.build("smoke318", sequence)
                self.assertEqual(result["primary_lifecycles"], 100)
                self.assertEqual(len(result["slots"]), 100)
                self.assertEqual(result["traffic_counts"], {
                    "auth_capture_purchase": 40,
                    "authorize_capture": 25,
                    "authorize_cancel": 10,
                    "auth_capture_refund": 15,
                    "fault_overlay": 10,
                })
                self.assertEqual(Counter(s["kind"] for s in result["slots"]), result["traffic_counts"])
                self.assertEqual(result["fault_counts"], dict.fromkeys(traffic.FAULT_CLASSES, 2))
                self.assertFalse(result["executable"])
                self.assertTrue(result["external_verification_required"])

    def test_two_days_have_nonoverlapping_deterministic_ids(self):
        one = traffic.build("smoke318", 1)
        two = traffic.build("smoke318", 2)
        self.assertEqual(one, traffic.build("smoke318", 1))
        self.assertEqual(two, traffic.build("smoke318", 2))
        self.assertNotEqual(one["day_seed"], two["day_seed"])
        self.assertEqual(one["logical_business_date"], "2026-11-18")
        self.assertEqual(two["logical_business_date"], "2026-11-19")
        ids1 = {s["merchant_payment_id"] for s in one["slots"]}
        ids2 = {s["merchant_payment_id"] for s in two["slots"]}
        self.assertEqual(len(ids1), 100)
        self.assertEqual(len(ids2), 100)
        self.assertFalse(ids1 & ids2)
        self.assertEqual(len({s["merchant_order_id"] for p in (one, two) for s in p["slots"]}), 200)
        operations = [a["body"]["operationId"] for p in (one, two) for s in p["slots"] for a in s["mutations"]]
        keys = [a["body"]["idempotencyKey"] for p in (one, two) for s in p["slots"] for a in s["mutations"]]
        self.assertEqual(len(operations), len(set(operations)))
        self.assertEqual(len(keys), len(set(keys)))

    def test_merchant_api_contract_and_terminal_money_geometry(self):
        result = traffic.build("smoke318", 1)
        self.assertEqual([x["path"] for x in result["merchant_setup"]], ["/merchants", "/customers"])
        for slot in result["slots"]:
            prefix = f"smoke318-d000001-s{slot['slot']:03d}"
            self.assertEqual(slot["merchant_payment_id"], f"p-{prefix}")
            self.assertEqual(slot["create_order"]["body"]["merchantOrderId"], slot["merchant_order_id"])
            self.assertEqual(slot["prepare_payment"]["body"]["merchantPaymentId"], slot["merchant_payment_id"])
            self.assertEqual(slot["create_order"]["body"]["amountMinor"], 1000)
            self.assertEqual(slot["prepare_payment"]["body"]["currency"], "USD")
            self.assertEqual(slot["slot"], int(prefix[-3:]))
            self.assertTrue(slot["prepare_payment"]["body"]["paymentMethodToken"].startswith("tok-"))
            for a in slot["mutations"]:
                self.assertEqual(a["method"], "POST")
                self.assertTrue(a["path"].startswith(f"/payments/{slot['merchant_payment_id']}/"))
                self.assertEqual(set(a["body"]), {"operationId", "amountMinor", "idempotencyKey"})
                self.assertGreater(a["body"]["amountMinor"], 0)

    def test_per_lifecycle_mutation_sequence_matches_qualified_merchant_endpoints(self):
        result = traffic.build("smoke318", 1)
        expected = {
            "auth_capture_purchase": [("auth-capture", 1000)],
            "authorize_capture": [("authorize", 1000), ("capture", 600), ("capture", 400)],
            "authorize_cancel": [("authorize", 1000), ("cancel", 1000)],
            "auth_capture_refund": [("auth-capture", 1000), ("refund", 250), ("refund", 750)],
            "fault_overlay": [("authorize", 1000)],
        }
        for slot in result["slots"]:
            self.assertEqual(
                [(a["path"].rsplit("/", 1)[-1], a["body"]["amountMinor"]) for a in slot["mutations"]],
                expected[slot["kind"]],
            )
            self.assertEqual(slot["expected_normal_terminal_state"], {
                "auth_capture_purchase": "PAID",
                "authorize_capture": "PAID",
                "authorize_cancel": "CANCELLED",
                "auth_capture_refund": "REFUNDED",
                "fault_overlay": None,
            }[slot["kind"]])
            self.assertEqual(slot["fault_class"] is None, slot["kind"] != "fault_overlay")

    def test_fixed_seed_json_is_identical_across_new_python_process(self):
        expected = traffic.build("smoke318", 2)
        cmd = [sys.executable, str(HERE / "campaign-traffic-plan.py"), "--campaign-id", "smoke318", "--day", "2"]
        a = subprocess.run(cmd, text=True, capture_output=True, check=False)
        b = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertEqual(a.returncode, 0)
        self.assertEqual(b.returncode, 0)
        self.assertEqual(a.stdout, b.stdout)
        self.assertEqual(json.loads(a.stdout), expected)
        self.assertEqual(
            hashlib.sha256(a.stdout.encode()).hexdigest(),
            hashlib.sha256(b.stdout.encode()).hexdigest()
        )

    def test_invalid_campaign_and_day_block_without_side_effects(self):
        for campaign_id, day in (
            ("../../x", 1), ("", 1), ("valid", 0), ("valid", -1),
            ("valid", 100001), ("valid", True),
        ):
            with self.subTest(campaign_id=campaign_id, day=day):
                with self.assertRaises((ValueError, KeyError)):
                    traffic.build(campaign_id, day)
        r = subprocess.run([
            sys.executable, str(HERE / "campaign-traffic-plan.py"),
            "--campaign-id", "../unsafe", "--day", "1"
        ], text=True, capture_output=True, check=False)
        self.assertEqual(r.returncode, 75)
        self.assertEqual(json.loads(r.stderr)["decision"], "BLOCK")

    def test_only_planner_pure_functions_no_runtime_network(self):
        a = traffic.build("smoke318", 1)
        self.assertEqual(a["plan_contract_sha256"], traffic.evidence.sha256(traffic.evidence.CONTRACT_PATH))
        self.assertEqual(tuple(a["fault_counts"]), traffic.fault_catalog.classes())
        self.assertEqual(a["fault_catalog_sha256"], traffic.fault_catalog.catalog_sha256())
        self.assertEqual([r["slot"] for r in a["slots"]], list(range(1, 101)))
        self.assertEqual({row["kind"] for row in a["slots"]}, set(traffic.KINDS))
        self.assertEqual(sum(1 for row in a["slots"] if row["fault_class"]), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
