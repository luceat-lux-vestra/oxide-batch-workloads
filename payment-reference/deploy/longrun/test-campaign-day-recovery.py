#!/usr/bin/env python3
"""Offline #318 day-recovery proof contract tests."""
from __future__ import annotations

import copy
import unittest

import campaign_day_cardinality as cardinality
import campaign_day_recovery as recovery


SHA40 = "a" * 40
BINARY_SHA = "c" * 64


def plan(day: int):
    return cardinality.plan_for("test318", day)


def fault_item(value, fault_class="post-commit-response-loss"):
    return next(item for item in value["slots"] if item["fault_class"] == fault_class)


def provider_timeline(provider_id: str, *, status="authorized"):
    event_type = "payment.authorized" if status == "authorized" else "payment.failed"
    authorized = 1000 if status == "authorized" else 0
    return {
        "payment": {
            "id": provider_id,
            "amount": 1000,
            "currency": "USD",
            "status": status,
            "authorized_amount": authorized,
            "captured_amount": 0,
            "refunded_amount": 0,
            "version": 2,
        },
        "events": [
            {
                "id": f"{provider_id}:payment.created:1",
                "aggregate_id": provider_id,
                "type": "payment.created",
                "occurred_at": "2026-11-21T03:00:01Z",
                "aggregate_version": 1,
                "correlation_id": "corr-1",
                "causation_id": "",
                "runtime_sequence": 101,
            },
            {
                "id": f"{provider_id}:{event_type}:2",
                "aggregate_id": provider_id,
                "type": event_type,
                "occurred_at": "2026-11-21T03:00:02Z",
                "aggregate_version": 2,
                "correlation_id": "corr-2",
                "causation_id": f"{provider_id}:payment.created:1",
                "runtime_sequence": 102,
            },
        ],
    }


def candidate_for(value, item, provider_id):
    mutation = item["mutations"][0]["body"]
    prepare = item["prepare_payment"]["body"]
    candidate = {
        "candidate_id": "",
        "merchant_id": value["merchant_setup"][0]["body"]["merchantId"],
        "merchant_order_id": item["merchant_order_id"],
        "merchant_payment_id": item["merchant_payment_id"],
        "operation_id": mutation["operationId"],
        "operation_kind": "AUTHORIZE",
        "payment_state": "PAYMENT_UNKNOWN",
        "operation_state": "UNKNOWN",
        "amount_minor": mutation["amountMinor"],
        "currency": "USD",
        "idempotency_key_digest": recovery.secret_digest(mutation["idempotencyKey"]),
        "payment_method_token_digest": recovery.secret_digest(prepare["paymentMethodToken"]),
        "provider_payment_id": provider_id,
        "gateway_operation_id": mutation["operationId"],
        "source_updated_at": "2026-11-21T03:00:00.000000Z",
    }
    candidate["candidate_id"] = recovery.candidate_identity(candidate)
    return candidate


def snapshot_for(value, fault_class="post-commit-response-loss"):
    item = fault_item(value, fault_class)
    provider_id = "provider-" + item["merchant_payment_id"]
    candidate = candidate_for(value, item, provider_id)
    if fault_class == "plan-503-before-side-effect":
        candidate["payment_state"] = "PAYMENT_PENDING"
        candidate["operation_state"] = "RETRY_PENDING"
        candidate["candidate_id"] = recovery.candidate_identity(candidate)
    attempt = {
        "candidate_id": candidate["candidate_id"],
        "attempt_no": 1,
        "processing_generation": 1,
        "attempt_state": "OBSERVED",
        "merchant_http_status": 200,
        "canonical_payment_state": "AUTHORIZED",
        "canonical_operation_state": "SUCCEEDED",
        "canonical_provider_payment_id": provider_id,
        "canonical_gateway_operation_id": candidate["operation_id"],
        "provider_visibility": "IMMEDIATE",
        "provider_payment_id": provider_id,
        "provider_currency": "USD",
        "provider_status": "authorized",
        "provider_authorized_amount_minor": 1000,
        "provider_captured_amount_minor": 0,
        "provider_refunded_amount_minor": 0,
        "outcome_kind": "MERCHANT_CANONICAL_RESPONSE",
    }
    result = {
        "candidate_id": candidate["candidate_id"],
        "classification": "RESOLVED_SUCCEEDED",
        "duplicate_provider_mutation_count": 0,
        "final_canonical_payment_state": "AUTHORIZED",
        "final_canonical_operation_state": "SUCCEEDED",
        "final_provider_status": "authorized",
        "result_digest": "",
        "verified": False,
        "oracle_violations": [],
    }
    result["result_digest"] = recovery.qualification.digest(candidate, attempt, result)
    run_id = recovery.expected_run_id(value["campaign_id"], value["logical_day_sequence"])
    run = {
        "run_id": run_id,
        "contract_version": recovery.RUN_CONTRACT,
        "oxide_batch_version": recovery.OXIDE_BATCH_VERSION,
        "business_date": "2026-11-21",
        "eligibility_cutoff": "2026-11-21T03:01:00.000000Z",
        "scenario_set_id": run_id,
        "policy_version": recovery.POLICY_VERSION,
        "candidate_set_digest": recovery.candidate_set_digest([candidate]),
        "processing_generation": 1,
        "run_state": "PROCESSED",
    }
    return {
        "run": run,
        "candidates": [candidate],
        "attempts": [attempt],
        "results": [result],
    }, {provider_id: provider_timeline(provider_id)}


