#!/usr/bin/env python3
"""Offline strict financial consistency contract tests — no online payment writes."""
import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("payment_reference_financial", HERE / "campaign-financial-consistency.py")
assert spec is not None and spec.loader is not None
financial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(financial)


def fixture(case):
    state, status, tx_counts, captured, refunded = financial.EXPECTED[case]
    merchant = {
        "merchantPaymentId": "payment-318", "merchantOrderId": "order-318",
        "amountMinor": 1000, "currency": "USD", "paymentState": state,
    }
    gateway = {
        "merchantPaymentId": "payment-318", "paymentState": state,
        "lastOperationState": "SUCCEEDED", "mapping": {
            "merchantPaymentId": "payment-318",
            "killbillPaymentId": "kb-318",
            "providerPaymentId": "provider-318",
        },
    }
    provider = {
        "status": status, "authorized_amount": 1000,
        "captured_amount": captured, "refunded_amount": refunded,
    }
    kb = {"paymentId": "kb-318", "transactions": [
        {"transactionType": tx, "transactionStatus": "SUCCESS"}
        for tx, count in tx_counts.items()
        for _ in range(count)
    ]}
    return merchant, gateway, provider, kb


def check(case, values):
    a, b, c, d = values
    return financial.canonical_consistency(
        payment_id="payment-318", order_id="order-318", case=case,
        merchant=a, gateway=b, provider=c, killbill=d,
    )


