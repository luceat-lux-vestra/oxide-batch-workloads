#!/usr/bin/env python3
"""Offline #318 reconciliation proof producer/retention tests."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import campaign_day_cardinality as cardinality
import campaign_day_reconciliation as reconciliation


SHA40 = "a" * 40


def producer_report(plan):
    item = next(
        row
        for row in plan["slots"]
        if row["kind"] in {"auth_capture_purchase", "authorize_capture", "auth_capture_refund"}
    )
    provider_id = "provider-" + item["merchant_payment_id"]
    normalized = {
        "auth_capture_purchase": "CAPTURED",
        "authorize_capture": "CAPTURED",
        "auth_capture_refund": "REFUNDED",
    }[item["kind"]]
    captured = 1000
    refunded = 1000 if item["kind"] == "auth_capture_refund" else 0
    internal = [{
        "source_row_id": "internal:" + item["merchant_payment_id"],
        "merchant_payment_id": item["merchant_payment_id"],
        "provider_payment_id": provider_id,
        "currency": "USD",
        "normalized_status": normalized,
        "authorized_amount_minor": 1000,
        "captured_amount_minor": captured,
        "refunded_amount_minor": refunded,
    }]
    provider = [{
        "source_row_id": "provider:" + provider_id,
        "provider_payment_id": provider_id,
        "currency": "USD",
        "normalized_status": normalized,
        "authorized_amount_minor": 1000,
        "captured_amount_minor": captured,
        "refunded_amount_minor": refunded,
        "provider_version": 3,
    }]
    snapshot = {
        "metadata": {
            "snapshot_id": reconciliation.expected_snapshot_id(
                plan["campaign_id"], plan["logical_day_sequence"]
            ),
            "business_date": plan["logical_business_date"],
            "provider_scope": plan["merchant_setup"][0]["body"]["merchantId"],
            "scenario_case": "CLEAR",
            "base_snapshot_id": None,
            "internal_snapshot_digest": reconciliation.qualification.row_digest(internal),
            "provider_snapshot_digest": reconciliation.qualification.row_digest(provider),
            "policy_version": reconciliation.POLICY_VERSION,
        },
        "internal": internal,
        "provider": provider,
        "mutation_recipe": {},
    }
    oracle = reconciliation.qualification.oracle(snapshot)
    return {
        "schema_version": 1,
        "campaign_issue": 148,
        "classification": reconciliation.qualification.CLASSIFICATION,
        "snapshot": snapshot,
        "base_snapshot": None,
        "oracle": oracle,
        "actual": {
            "run_id": "run-test318-day-000001-reconciliation",
            "processing_state": "PROCESSED",
            "business_state": "CLEAR",
            "mismatch_count": 0,
            "aggregate_delta_minor": 0,
            "result_digest": oracle["result_digest"],
            "details": oracle["details"],
            "published_count": 1,
        },
        "violations": [],
    }


def make_campaign(base: Path, binary_sha: str) -> Path:
    campaign = base / "test318"
    (campaign / "days").mkdir(parents=True)
    record = {
        "schema": "oxide-batch-workloads.payment-reference-campaign",
        "schema_version": 1,
        "contract": reconciliation.core.contract()["contract"],
        "contract_sha256": reconciliation.core.sha256(reconciliation.core.CONTRACT_PATH),
        "identity": {
            "campaign_id": "test318",
            "source_revision": SHA40,
            "batch_binaries": {
                "payment-reconciliation-postgres": {
                    "sha256": binary_sha,
                    "size_bytes": 123,
                }
            },
        },
    }
    (campaign / "campaign.json").write_bytes(reconciliation.core.canonical_bytes(record))
    return campaign


class ReconciliationProducerTests(unittest.TestCase):
    def test_snapshot_identity_is_accepted_by_workload_contract(self):
        value = reconciliation.expected_snapshot_id("test318", 12)
        self.assertEqual(value, "test318-day-000012-reconciliation")
        self.assertNotIn(":", value)
        self.assertTrue(all(ch.isalnum() or ch in "-_." for ch in value))

    def test_load_producer_report_invokes_verify_only(self):
        plan = cardinality.plan_for("test318", 1)
        report = producer_report(plan)
        observed = {}

        def runner(command, **kwargs):
            observed["command"] = command
            observed["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(report),
                stderr="",
            )

        result = reconciliation.load_producer_report(
            workload_binary=Path("/qualified/payment-reconciliation-postgres"),
            database_url="postgresql://read-only-test",
            snapshot_id=reconciliation.expected_snapshot_id("test318", 1),
            runner=runner,
        )
        self.assertEqual(result, report)
        self.assertEqual(observed["command"][1], "verify")
        self.assertNotIn("snapshot", observed["command"])
        self.assertNotIn("run", observed["command"])
        self.assertNotIn("recover", observed["command"])
        self.assertNotIn("finalize", observed["command"])
        self.assertEqual(
            observed["command"][-1],
            "test318-day-000001-reconciliation",
        )
        self.assertEqual(observed["kwargs"]["timeout"], 30)
        self.assertFalse(observed["kwargs"]["check"])

    def test_producer_command_failure_and_invalid_json_fail_closed(self):
        def failed(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="secret-db-url")

        with self.assertRaisesRegex(
            reconciliation.ReconciliationEvidenceError,
            "verify command failed",
        ):
            reconciliation.load_producer_report(
                workload_binary=Path("/qualified/payment-reconciliation-postgres"),
                database_url="postgresql://secret",
                snapshot_id="test318-day-000001-reconciliation",
                runner=failed,
            )

        def malformed(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{", stderr="")

        with self.assertRaisesRegex(
            reconciliation.ReconciliationEvidenceError,
            "not valid JSON",
        ):
            reconciliation.load_producer_report(
                workload_binary=Path("/qualified/payment-reconciliation-postgres"),
                database_url="postgresql://secret",
                snapshot_id="test318-day-000001-reconciliation",
                runner=malformed,
            )

    def test_exact_binary_identity_and_immutable_retention(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary = root / "payment-reconciliation-postgres"
            binary.write_bytes(b"qualified-binary")
            binary.chmod(0o700)
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            self.assertEqual(
                reconciliation.verify_workload_binary(binary, digest),
                binary.resolve(),
            )
            with self.assertRaisesRegex(
                reconciliation.ReconciliationEvidenceError,
                "binary SHA mismatch",
            ):
                reconciliation.verify_workload_binary(binary, "f" * 64)

            output = root / "days" / "proof.json"
            self.assertEqual(reconciliation.retain_immutable(output, {"a": 1}), "created")
            self.assertEqual(reconciliation.retain_immutable(output, {"a": 1}), "replay")
            with self.assertRaisesRegex(
                reconciliation.ReconciliationEvidenceError,
                "immutable reconciliation verification conflict",
            ):
                reconciliation.retain_immutable(output, {"a": 2})

    def test_build_artifact_validates_report_before_retention(self):
        plan = cardinality.plan_for("test318", 1)
        report = producer_report(plan)
        artifact = reconciliation.build_artifact(
            plan=plan,
            source_revision=SHA40,
            workload_binary_sha256="b" * 64,
            producer_report=report,
            crash_evidence=None,
        )
        subject = reconciliation.validate(
            artifact,
            plan=plan,
            source_revision=SHA40,
            workload_binary_sha256="b" * 64,
        )
        self.assertEqual(subject["snapshot_id"], "test318-day-000001-reconciliation")
        self.assertEqual(subject["business_state"], "CLEAR")
        self.assertEqual(subject["published_count"], 1)

        bad = copy.deepcopy(report)
        bad["actual"]["published_count"] = 0
        with self.assertRaisesRegex(
            reconciliation.ReconciliationEvidenceError,
            "exactly one publication",
        ):
            reconciliation.build_artifact(
                plan=plan,
                source_revision=SHA40,
                workload_binary_sha256="b" * 64,
                producer_report=bad,
                crash_evidence=None,
            )

    def test_cli_retains_validated_artifact_and_replays_exactly(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary = root / "payment-reconciliation-postgres"
            binary.write_bytes(b"qualified-binary")
            binary.chmod(0o700)
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            campaign = make_campaign(root, digest)
            plan = cardinality.plan_for("test318", 1)
            report = producer_report(plan)
            argv = [
                "campaign_day_reconciliation.py",
                "--campaign-dir", str(campaign),
                "--day", "1",
                "--workload-binary", str(binary),
                "--database-url", "postgresql://read-only-test",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                reconciliation, "load_producer_report", return_value=report
            ):
                self.assertEqual(reconciliation.main(), 0)
                self.assertEqual(reconciliation.main(), 0)

            output = campaign / "days/day-000001.reconciliation.verification.json"
            self.assertTrue(output.is_file())
            artifact = json.loads(output.read_text())
            self.assertEqual(artifact["decision"], reconciliation.DECISION)
            self.assertEqual(
                artifact["producer_report"]["snapshot"]["metadata"]["snapshot_id"],
                "test318-day-000001-reconciliation",
            )

    def test_scheduled_reconciliation_day_requires_crash_evidence(self):
        plan = cardinality.plan_for("test318", 3)
        report = producer_report(plan)
        # Rebind the synthetic report to day 3 so the scheduled-crash check is
        # the only missing requirement.
        snapshot = report["snapshot"]
        snapshot["metadata"]["snapshot_id"] = reconciliation.expected_snapshot_id("test318", 3)
        snapshot["metadata"]["business_date"] = plan["logical_business_date"]
        snapshot["metadata"]["provider_scope"] = plan["merchant_setup"][0]["body"]["merchantId"]
        report["oracle"] = reconciliation.qualification.oracle(snapshot)
        report["actual"]["result_digest"] = report["oracle"]["result_digest"]
        report["actual"]["details"] = report["oracle"]["details"]
        with self.assertRaisesRegex(
            reconciliation.ReconciliationEvidenceError,
            "scheduled reconciliation crash evidence required",
        ):
            reconciliation.build_artifact(
                plan=plan,
                source_revision=SHA40,
                workload_binary_sha256="b" * 64,
                producer_report=report,
                crash_evidence=None,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