def provider_reader(timelines):
    return lambda provider_id: (200, copy.deepcopy(timelines[provider_id]))


def artifact_for(day=1, crash=None):
    value = plan(day)
    snapshot, timelines = snapshot_for(value)
    artifact = recovery.collect(
        plan=value,
        source_revision=SHA40,
        workload_binary_sha256=BINARY_SHA,
        snapshot=snapshot,
        crash_evidence=crash,
        provider_read=provider_reader(timelines),
    )
    return value, snapshot, timelines, artifact


class DayRecoveryTests(unittest.TestCase):
    def test_pre_side_effect_503_recovery_converges_to_authorized_eod_truth(self):
        value = plan(1)
        snapshot, timelines = snapshot_for(value, "plan-503-before-side-effect")
        artifact = recovery.collect(
            plan=value,
            source_revision=SHA40,
            workload_binary_sha256=BINARY_SHA,
            snapshot=snapshot,
            crash_evidence=None,
            provider_read=provider_reader(timelines),
        )
        subject = recovery.validate(
            artifact,
            plan=value,
            source_revision=SHA40,
            workload_binary_sha256=BINARY_SHA,
        )
        entry = artifact["candidates"][0]
        self.assertEqual(entry["candidate"]["payment_state"], "PAYMENT_PENDING")
        self.assertEqual(entry["candidate"]["operation_state"], "RETRY_PENDING")
        self.assertEqual(entry["provider_verification"]["payment_status"], "authorized")
        self.assertEqual(entry["provider_verification"]["effective_mutation_count"], 1)
        self.assertEqual(subject["resolved_succeeded_count"], 1)

    def test_terminal_candidate_replays_271_semantics_and_reads_provider_authority_independently(self):
        value, snapshot, _, artifact = artifact_for()
        subject = recovery.validate(
            artifact,
            plan=value,
            source_revision=SHA40,
            workload_binary_sha256=BINARY_SHA,
        )
        self.assertEqual(subject["candidate_count"], 1)
        self.assertEqual(subject["resolved_succeeded_count"], 1)
        self.assertEqual(subject["resolved_failed_count"], 0)
        self.assertEqual(subject["candidate_set_digest"], snapshot["run"]["candidate_set_digest"])
        self.assertEqual(subject["provider_authority"], recovery.provider.AUTHORITY)
        self.assertEqual(subject["provider_source_revision"], recovery.provider.PAYMENT_SANDBOX_SOURCE_REVISION)
        self.assertEqual(
            subject["provider_proofs_sha256"],
            recovery.json_sha256([artifact["candidates"][0]["provider_verification"]]),
        )

    def test_raw_replay_secret_and_candidate_set_tamper_fail_closed(self):
        value, _, _, artifact = artifact_for()
        bad = copy.deepcopy(artifact)
        bad["candidates"][0]["candidate"]["idempotency_key"] = "idem-leak"
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "forbidden raw replay field"):
            recovery.validate(
                bad, plan=value, source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
            )

        bad = copy.deepcopy(artifact)
        bad["run"]["candidate_set_digest"] = "f" * 64
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "candidate-set digest"):
            recovery.validate(
                bad, plan=value, source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
            )

    def test_non_day_or_non_fault_candidate_is_rejected(self):
        value, _, _, artifact = artifact_for()
        bad = copy.deepcopy(artifact)
        normal = next(item for item in value["slots"] if item["kind"] == "auth_capture_purchase")
        bad["candidates"][0]["candidate"]["merchant_payment_id"] = normal["merchant_payment_id"]
        with self.assertRaises(recovery.RecoveryEvidenceError):
            recovery.validate(
                bad, plan=value, source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
            )

    def test_unresolved_or_rehashed_result_cannot_pass(self):
        value, _, _, artifact = artifact_for()
        bad = copy.deepcopy(artifact)
        bad["candidates"][0]["result"]["classification"] = "RETRY_LATER"
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "allowed terminal"):
            recovery.validate(
                bad, plan=value, source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
            )

        bad = copy.deepcopy(artifact)
        bad["candidates"][0]["result"]["result_digest"] = "e" * 64
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "result digest"):
            recovery.validate(
                bad, plan=value, source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
            )

    def test_workload_duplicate_zero_is_not_enough_when_independent_provider_proof_is_corrupt(self):
        value, _, _, artifact = artifact_for()
        bad = copy.deepcopy(artifact)
        bad["candidates"][0]["provider_verification"]["effective_mutation_count"] = 2
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "retained recovery provider proof mismatch"):
            recovery.validate(
                bad, plan=value, source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
            )

        bad = copy.deepcopy(artifact)
        bad["candidates"][0]["provider_timeline"]["events"][1]["aggregate_version"] = 3
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "independent recovery provider proof failed"):
            recovery.validate(
                bad, plan=value, source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
            )

    def test_provider_read_must_be_authenticated_present_timeline(self):
        value = plan(1)
        snapshot, timelines = snapshot_for(value)
        provider_id = next(iter(timelines))
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "authenticated HTTP 200"):
            recovery.collect(
                plan=value,
                source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
                snapshot=snapshot,
                crash_evidence=None,
                provider_read=lambda _: (404, None),
            )
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "authenticated HTTP 200"):
            recovery.collect(
                plan=value,
                source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
                snapshot=snapshot,
                crash_evidence=None,
                provider_read=lambda _: (500, None),
            )
        self.assertIn(provider_id, timelines)

    def test_day_eight_requires_sigkill_new_process_execution_evidence(self):
        value = plan(8)
        snapshot, timelines = snapshot_for(value)
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "scheduled recovery crash"):
            recovery.collect(
                plan=value,
                source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
                snapshot=snapshot,
                crash_evidence=None,
                provider_read=provider_reader(timelines),
            )

        candidate_id = snapshot["candidates"][0]["candidate_id"]
        crash = {
            "schema": recovery.CRASH_SCHEMA,
            "schema_version": 1,
            "campaign_id": value["campaign_id"],
            "logical_day_sequence": 8,
            "run_id": recovery.expected_run_id(value["campaign_id"], 8),
            "target_candidate_id": candidate_id,
            "boundary": "after-external-convergence-before-local-commit",
            "semantic_marker_observed": True,
            "signal": 9,
            "killed_pid": 1101,
            "process_gone_after_kill": True,
            "pre_recovery_run_state": "PROCESSING",
            "pre_recovery_target_attempt_count": 0,
            "recover_process_pid": 1102,
            "recover_launched": True,
            "post_recovery_processing_generation": 1,
        }
        artifact = recovery.collect(
            plan=value,
            source_revision=SHA40,
            workload_binary_sha256=BINARY_SHA,
            snapshot=snapshot,
            crash_evidence=crash,
            provider_read=provider_reader(timelines),
        )
        subject = recovery.validate(
            artifact,
            plan=value,
            source_revision=SHA40,
            workload_binary_sha256=BINARY_SHA,
        )
        self.assertTrue(subject["crash_subject"]["new_process_recovery"])

    def test_crash_evidence_is_forbidden_on_non_recovery_crash_day(self):
        value, snapshot, timelines, _ = artifact_for()
        crash = {"schema": recovery.CRASH_SCHEMA}
        with self.assertRaisesRegex(recovery.RecoveryEvidenceError, "without a scheduled recovery crash"):
            recovery.collect(
                plan=value,
                source_revision=SHA40,
                workload_binary_sha256=BINARY_SHA,
                snapshot=snapshot,
                crash_evidence=crash,
                provider_read=provider_reader(timelines),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
