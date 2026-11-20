#!/usr/bin/env python3
"""Offline #318 provider timeline/cardinality authority tests."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("provider_cardinality", HERE / "campaign-provider-cardinality.py")
assert spec is not None and spec.loader is not None
cardinality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cardinality)


def timeline(version=4, status="captured", authorized=1000, captured=1000, refunded=0):
    types = [
        "payment.created",
        "payment.authorized",
        "payment.captured",
        "payment.captured",
        "payment.refunded",
        "payment.refunded",
    ]
    events = []
    for index in range(1, version + 1):
        event_type = types[index - 1]
        previous_type = types[index - 2] if index > 1 else None
        events.append({
            "id": f"provider-318:{event_type}:{index}",
            "aggregate_id": "provider-318",
            "type": event_type,
            "occurred_at": f"2026-11-20T00:00:0{index}Z",
            "aggregate_version": index,
            "correlation_id": f"corr-{index}",
            "causation_id": "" if index == 1 else f"provider-318:{previous_type}:{index - 1}",
            "runtime_sequence": 100 + index,
        })
    return {
        "payment": {
            "id": "provider-318",
            "amount": 1000,
            "currency": "USD",
            "status": status,
            "authorized_amount": authorized,
            "captured_amount": captured,
            "refunded_amount": refunded,
            "version": version,
        },
        "events": events,
    }


class ProviderCardinalityTests(unittest.TestCase):
    def test_contiguous_immutable_timeline_is_provider_operation_ledger(self):
        result = cardinality.verify_cardinality(
            provider_payment_id="provider-318",
            timeline=timeline(),
            expected_effective_mutations=3,
            expected_status="captured",
            expected_authorized=1000,
            expected_captured=1000,
            expected_refunded=0,
        )
        self.assertEqual(result["decision"], "PROVIDER_EFFECT_CARDINALITY_VERIFIED_NOT_CAMPAIGN_ACCEPTED")
        self.assertEqual(result["authority"], cardinality.AUTHORITY)
        self.assertEqual(result["authority_source_revision"], cardinality.PAYMENT_SANDBOX_SOURCE_REVISION)
        self.assertTrue(result["provider_exists"])
        self.assertEqual(result["provider_version"], 4)
        self.assertEqual(result["creation_event_count"], 1)
        self.assertEqual(result["effective_mutation_count"], 3)
        self.assertEqual(len(result["timeline_sha256"]), 64)
        self.assertEqual(result["request_attempt_cardinality_authority"], "SEPARATE_EXECUTION_EVIDENCE_REQUIRED")

    def test_replay_same_timeline_has_identical_digest_and_no_extra_effect(self):
        value = timeline(version=2, status="authorized", authorized=1000, captured=0)
        before = cardinality.provider_ledger("provider-318", value)
        after = cardinality.provider_ledger("provider-318", copy.deepcopy(value))
        self.assertEqual(before["timeline_sha256"], after["timeline_sha256"])
        self.assertEqual(before["effective_mutation_count"], 1)
        self.assertEqual(after["effective_mutation_count"], 1)

    def test_cardinality_and_provider_contract_corruption_fail_closed(self):
        cases = []
        cases.append(("count", timeline(), {"expected_effective_mutations": 2}))

        bad = timeline(); bad["events"][2]["aggregate_version"] = 2
        cases.append(("aggregate-gap", bad, {}))

        bad = timeline(); bad["events"][2]["id"] = bad["events"][1]["id"]
        cases.append(("event-id", bad, {}))

        bad = timeline(); bad["events"][3]["runtime_sequence"] = bad["events"][2]["runtime_sequence"]
        cases.append(("runtime-sequence", bad, {}))

        bad = timeline(); bad["payment"]["id"] = "other"
        cases.append(("payment-id", bad, {}))

        bad = timeline(); bad["payment"]["captured_amount"] = 900
        cases.append(("financial", bad, {}))

        bad = timeline(); bad["events"][0]["type"] = "payment.authorized"
        cases.append(("creation-event", bad, {}))

        bad = timeline(); bad["events"][2]["type"] = "payment.created"
        cases.append(("duplicate-create", bad, {}))

        bad = timeline(); bad["events"][2]["causation_id"] = bad["events"][0]["id"]
        cases.append(("causation-chain", bad, {}))

        bad = timeline(); bad["events"][-1]["type"] = "payment.authorized"
        cases.append(("final-event", bad, {}))

        bad = timeline(); bad["events"][1]["type"] = "payment.unknown"
        cases.append(("event-type", bad, {}))

        bad = timeline(); bad["events"][1]["correlation_id"] = ""
        cases.append(("correlation", bad, {}))

        bad = timeline(); bad["unexpected"] = True
        cases.append(("timeline-schema", bad, {}))

        for name, value, override in cases:
            with self.subTest(name=name), self.assertRaises(cardinality.Block):
                kwargs = {
                    "provider_payment_id": "provider-318",
                    "timeline": value,
                    "expected_effective_mutations": 3,
                    "expected_status": "captured",
                    "expected_authorized": 1000,
                    "expected_captured": 1000,
                    "expected_refunded": 0,
                }
                kwargs.update(override)
                cardinality.verify_cardinality(**kwargs)

    def test_provider_lifecycle_state_invariants_are_pinned(self):
        valid = (
            timeline(version=1, status="pending", authorized=0, captured=0, refunded=0),
            timeline(version=2, status="authorized", authorized=1000, captured=0, refunded=0),
            timeline(version=4, status="captured", authorized=1000, captured=1000, refunded=0),
            timeline(version=6, status="refunded", authorized=1000, captured=1000, refunded=1000),
        )
        for value in valid:
            cardinality.provider_ledger("provider-318", value)

        cancelled = timeline(version=3, status="cancelled", authorized=1000, captured=0, refunded=0)
        cancelled["events"][2]["type"] = "payment.cancelled"
        cancelled["events"][2]["id"] = "provider-318:payment.cancelled:3"
        cardinality.provider_ledger("provider-318", cancelled)

        invalid = timeline(version=2, status="authorized", authorized=999, captured=0, refunded=0)
        with self.assertRaises(cardinality.Block):
            cardinality.provider_ledger("provider-318", invalid)

    def test_absent_provider_is_explicit_zero_effect_authority(self):
        result = cardinality.verify_absent("provider-318", 404)
        self.assertEqual(result["authority_source_revision"], cardinality.PAYMENT_SANDBOX_SOURCE_REVISION)
        self.assertFalse(result["provider_exists"])
        self.assertIsNone(result["provider_version"])
        self.assertEqual(result["creation_event_count"], 0)
        self.assertEqual(result["effective_mutation_count"], 0)
        self.assertEqual(result["payment_status"], "ABSENT")
        self.assertIsNone(result["timeline_sha256"])
        for status in (200, 400, 500):
            with self.subTest(status=status), self.assertRaises(cardinality.Block):
                cardinality.verify_absent("provider-318", status)

    def test_safe_identity_and_boolean_integer_rejections(self):
        with self.assertRaises(cardinality.Block):
            cardinality.provider_ledger("../unsafe", timeline())
        bad = timeline()
        bad["payment"]["version"] = True
        with self.assertRaises(cardinality.Block):
            cardinality.provider_ledger("provider-318", bad)

    def test_live_collectors_use_provider_service_admin_timeline_without_literal_secret(self):
        payload = timeline(version=2, status="authorized", authorized=1000, captured=0)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload).encode(), stderr=b"",
        )
        with patch.object(cardinality.subprocess, "run", return_value=completed) as run:
            observed = cardinality.provider_timeline("provider-318")
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["docker", "compose"])
        self.assertIn("exec", command)
        self.assertIn("-T", command)
        self.assertIn("payment-sandbox", command)
        self.assertIn("/admin/payments/$1/timeline", command[-3])
        self.assertIn("PAYMENT_SANDBOX_ADMIN_TOKEN", command[-3])
        self.assertNotIn("payment-reference-local-only", " ".join(command))
        self.assertNotIn("oxide-payment-reference-payment-sandbox-1", " ".join(command))
        self.assertEqual(observed["payment"]["id"], "provider-318")

        status_completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"404", stderr=b"",
        )
        with patch.object(cardinality.subprocess, "run", return_value=status_completed) as status_run:
            status = cardinality.provider_timeline_status("provider-318")
        status_command = status_run.call_args.args[0]
        self.assertEqual(status, 404)
        self.assertIn("payment-sandbox", status_command)
        self.assertIn("/admin/payments/$1/timeline", status_command[-3])
        self.assertIn("PAYMENT_SANDBOX_ADMIN_TOKEN", status_command[-3])
        self.assertNotIn("payment-reference-local-only", " ".join(status_command))


if __name__ == "__main__":
    unittest.main(verbosity=2)
