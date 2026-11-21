#!/usr/bin/env python3
"""Offline independent 100-slot provider cardinality proof regressions (#318)."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import unittest

import campaign_day_cardinality as card

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("day_card_test_fixtures", HERE / "test-campaign-evidence.py")
assert spec is not None and spec.loader is not None
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class DayCardinalityTests(unittest.TestCase):
    def setUp(self):
        self.plan = card.plan_for("test318", 1)
        self.rows, self.timelines = fixtures.mocked_day_observations(self.plan)
        self.roster = {
            "schema": "oxide-batch-workloads.payment-reference-settlement-roster-verification",
            "schema_version": 1,
            "decision": "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED",
            "campaign_id": "test318",
            "logical_day_sequence": 1,
            "source_revision": fixtures.SHA40,
            "day_plan_sha256": card.digest(self.plan).removeprefix("sha256:"),
            "plan_contract_sha256": self.plan["plan_contract_sha256"],
            "fault_catalog_sha256": self.plan["fault_catalog_sha256"],
            "roster_sha256": "sha256:" + "1" * 64,
            "provider_account_id": "payment-sandbox-default",
            "settlement_date": self.plan["logical_business_date"],
            "currency": "USD",
            "eligible_payment_count": 80,
            "observed_slot_count": 100,
            "observations": self.rows,
        }

    def collect(self):
        return card.collect(
            plan=self.plan,
            source_revision=fixtures.SHA40,
            roster_verification=self.roster,
            read=lambda payment_id: self.timelines[payment_id],
        )

    def test_all_100_slots_verified_with_pinned_provider_timeline_authority(self):
        result = self.collect()
        self.assertEqual(result["observed_slot_count"], 100)
        self.assertEqual(result["effective_mutation_total"], 245)
        self.assertEqual(result["provider_source_revision"], card.provider.PAYMENT_SANDBOX_SOURCE_REVISION)
        self.assertEqual(card.validate(
            result, plan=self.plan, source_revision=fixtures.SHA40,
            roster_verification=self.roster,
        )["effective_mutation_total"], 245)
        absent = [row for row in result["observations"] if row["http_status"] == 404]
        self.assertEqual(absent, [])
        recovered_503 = [
            row for row in result["observations"]
            if row["fault_class"] == "plan-503-before-side-effect"
        ]
        self.assertEqual(len(recovered_503), 2)
        self.assertTrue(all(row["verification"]["payment_status"] == "authorized" for row in recovered_503))
        self.assertTrue(all(row["verification"]["effective_mutation_count"] == 1 for row in recovered_503))
        self.assertEqual(result, self.collect())

    def test_missing_and_duplicate_slots_fail_closed(self):
        artifact = self.collect()
        artifact["observations"].pop()
        with self.assertRaisesRegex(ValueError, "exactly 100 slots"):
            card.validate(artifact, plan=self.plan, source_revision=fixtures.SHA40,
                          roster_verification=self.roster)
        self.rows[1]["provider_payment_id"] = self.rows[0]["provider_payment_id"]
        with self.assertRaises(ValueError):
            self.collect()

    def test_provider_ledger_corruption_is_rechecked_not_trusted(self):
        artifact = self.collect()
        present = next(x for x in artifact["observations"] if x["http_status"] == 200)
        present["timeline"]["events"][-1]["aggregate_version"] = 999
        with self.assertRaises(ValueError):
            card.validate(artifact, plan=self.plan, source_revision=fixtures.SHA40,
                          roster_verification=self.roster)

    def test_recovered_pre_side_effect_503_requires_present_authorized_eod_truth(self):
        artifact = self.collect()
        recovered = next(
            x for x in artifact["observations"]
            if x["fault_class"] == "plan-503-before-side-effect"
        )
        self.assertEqual(recovered["http_status"], 200)
        self.assertEqual(recovered["verification"]["payment_status"], "authorized")

        self.timelines[recovered["provider_payment_id"]] = (404, None)
        with self.assertRaisesRegex(ValueError, "expected present provider timeline"):
            self.collect()

        bad = copy.deepcopy(artifact)
        target = next(
            x for x in bad["observations"]
            if x["fault_class"] == "plan-503-before-side-effect"
        )
        target["http_status"] = 404
        target["timeline"] = None
        target["verification"] = card.provider.verify_absent(target["provider_payment_id"], 404)
        with self.assertRaises(ValueError):
            card.validate(
                bad, plan=self.plan, source_revision=fixtures.SHA40,
                roster_verification=self.roster,
            )

    def test_roster_plan_identity_and_source_provenance_cannot_drift(self):
        artifact = self.collect()
        changed = copy.deepcopy(self.roster)
        changed["observations"][0]["merchant_payment_id"] = "different"
        with self.assertRaisesRegex(ValueError, "roster digest mismatch|roster plan"):
            card.validate(artifact, plan=self.plan, source_revision=fixtures.SHA40,
                          roster_verification=changed)
        with self.assertRaisesRegex(ValueError, "campaign/day/source mismatch"):
            card.validate(artifact, plan=self.plan, source_revision="f" * 40,
                          roster_verification=self.roster)

    def test_roster_eligibility_count_is_independently_recomputed(self):
        artifact = self.collect()
        corrupted_roster = copy.deepcopy(self.roster)
        corrupted_roster["eligible_payment_count"] = 81
        artifact["roster_verification_sha256"] = card.digest(corrupted_roster)
        with self.assertRaisesRegex(ValueError, "eligibility count mismatch"):
            card.validate(artifact, plan=self.plan, source_revision=fixtures.SHA40,
                          roster_verification=corrupted_roster)

    def test_expected_transition_count_cannot_be_reduced(self):
        artifact = self.collect()
        artifact["effective_mutation_total"] -= 1
        with self.assertRaisesRegex(ValueError, "total mismatch"):
            card.validate(artifact, plan=self.plan, source_revision=fixtures.SHA40,
                          roster_verification=self.roster)
        artifact["effective_mutation_total"] += 1
        present = next(x for x in artifact["observations"] if x["http_status"] == 200)
        present["verification"]["effective_mutation_count"] = 0
        with self.assertRaises(ValueError):
            card.validate(artifact, plan=self.plan, source_revision=fixtures.SHA40,
                          roster_verification=self.roster)


if __name__ == "__main__":
    unittest.main(verbosity=2)
