#!/usr/bin/env python3

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / ".github" / "repository-settings-policy.json"
CLASSIFICATIONS = {"required", "conditional", "advisory/hygiene", "manual-readback"}
READBACKS = {"repository-api", "ruleset-api", "manual-readback"}


class RepositorySettingsPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def test_top_level_shape_is_strict(self) -> None:
        self.assertEqual(
            set(self.policy),
            {"schema_version", "repository", "ruleset_id", "controls"},
        )
        self.assertEqual(self.policy["schema_version"], 1)
        self.assertEqual(self.policy["repository"], "luceat-lux-vestra/oxide-batch-workloads")
        self.assertIsInstance(self.policy["ruleset_id"], int)
        self.assertGreater(self.policy["ruleset_id"], 0)

    def test_controls_have_unique_machine_ids_and_supported_metadata(self) -> None:
        controls = self.policy["controls"]
        self.assertIsInstance(controls, list)
        self.assertTrue(controls)
        ids = []
        for control in controls:
            self.assertIsInstance(control, dict)
            self.assertTrue({"id", "classification", "readback", "expected"} <= set(control))
            self.assertTrue(set(control) <= {"id", "classification", "readback", "expected", "rationale"})
            self.assertIsInstance(control["id"], str)
            self.assertTrue(control["id"])
            self.assertIn(control["classification"], CLASSIFICATIONS)
            self.assertIn(control["readback"], READBACKS)
            if "rationale" in control:
                self.assertIsInstance(control["rationale"], str)
                self.assertTrue(control["rationale"].strip())
            ids.append(control["id"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_manual_readback_is_explicit_not_disguised_as_automated(self) -> None:
        manual = [c for c in self.policy["controls"] if c["readback"] == "manual-readback"]
        self.assertTrue(manual)
        for control in manual:
            self.assertIn(control["classification"], {"required", "conditional", "manual-readback"})

    def test_required_contexts_are_the_stable_aggregate_gate_set(self) -> None:
        control = next(c for c in self.policy["controls"] if c["id"] == "ruleset.required_status_contexts")
        self.assertEqual(
            control["expected"],
            ["dependency-review", "supply-chain", "workloads-ci", "workloads-msrv"],
        )

    def test_zero_approval_policy_does_not_claim_extra_approval_as_effective(self) -> None:
        approvals = next(c for c in self.policy["controls"] if c["id"] == "ruleset.required_approving_review_count")
        unattributed = next(
            c
            for c in self.policy["controls"]
            if c["id"] == "ruleset.require_extra_approval_for_unattributed_changes"
        )
        self.assertEqual(approvals["expected"], 0)
        self.assertEqual(approvals["classification"], "conditional")
        self.assertFalse(unattributed["expected"])
        self.assertEqual(unattributed["classification"], "conditional")

    def test_signed_commit_requirement_is_not_silently_asserted(self) -> None:
        signed = next(c for c in self.policy["controls"] if c["id"] == "security.signed_commits")
        self.assertEqual(signed["classification"], "conditional")
        self.assertFalse(signed["expected"])
        self.assertEqual(signed["readback"], "ruleset-api")


if __name__ == "__main__":
    unittest.main()
