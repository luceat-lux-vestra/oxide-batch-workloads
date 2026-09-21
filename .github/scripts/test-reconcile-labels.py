#!/usr/bin/env python3

import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).with_name("reconcile-labels.py")
SPEC = importlib.util.spec_from_file_location("reconcile_labels", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class LabelReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = MODULE.load_policy()

    def test_strong_issue_type_signals(self):
        cases = {
            "epic: program": "type:epic",
            "track(A): item I/O": "type:track",
            "workload: postgres-to-postgres": "type:campaign",
            "bug: restart defect": "type:bug",
            "security: audit dependencies": "type:security",
            "docs: clarify evidence": "type:docs",
            "research: compare schedulers": "type:research",
            "governance: reconcile settings": "type:task",
            "hardening(reassessment): close workflow-security drift": "type:task",
            "hardening: classify repository work": "type:task",
            "chore(deps): bump foo": "type:task",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(MODULE.strong_type_from_title(title), expected)

    def test_structural_type_is_preserved_over_generic_signal(self):
        result = MODULE.reconcile_labels(
            ["type:track", "area:workload"],
            "type:task",
            {"area:governance"},
            self.policy,
        )
        self.assertEqual([x for x in result if x.startswith("type:")], ["type:track"])
        self.assertIn("area:workload", result)
        self.assertIn("area:governance", result)

    def test_strong_nonstructural_signal_replaces_nonstructural_type(self):
        result = MODULE.reconcile_labels(
            ["type:task", "help wanted"],
            "type:bug",
            {"area:workload"},
            self.policy,
        )
        self.assertIn("type:bug", result)
        self.assertNotIn("type:task", result)
        self.assertIn("help wanted", result)

    def test_areas_are_additive_and_nonmanaged_labels_are_preserved(self):
        result = MODULE.reconcile_labels(
            ["type:task", "area:governance", "dependencies"],
            "type:task",
            {"area:ci"},
            self.policy,
        )
        self.assertEqual(result.count("type:task"), 1)
        self.assertIn("area:governance", result)
        self.assertIn("area:ci", result)
        self.assertIn("dependencies", result)

    def test_reconciliation_is_idempotent(self):
        first = MODULE.reconcile_labels(
            ["type:task", "area:governance"],
            "type:task",
            {"area:ci"},
            self.policy,
        )
        second = MODULE.reconcile_labels(
            first,
            "type:task",
            {"area:ci"},
            self.policy,
        )
        self.assertEqual(first, second)

    def test_multiple_structural_types_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "multiple structural type labels"):
            MODULE.reconcile_labels(
                ["type:epic", "type:track"],
                None,
                set(),
                self.policy,
            )

    def test_ambiguous_multiple_nonstructural_types_fail_closed_without_signal(self):
        with self.assertRaisesRegex(ValueError, "multiple managed type labels"):
            MODULE.reconcile_labels(
                ["type:bug", "type:task"],
                None,
                set(),
                self.policy,
            )

    def test_workload_paths_are_registry_driven(self):
        registered_path = self.policy["workload_paths"][0][1] + "src/main.rs"
        areas = MODULE.infer_path_areas([registered_path], self.policy)
        self.assertIn("area:workload", areas)
        self.assertIn("area:postgres", areas)

    def test_hardening_prefix_adds_governance_area_only(self):
        for title in (
            "hardening: classify repository work",
            "hardening(reassessment): close workflow-security drift",
            "hardening(metadata): make backfill fail closed",
        ):
            with self.subTest(title=title):
                self.assertEqual(MODULE.infer_title_areas(title), {"area:governance"})

    def test_real_reassessment_titles_reconcile_to_one_task_type(self):
        for title in (
            "hardening(reassessment): close workflow-security and code-scanning drift",
            "hardening(reassessment): make CodeQL language coverage explicit",
            "hardening(metadata): make manual backlog reconciliation fail closed",
        ):
            with self.subTest(title=title):
                result = MODULE.reconcile_labels(
                    [],
                    MODULE.strong_type_from_title(title),
                    MODULE.infer_title_areas(title),
                    self.policy,
                )
                self.assertEqual([x for x in result if x.startswith("type:")], ["type:task"])
                self.assertIn("area:governance", result)

    def test_governance_workflow_path_gets_ci_and_governance(self):
        areas = MODULE.infer_path_areas([".github/workflows/label-automation.yml"], self.policy)
        self.assertIn("area:ci", areas)
        self.assertIn("area:governance", areas)

    def test_structural_backfill_remains_stable(self):
        for current in ("type:epic", "type:track", "type:campaign"):
            with self.subTest(current=current):
                first = MODULE.reconcile_labels([current], "type:task", {"area:governance"}, self.policy)
                second = MODULE.reconcile_labels(first, "type:task", {"area:governance"}, self.policy)
                self.assertEqual(first, second)
                self.assertEqual([x for x in second if x.startswith("type:")], [current])


if __name__ == "__main__":
    unittest.main()