class FinancialConsistencyTests(unittest.TestCase):
    def test_all_four_normal_financial_cases_are_partial_only(self):
        for case in sorted(financial.KINDS):
            with self.subTest(case=case):
                summary = check(case, fixture(case))
                self.assertEqual(summary["decision"], "CROSS_SYSTEM_CONSISTENT_NOT_CAMPAIGN_ACCEPTED")
                self.assertEqual(summary["schema"], financial.SCHEMA)
                self.assertEqual(summary["captured_amount_minor"] - summary["refunded_amount_minor"], summary["net_settlement_amount_minor"])
                self.assertEqual(len(summary["unverified"]), 5)
                self.assertIn("provider_mutation_side_effect_cardinality", summary["unverified"])
                self.assertIn("provider_settlement_statement", summary["unverified"])

    def test_merchant_identity_state_amount_currency_rejections(self):
        for field, new_value in [
            ("merchantPaymentId", "other"), ("merchantOrderId", "other"),
            ("paymentState", "PAYMENT_PENDING"), ("amountMinor", 999),
            ("amountMinor", True), ("currency", "KRW"),
        ]:
            with self.subTest(field=field, value=new_value):
                witness = fixture("auth_capture_purchase")
                witness[0][field] = new_value
                with self.assertRaises(financial.Block):
                    check("auth_capture_purchase", witness)

    def test_gateway_mapping_and_pending_rejections(self):
        for section, field, value in [
            ("gateway", "paymentState", "REFUNDED"),
            ("gateway", "lastOperationState", "RETRY_PENDING"),
            ("gateway", "merchantPaymentId", "different"),
            ("mapping", "merchantPaymentId", "different"),
            ("mapping", "killbillPaymentId", None),
            ("mapping", "providerPaymentId", "../../tmp"),
        ]:
            with self.subTest(section=section, field=field):
                witness = fixture("auth_capture_purchase")
                obj = witness[1] if section == "gateway" else witness[1]["mapping"]
                obj[field] = value
                with self.assertRaises(financial.Block):
                    check("auth_capture_purchase", witness)

    def test_provider_financial_truth_invariant_and_exact_amounts(self):
        for field, value in [
            ("status", "authorized"), ("authorized_amount", 999),
            ("captured_amount", 800), ("refunded_amount", 999),
            ("authorized_amount", True),
        ]:
            with self.subTest(field=field, value=value):
                witness = fixture("auth_capture_purchase")
                witness[2][field] = value
                with self.assertRaises(financial.Block):
                    check("auth_capture_purchase", witness)
        cancellation = fixture("authorize_cancel")
        cancellation[2]["captured_amount"] = 1
        with self.assertRaises(financial.Block):
            check("authorize_cancel", cancellation)

    def test_killbill_duplicate_missing_or_unsuccessful_transaction_rejected(self):
        for problem in ("duplicate", "missing", "wrong-type", "unknown", "payment-id"):
            with self.subTest(problem=problem):
                witness = fixture("authorize_capture")
                kb = witness[3]
                if problem == "duplicate":
                    kb["transactions"].append(copy.deepcopy(kb["transactions"][0]))
                elif problem == "missing":
                    kb["transactions"].pop()
                elif problem == "wrong-type":
                    kb["transactions"][0]["transactionType"] = "REFUND"
                elif problem == "unknown":
                    kb["transactions"][0]["transactionStatus"] = "UNKNOWN"
                else:
                    kb["paymentId"] = "other"
                with self.assertRaises(financial.Block):
                    check("authorize_capture", witness)

    def test_ambiguous_cases_and_unsafe_ids_are_blocked(self):
        witness = fixture("auth_capture_purchase")
        for case in ("fault_overlay", "authorize_partial", ""):
            with self.subTest(case=case), self.assertRaises(financial.Block):
                check(case, witness)
        with self.assertRaises(financial.Block):
            financial.canonical_consistency(
                payment_id="../unsafe", order_id="order-318",
                case="auth_capture_purchase", merchant=witness[0], gateway=witness[1],
                provider=witness[2], killbill=witness[3],
            )

    def test_loopback_only_no_proxy_or_external_urls(self):
        self.assertEqual(financial.local_base("http://127.0.0.1:18083"), "http://127.0.0.1:18083")
        for bad in (
            "http://localhost:18083", "https://127.0.0.1:18083",
            "http://127.0.0.1:12345", "http://127.0.0.1:18083/special",
            "http://user:password@127.0.0.1:18083",
            "http://10.0.0.2:18083", "http://127.0.0.1:18083/?q=test"
        ):
            with self.subTest(url=bad), self.assertRaises(financial.Block):
                financial.local_base(bad)

    def test_collector_uses_only_canonical_local_gets(self):
        merchant, gateway, provider, killbill = fixture("auth_capture_purchase")
        reads = []
        def fake_local(base, path, headers=None):
            reads.append((base, path))
            if base.endswith(":18083"):
                return merchant
            if base.endswith(":18082"):
                return gateway
            if base.endswith(":8080"):
                self.assertEqual(path, "/1.0/kb/payments/kb-318/?withPluginInfo=true")
                self.assertIn("X-Killbill-ApiKey", headers)
                return killbill
            raise AssertionError("external endpoint")
        with patch.object(financial, "local_json", side_effect=fake_local):
            with patch.object(financial, "provider_json", return_value=provider) as pg:
                with patch.dict("os.environ", {
                    "KILLBILL_USERNAME": "admin", "KILLBILL_PASSWORD": "not-printed",
                    "KILLBILL_API_KEY": "hostq-318", "KILLBILL_API_SECRET": "not-printed",
                }):
                    result = financial.collect_and_verify(
                        "payment-318", "order-318", "auth_capture_purchase",
                        "http://127.0.0.1:18083",
                        "http://127.0.0.1:18082",
                        "http://127.0.0.1:8080",
                    )
        self.assertEqual(result["decision"], "CROSS_SYSTEM_CONSISTENT_NOT_CAMPAIGN_ACCEPTED")
        self.assertEqual(len(reads), 3)
        self.assertEqual(reads[0], ("http://127.0.0.1:18083", "/payments/payment-318"))
        self.assertEqual(reads[1], ("http://127.0.0.1:18082", "/payments/payment-318"))
        pg.assert_called_once_with("provider-318")
        self.assertNotIn("not-printed", str(result))

    def test_missing_credential_blocks_without_network_mutation(self):
        merchant, gateway, _, _ = fixture("auth_capture_purchase")
        def fake_get(base, *_args, **_kwargs):
            return merchant if base.endswith(":18083") else gateway
        with patch.object(financial, "local_json", side_effect=fake_get):
            with patch.dict("os.environ", {
                "KILLBILL_USERNAME": "", "KILLBILL_PASSWORD": "",
                "KILLBILL_API_KEY": "", "KILLBILL_API_SECRET": "",
            }):
                with self.assertRaises(financial.Block):
                    financial.collect_and_verify(
                        "payment-318", "order-318", "auth_capture_purchase",
                        "http://127.0.0.1:18083", "http://127.0.0.1:18082", "http://127.0.0.1:8080",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
