#!/usr/bin/env python3
"""Architecture guard for the #318 pure campaign-core boundary."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import unittest

import campaign_core as core

HERE = Path(__file__).resolve().parent


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evidence = load("campaign_evidence_boundary", "campaign-evidence.py")
traffic = load("campaign_traffic_boundary", "campaign-traffic-plan.py")


class CampaignCoreBoundaryTests(unittest.TestCase):
    def test_evidence_cli_reexports_the_frozen_core_contract(self) -> None:
        self.assertIs(evidence.EvidenceError, core.EvidenceError)
        self.assertIs(evidence.contract, core.contract)
        self.assertIs(evidence.plan_day, core.plan_day)
        self.assertEqual(evidence.CONTRACT_PATH, core.CONTRACT_PATH)
        self.assertIs(evidence.CAMPAIGN_ID, core.CAMPAIGN_ID)

    def test_traffic_planner_depends_on_core_not_evidence_cli(self) -> None:
        self.assertIs(traffic.evidence, core)
        source = (HERE / "campaign-traffic-plan.py").read_text(encoding="utf-8")
        self.assertNotIn("campaign-evidence.py", source)
        self.assertNotIn("load_campaign_evidence", source)
        self.assertNotIn("importlib.util", source)

    def test_core_is_read_only_and_runtime_neutral(self) -> None:
        source = (HERE / "campaign_core.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertTrue(imported.isdisjoint({"subprocess", "urllib", "socket", "fcntl", "os"}))

    def test_plan_identity_is_unchanged_through_both_adapters(self) -> None:
        for sequence in (1, 2, 3, 10, 30):
            with self.subTest(sequence=sequence):
                expected = core.plan_day(sequence)
                self.assertEqual(evidence.plan_day(sequence), expected)
                plan = traffic.build("smoke318", sequence)
                self.assertEqual(plan["logical_day_sequence"], expected["logical_day_sequence"])
                self.assertEqual(plan["logical_business_date"], expected["logical_business_date"])
                self.assertEqual(plan["day_seed"], expected["day_seed"])
                self.assertEqual(plan["traffic_counts"], expected["traffic"])
                self.assertEqual(plan["plan_contract_sha256"], core.sha256(core.CONTRACT_PATH))


if __name__ == "__main__":
    unittest.main(verbosity=2)
