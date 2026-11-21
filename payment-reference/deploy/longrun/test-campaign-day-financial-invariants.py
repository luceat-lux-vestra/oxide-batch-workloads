#!/usr/bin/env python3
"""Offline #318 day financial-invariants collector/retention tests."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import campaign_day_cardinality as cardinality
import campaign_day_financial_invariants as financial_day


SOURCE_REVISION = "a" * 40


def fixture_views(plan):
    merchants = {}
    gateways = {}
    killbills = {}
    providers = {}
    for item in plan["slots"]:
        payment_id = item["merchant_payment_id"]
        order_id = item["merchant_order_id"]
        provider_id = "provider-" + payment_id
        killbill_id = "kb-" + payment_id
        expected = cardinality.expected(item)

        if item["kind"] == "fault_overlay":
            truth = financial_day.financial.fault_catalog.end_of_day_provider_truth(
                item["fault_class"]
            )
            payment_state = "AUTHORIZED" if truth == "AUTHORIZED" else "PAYMENT_FAILED"
            operation_state = "SUCCEEDED" if truth == "AUTHORIZED" else "FAILED"
            transactions = [{
                "transactionType": "AUTHORIZE",
                "transactionStatus": "SUCCESS" if truth == "AUTHORIZED" else "PAYMENT_FAILURE",
            }]
        else:
            payment_state, _, transaction_counts, _, _ = financial_day.financial.EXPECTED[
                item["kind"]
            ]
            operation_state = "SUCCEEDED"
            transactions = [
                {"transactionType": transaction_type, "transactionStatus": "SUCCESS"}
                for transaction_type, count in transaction_counts.items()
                for _ in range(count)
            ]

        merchants[payment_id] = {
            "merchantPaymentId": payment_id,
            "merchantOrderId": order_id,
            "amountMinor": cardinality.traffic.AMOUNT_MINOR,
            "currency": cardinality.traffic.CURRENCY,
            "paymentState": payment_state,
        }
        gateways[payment_id] = {
            "merchantPaymentId": payment_id,
            "paymentState": payment_state,
            "lastOperationState": operation_state,
            "mapping": {
                "merchantPaymentId": payment_id,
                "killbillPaymentId": killbill_id,
                "providerPaymentId": provider_id,
            },
        }
        killbills[killbill_id] = {
            "paymentId": killbill_id,
            "transactions": transactions,
        }
        providers[provider_id] = {
            "id": provider_id,
            "amount": cardinality.traffic.AMOUNT_MINOR,
            "currency": cardinality.traffic.CURRENCY,
            "status": expected["provider_status"],
            "authorized_amount": expected["authorized_amount_minor"],
            "captured_amount": expected["captured_amount_minor"],
            "refunded_amount": expected["refunded_amount_minor"],
            "version": item["slot"],
        }
    return merchants, gateways, killbills, providers


class DayFinancialInvariantCollectorTests(unittest.TestCase):
    def test_collect_reads_all_100_slots_and_validates(self):
        plan = cardinality.plan_for("test318", 1)
        merchants, gateways, killbills, providers = fixture_views(plan)
        calls = {"merchant": 0, "gateway": 0, "killbill": 0, "provider": 0}

        def read(table, key, name):
            calls[name] += 1
            return copy.deepcopy(table[key])

        artifact = financial_day.collect(
            plan=plan,
            source_revision=SOURCE_REVISION,
            merchant_read=lambda key: read(merchants, key, "merchant"),
            gateway_read=lambda key: read(gateways, key, "gateway"),
            killbill_read=lambda key: read(killbills, key, "killbill"),
            provider_read=lambda key: read(providers, key, "provider"),
        )
        subject = financial_day.validate(
            artifact,
            plan=plan,
            source_revision=SOURCE_REVISION,
        )

        self.assertEqual(calls, {
            "merchant": 100,
            "gateway": 100,
            "killbill": 100,
            "provider": 100,
        })
        self.assertEqual(subject["observed_slot_count"], 100)
        self.assertEqual(subject["normal_slot_count"], 90)
        self.assertEqual(subject["fault_slot_count"], 10)
        self.assertEqual(subject["settlement_eligible_count"], 80)
        self.assertEqual(
            subject["net_settlement_total_minor"],
            artifact["net_settlement_total_minor"],
        )

    def test_collect_fails_closed_on_live_provider_drift(self):
        plan = cardinality.plan_for("test318", 1)
        merchants, gateways, killbills, providers = fixture_views(plan)
        first = plan["slots"][0]["merchant_payment_id"]
        provider_id = gateways[first]["mapping"]["providerPaymentId"]
        providers[provider_id]["status"] = "failed"

        with self.assertRaisesRegex(
            financial_day.FinancialInvariantError,
            "cross-system invariant failed",
        ):
            financial_day.collect(
                plan=plan,
                source_revision=SOURCE_REVISION,
                merchant_read=lambda key: copy.deepcopy(merchants[key]),
                gateway_read=lambda key: copy.deepcopy(gateways[key]),
                killbill_read=lambda key: copy.deepcopy(killbills[key]),
                provider_read=lambda key: copy.deepcopy(providers[key]),
            )

    def test_immutable_retention_allows_exact_replay_only(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "days" / "day-000001.financial-invariants.verification.json"
            value = {"proof": "value"}
            self.assertEqual(financial_day.retain_immutable(output, value), "created")
            self.assertEqual(financial_day.retain_immutable(output, value), "replay")
            with self.assertRaisesRegex(
                financial_day.FinancialInvariantError,
                "immutable financial invariant verification conflict",
            ):
                financial_day.retain_immutable(output, {"proof": "different"})

    def test_live_readers_use_only_read_helpers_and_environment_credentials(self):
        env = {
            "KILLBILL_USERNAME": "user",
            "KILLBILL_PASSWORD": "password",
            "KILLBILL_API_KEY": "api-key",
            "KILLBILL_API_SECRET": "api-secret",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            financial_day.financial,
            "local_json",
            side_effect=lambda base, path, headers=None: {
                "base": base,
                "path": path,
                "headers": headers,
            },
        ) as local_json, patch.object(
            financial_day.financial,
            "provider_json",
            return_value={"id": "provider-payment"},
        ) as provider_json:
            merchant, gateway, killbill, provider = financial_day.live_readers(
                merchant_url="http://127.0.0.1:18083",
                gateway_url="http://127.0.0.1:18082",
                killbill_url="http://127.0.0.1:8080",
            )
            merchant_result = merchant("merchant-payment")
            gateway_result = gateway("merchant-payment")
            killbill_result = killbill("killbill-payment")
            provider_result = provider("provider-payment")

        self.assertEqual(merchant_result["path"], "/payments/merchant-payment")
        self.assertEqual(gateway_result["path"], "/payments/merchant-payment")
        self.assertEqual(
            killbill_result["path"],
            "/1.0/kb/payments/killbill-payment/?withPluginInfo=true",
        )
        self.assertIn("Authorization", killbill_result["headers"])
        self.assertEqual(provider_result, {"id": "provider-payment"})
        self.assertEqual(local_json.call_count, 3)
        provider_json.assert_called_once_with("provider-payment")


if __name__ == "__main__":
    unittest.main(verbosity=2)
