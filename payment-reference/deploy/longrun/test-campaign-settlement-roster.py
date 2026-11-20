#!/usr/bin/env python3
"""Offline #318 settlement roster builder tests."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("settlement_roster", HERE / "campaign-settlement-roster.py")
assert spec is not None and spec.loader is not None
roster_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roster_builder)


def readers(plan):
    by_payment = {item["merchant_payment_id"]: item for item in plan["slots"]}
    provider_to_item = {}
    for item in plan["slots"]:
        provider_to_item[f"prov-{item['slot']:03d}"] = item

    def gateway_read(payment_id):
        item = by_payment[payment_id]
        provider_id = f"prov-{item['slot']:03d}"
        return {
            "merchantPaymentId": payment_id,
            "paymentState": "PAYMENT_UNKNOWN" if item["kind"] == "fault_overlay" else item["expected_normal_terminal_state"],
            "lastOperationState": "UNKNOWN" if item["kind"] == "fault_overlay" else "SUCCEEDED",
            "mapping": {
                "merchantPaymentId": payment_id,
                "gatewayOperationId": f"gw-{item['slot']:03d}",
                "killbillPaymentId": f"kb-{item['slot']:03d}",
                "killbillTransactionId": f"tx-{item['slot']:03d}",
                "hyperswitchPaymentId": None,
                "hyperswitchAttemptId": None,
                "providerPaymentId": provider_id,
                "providerOperationId": None,
                "providerIdempotencyKey": None,
            },
        }

    def provider_read(provider_id):
        item = provider_to_item[provider_id]
        expected = roster_builder.expected_provider_status(item)
        if expected == "ABSENT":
            return 404, None
        captured = 1000 if expected in {"captured", "refunded"} else 0
        refunded = 1000 if expected == "refunded" else 0
        authorized = 1000 if expected in {"authorized", "cancelled", "captured", "refunded"} else 0
        return 200, {
            "id": provider_id,
            "amount": 1000,
            "currency": "USD",
            "status": expected,
            "authorized_amount": authorized,
            "captured_amount": captured,
            "refunded_amount": refunded,
            "version": 2,
        }

    return gateway_read, provider_read


class SettlementRosterBuilderTests(unittest.TestCase):
    def setUp(self):
        self.plan = roster_builder.traffic.build("campaign-roster-test", 1)
        self.source = "a" * 40
        self.gateway_read, self.provider_read = readers(self.plan)

    def build(self):
        return roster_builder.collect(
            plan=self.plan,
            source_revision=self.source,
            provider_account_id="payment-sandbox-default",
            gateway_read=self.gateway_read,
            provider_read=self.provider_read,
        )

    def test_complete_day_derives_exact_settlement_membership(self):
        roster, verification = self.build()
        self.assertEqual(verification["observed_slot_count"], 100)
        self.assertEqual(verification["eligible_payment_count"], 80)
        self.assertEqual(len(roster["provider_payment_ids"]), 80)
        self.assertEqual(roster["scenario_seed"], self.plan["day_seed"])
        self.assertEqual(roster["settlement_date"], self.plan["logical_business_date"])
        self.assertEqual(roster["source_revision"], self.source)
        roster_builder.statement.validate_roster(roster)
        roster_builder.validate_verification(verification, roster, self.plan)

        eligible_kinds = {
            row["kind"] for row in verification["observations"] if row["settlement_eligible"]
        }
        self.assertEqual(
            eligible_kinds,
            {"auth_capture_purchase", "authorize_capture", "auth_capture_refund"},
        )
        self.assertFalse(any(
            row["settlement_eligible"]
            for row in verification["observations"]
            if row["kind"] == "fault_overlay"
        ))

    def test_builder_roster_is_consumed_unchanged_by_statement_producer(self):
        roster, _ = self.build()

        def provider(_base, payment_id, _timeout):
            self.assertIn(payment_id, roster["provider_payment_ids"])
            return {
                "id": payment_id,
                "currency": "USD",
                "status": "captured",
                "captured_amount": 1000,
                "refunded_amount": 0,
                "version": 3,
            }

        with patch.object(roster_builder.statement, "fetch_provider_payment", side_effect=provider):
            statement = roster_builder.statement.produce_statement(
                roster,
                "http://127.0.0.1:18084",
                2.0,
            )
        self.assertEqual(statement["line_count"], 80)
        self.assertEqual(statement["source_roster_digest"], roster_builder.statement.sha256_digest(roster))
        roster_builder.statement.validate_statement(statement, roster)

    def test_provider_truth_mismatch_or_mapping_collision_fails_closed(self):
        target = next(item for item in self.plan["slots"] if item["kind"] != "fault_overlay")
        target_provider = f"prov-{target['slot']:03d}"

        def bad_provider(provider_id):
            status, payload = self.provider_read(provider_id)
            if provider_id == target_provider and status == 200:
                payload = copy.deepcopy(payload)
                payload["status"] = "authorized"
                payload["captured_amount"] = 0
            return status, payload

        with self.assertRaisesRegex(roster_builder.RosterError, "provider truth"):
            roster_builder.collect(
                plan=self.plan,
                source_revision=self.source,
                provider_account_id="payment-sandbox-default",
                gateway_read=self.gateway_read,
                provider_read=bad_provider,
            )

        first_payment = self.plan["slots"][0]["merchant_payment_id"]
        second_payment = self.plan["slots"][1]["merchant_payment_id"]
        first_provider_id = self.gateway_read(first_payment)["mapping"]["providerPaymentId"]

        def colliding_gateway(payment_id):
            value = copy.deepcopy(self.gateway_read(payment_id))
            if payment_id == second_payment:
                value["mapping"]["providerPaymentId"] = first_provider_id
            return value

        with self.assertRaisesRegex(roster_builder.RosterError, "mapping collision"):
            roster_builder.collect(
                plan=self.plan,
                source_revision=self.source,
                provider_account_id="payment-sandbox-default",
                gateway_read=colliding_gateway,
                provider_read=self.provider_read,
            )

    def test_normal_gateway_terminal_state_must_match_plan(self):
        target = next(item for item in self.plan["slots"] if item["kind"] != "fault_overlay")

        def bad_gateway(payment_id):
            value = copy.deepcopy(self.gateway_read(payment_id))
            if payment_id == target["merchant_payment_id"]:
                value["lastOperationState"] = "UNKNOWN"
            return value

        with self.assertRaisesRegex(roster_builder.RosterError, "Gateway normal operation"):
            roster_builder.collect(
                plan=self.plan,
                source_revision=self.source,
                provider_account_id="payment-sandbox-default",
                gateway_read=bad_gateway,
                provider_read=self.provider_read,
            )

    def test_pre_side_effect_fault_must_be_provider_absent(self):
        item = next(
            item for item in self.plan["slots"]
            if item["fault_class"] == "plan-503-before-side-effect"
        )
        provider_id = f"prov-{item['slot']:03d}"

        def present_provider(pid):
            if pid != provider_id:
                return self.provider_read(pid)
            return 200, {
                "id": pid,
                "amount": 1000,
                "currency": "USD",
                "status": "authorized",
                "authorized_amount": 1000,
                "captured_amount": 0,
                "refunded_amount": 0,
                "version": 2,
            }

        with self.assertRaisesRegex(roster_builder.RosterError, "provider truth"):
            roster_builder.collect(
                plan=self.plan,
                source_revision=self.source,
                provider_account_id="payment-sandbox-default",
                gateway_read=self.gateway_read,
                provider_read=present_provider,
            )

    def test_immutable_roster_and_verification_replay_without_rewrite(self):
        roster, verification = self.build()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roster_path = root / "roster.json"
            verification_path = root / "roster.verification.json"
            self.assertEqual(roster_builder.retain_immutable(roster_path, roster), "created")
            self.assertEqual(roster_builder.retain_immutable(verification_path, verification), "created")
            before_roster = roster_path.stat()
            before_verification = verification_path.stat()
            self.assertEqual(roster_builder.retain_immutable(roster_path, roster), "replay")
            self.assertEqual(roster_builder.retain_immutable(verification_path, verification), "replay")
            self.assertEqual(before_roster.st_mtime_ns, roster_path.stat().st_mtime_ns)
            self.assertEqual(before_verification.st_mtime_ns, verification_path.stat().st_mtime_ns)

            conflict = copy.deepcopy(roster)
            conflict["provider_payment_ids"] = conflict["provider_payment_ids"][:-1]
            with self.assertRaisesRegex(roster_builder.RosterError, "immutable content conflict"):
                roster_builder.retain_immutable(roster_path, conflict)

    def test_output_paths_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roster_builder.require_distinct_outputs(
                root / "roster.json",
                root / "verification.json",
            )
            with self.assertRaisesRegex(roster_builder.RosterError, "distinct paths"):
                roster_builder.require_distinct_outputs(
                    root / "same.json",
                    root / "same.json",
                )

    def test_loopback_role_ports_are_fail_closed(self):
        with patch.dict("os.environ", {}, clear=False):
            self.assertEqual(
                roster_builder.loopback_base("http://127.0.0.1:18082", 18082, "Gateway"),
                "http://127.0.0.1:18082",
            )
        for url in (
            "http://localhost:18082",
            "http://127.0.0.1:18081",
            "https://127.0.0.1:18082",
            "http://user:pass@127.0.0.1:18082",
        ):
            with self.subTest(url=url), self.assertRaises(roster_builder.RosterError):
                roster_builder.loopback_base(url, 18082, "Gateway")


if __name__ == "__main__":
    unittest.main(verbosity=2)
