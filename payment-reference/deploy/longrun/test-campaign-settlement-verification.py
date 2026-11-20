#!/usr/bin/env python3
"""Offline tests for #318 settlement statement + Merchant close verification."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "settlement_verification",
    HERE / "campaign-settlement-verification.py",
)
assert SPEC is not None and SPEC.loader is not None
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def campaign_inputs():
    plan = m.roster_builder.traffic.build("campaign-settlement-verification-test", 1)
    by_payment = {item["merchant_payment_id"]: item for item in plan["slots"]}
    provider_to_item = {
        f"prov-{item['slot']:03d}": item
        for item in plan["slots"]
    }

    def gateway_read(payment_id):
        item = by_payment[payment_id]
        provider_id = f"prov-{item['slot']:03d}"
        return {
            "merchantPaymentId": payment_id,
            "paymentState": (
                "PAYMENT_UNKNOWN"
                if item["kind"] == "fault_overlay"
                else item["expected_normal_terminal_state"]
            ),
            "lastOperationState": (
                "UNKNOWN" if item["kind"] == "fault_overlay" else "SUCCEEDED"
            ),
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
        expected = m.roster_builder.expected_provider_status(item)
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
            "version": 3,
        }

    roster, roster_verification = m.roster_builder.collect(
        plan=plan,
        source_revision="a" * 40,
        provider_account_id="payment-sandbox-default",
        gateway_read=gateway_read,
        provider_read=provider_read,
    )

    provider_payloads = {}
    for observation in roster_verification["observations"]:
        if not observation["settlement_eligible"]:
            continue
        provider_id = observation["provider_payment_id"]
        provider_payloads[provider_id] = {
            "id": provider_id,
            "currency": "USD",
            "status": observation["provider_status"],
            "captured_amount": observation["captured_amount_minor"],
            "refunded_amount": observation["refunded_amount_minor"],
            "version": observation["provider_version"],
        }

    def fetch_provider(_base, payment_id, _timeout):
        return copy.deepcopy(provider_payloads[payment_id])

    with patch.object(m.statement, "fetch_provider_payment", side_effect=fetch_provider):
        provider_statement = m.statement.produce_statement(
            roster,
            "http://127.0.0.1:18084",
            2.0,
        )
    m.statement.validate_statement(provider_statement, roster)
    return plan, roster, roster_verification, provider_statement


class FakeMerchant:
    def __init__(self):
        self.row = None
        self.event_count = 0
        self.drop_after_first_commit = False
        self.dropped = False

    def snapshot(self, _request):
        return {
            "rows": [] if self.row is None else [copy.deepcopy(self.row)],
            "event_count": self.event_count,
        }

    def post(self, request):
        if self.row is None:
            self.row = m.expected_persisted_row(request)
            self.event_count = 1
            if self.drop_after_first_commit and not self.dropped:
                self.dropped = True
                raise urllib.error.URLError("simulated post-commit response loss")
            return 201, {**copy.deepcopy(self.row), "replayed": False}

        if self.row == m.expected_persisted_row(request):
            return 200, {**copy.deepcopy(self.row), "replayed": True}

        return 409, {
            "error": "statement identity already exists with different digest or totals"
        }


class SettlementVerificationTests(unittest.TestCase):
    def setUp(self):
        (
            self.plan,
            self.roster,
            self.roster_verification,
            self.provider_statement,
        ) = campaign_inputs()

    def verification(self, proof):
        return m.build_verification(
            campaign_id=self.plan["campaign_id"],
            logical_day_sequence=self.plan["logical_day_sequence"],
            source_revision=self.roster["source_revision"],
            roster=self.roster,
            roster_verification=self.roster_verification,
            provider_statement=self.provider_statement,
            merchant_close_proof=proof,
        )

    def test_close_create_replay_conflicts_and_rerun_are_stable(self):
        merchant = FakeMerchant()
        first_proof = m.prove_merchant_close(
            self.provider_statement,
            post=merchant.post,
            snapshot=merchant.snapshot,
        )
        first = self.verification(first_proof)

        second_proof = m.prove_merchant_close(
            self.provider_statement,
            post=merchant.post,
            snapshot=merchant.snapshot,
        )
        second = self.verification(second_proof)

        self.assertEqual(first, second)
        self.assertEqual(first["decision"], m.DECISION)
        self.assertEqual(first["merchant_close"]["business_event_count"], 1)
        self.assertEqual(first["merchant_close"]["exact_replay_http_status"], 200)
        self.assertEqual(first["merchant_close"]["digest_conflict_http_status"], 409)
        self.assertEqual(first["merchant_close"]["totals_conflict_http_status"], 409)
        self.assertTrue(first["merchant_close"]["authoritative_state_unchanged_after_conflicts"])
        self.assertEqual(
            first["statement"]["source_roster_digest"],
            first["roster_sha256"],
        )

    def test_post_commit_response_loss_reconciles_from_db_on_next_run(self):
        merchant = FakeMerchant()
        merchant.drop_after_first_commit = True
        with self.assertRaises(urllib.error.URLError):
            m.prove_merchant_close(
                self.provider_statement,
                post=merchant.post,
                snapshot=merchant.snapshot,
            )
        self.assertIsNotNone(merchant.row)
        self.assertEqual(merchant.event_count, 1)

        proof = m.prove_merchant_close(
            self.provider_statement,
            post=merchant.post,
            snapshot=merchant.snapshot,
        )
        verification = self.verification(proof)
        self.assertEqual(verification["merchant_close"]["business_event_count"], 1)

    def test_preexisting_conflict_or_duplicate_event_blocks_before_mutation(self):
        request = m.close_request(self.provider_statement)
        merchant = FakeMerchant()
        merchant.row = m.expected_persisted_row(request)
        merchant.row["statement_digest"] = m.conflict_digest(request["statement_digest"])
        merchant.event_count = 1
        with self.assertRaisesRegex(m.SettlementVerificationError, "differs"):
            m.prove_merchant_close(
                self.provider_statement,
                post=merchant.post,
                snapshot=merchant.snapshot,
            )

        merchant = FakeMerchant()
        merchant.row = m.expected_persisted_row(request)
        merchant.event_count = 2
        with self.assertRaisesRegex(m.SettlementVerificationError, "exactly one"):
            m.prove_merchant_close(
                self.provider_statement,
                post=merchant.post,
                snapshot=merchant.snapshot,
            )

    def test_roster_verification_and_statement_binding_fail_closed(self):
        merchant = FakeMerchant()
        proof = m.prove_merchant_close(
            self.provider_statement,
            post=merchant.post,
            snapshot=merchant.snapshot,
        )

        bad_roster_verification = copy.deepcopy(self.roster_verification)
        bad_roster_verification["source_revision"] = "b" * 40
        with self.assertRaisesRegex(m.SettlementVerificationError, "identity"):
            m.build_verification(
                campaign_id=self.plan["campaign_id"],
                logical_day_sequence=1,
                source_revision=self.roster["source_revision"],
                roster=self.roster,
                roster_verification=bad_roster_verification,
                provider_statement=self.provider_statement,
                merchant_close_proof=proof,
            )

        bad_statement = copy.deepcopy(self.provider_statement)
        bad_statement["source_roster_digest"] = "sha256:" + ("f" * 64)
        unsigned = dict(bad_statement)
        unsigned.pop("statement_digest")
        bad_statement["statement_digest"] = m.statement.sha256_digest(unsigned)
        with self.assertRaises(
            (m.SettlementVerificationError, m.statement.SettlementStatementError)
        ):
            m.build_verification(
                campaign_id=self.plan["campaign_id"],
                logical_day_sequence=1,
                source_revision=self.roster["source_revision"],
                roster=self.roster,
                roster_verification=self.roster_verification,
                provider_statement=bad_statement,
                merchant_close_proof=proof,
            )

    def test_verification_artifact_is_immutable_and_replay_safe(self):
        merchant = FakeMerchant()
        proof = m.prove_merchant_close(
            self.provider_statement,
            post=merchant.post,
            snapshot=merchant.snapshot,
        )
        verification = self.verification(proof)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settlement.verification.json"
            self.assertEqual(
                m.roster_builder.retain_immutable(path, verification),
                "created",
            )
            before = path.stat()
            self.assertEqual(
                m.roster_builder.retain_immutable(path, verification),
                "replay",
            )
            self.assertEqual(before.st_mtime_ns, path.stat().st_mtime_ns)

            conflict = copy.deepcopy(verification)
            conflict["merchant_close"]["business_event_count"] = 2
            with self.assertRaisesRegex(
                m.roster_builder.RosterError,
                "immutable content conflict",
            ):
                m.roster_builder.retain_immutable(path, conflict)

    def test_compose_snapshot_uses_merchant_db_service_not_container_name(self):
        with tempfile.TemporaryDirectory() as directory:
            compose = Path(directory) / "compose.yaml"
            env_file = Path(directory) / "images.env"
            compose.write_text("services: {}\n", encoding="utf-8")
            env_file.write_text("", encoding="utf-8")
            response = {
                "rows": [],
                "event_count": 0,
            }
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(json.dumps(response) + "\n").encode(),
                stderr=b"",
            )
            request = m.close_request(self.provider_statement)
            with patch.dict(
                os.environ,
                {
                    "PAYMENT_REFERENCE_COMPOSE_FILE": str(compose),
                    "PAYMENT_REFERENCE_COMPOSE_ENV_FILE": str(env_file),
                },
                clear=False,
            ), patch.object(m.subprocess, "run", return_value=completed) as run:
                observed = m.merchant_db_snapshot(request)
            command = run.call_args.args[0]
            self.assertEqual(observed, response)
            self.assertEqual(command[:2], ["docker", "compose"])
            self.assertIn("--env-file", command)
            self.assertIn("exec", command)
            self.assertIn("-T", command)
            self.assertIn("merchant-db", command)
            self.assertIn("-X", command)
            self.assertNotIn("payment-reference-merchant-db-1", " ".join(command))

    def test_artifact_paths_and_loopback_endpoints_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            m.require_distinct_paths(
                root / "roster.json",
                root / "roster-verification.json",
                root / "statement.json",
                root / "verification.json",
            )
            with self.assertRaisesRegex(m.SettlementVerificationError, "distinct"):
                m.require_distinct_paths(
                    root / "same.json",
                    root / "same.json",
                )

        self.assertEqual(
            m.loopback_base("http://127.0.0.1:18083", 18083, "Merchant"),
            "http://127.0.0.1:18083",
        )
        for url in (
            "http://localhost:18083",
            "http://127.0.0.1:18081",
            "https://127.0.0.1:18083",
        ):
            with self.subTest(url=url), self.assertRaises(m.roster_builder.RosterError):
                m.loopback_base(url, 18083, "Merchant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
