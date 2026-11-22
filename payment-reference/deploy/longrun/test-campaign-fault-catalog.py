#!/usr/bin/env python3
"""#318 canonical fault-catalog contract and renderer tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

import campaign_fault_catalog as catalog

HERE = Path(__file__).resolve().parent
ADAPTER = HERE.parent / "fault-proxy" / "request_fault_adapter.py"


def load_adapter():
    spec = importlib.util.spec_from_file_location("campaign_fault_adapter", ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = load_adapter()


class CampaignFaultCatalogTests(unittest.TestCase):
    EXPECTED = {
        "plan-503-before-side-effect": (
            "fault-plan-pre-side-effect-503", "PENDING", "AUTHORIZED", "RETRYABLE_FAILURE",
            "HTTP_503", "RETRYABLE_PRE_SIDE_EFFECT", False, "SAME_IDENTITY_ONLY",
        ),
        "post-commit-response-loss": (
            "fault-plan-post-commit-response-loss", "AUTHORIZED", "AUTHORIZED", "UNKNOWN",
            "CONNECTION_DROP", "AMBIGUOUS_POST_COMMIT", True, "BLOCK_UNTIL_READ_RECONCILIATION",
        ),
        "async-success-stale-convergence": (
            "fault-plan-async-success-stale-convergence", "AUTHORIZED", "AUTHORIZED", "ACCEPTED_THEN_CONVERGED",
            "HTTP_200_PENDING_ACCEPTED", "ASYNC_PENDING_CONVERGENCE", True, "NO_MUTATION_RETRY_WHILE_PENDING",
        ),
        "async-failure": (
            "fault-plan-async-failure", "FAILED", "FAILED", "ACCEPTED_THEN_FAILED",
            "HTTP_200_PENDING_ACCEPTED", "ASYNC_TERMINAL_FAILURE", True, "NO_MUTATION_RETRY_BEFORE_TERMINAL_READ",
        ),
        "webhook-ordering-duplicate-drop-retry": (
            "fault-plan-webhook-delivery", "AUTHORIZED", "AUTHORIZED", "DELIVERY_SEQUENCE_REPRODUCED",
            "WEBHOOK_SCHEDULE_REPLAYED", "WEBHOOK_REPLAY_DELIVERY", False, "NOT_APPLICABLE",
        ),
    }

    def test_catalog_freezes_exact_five_campaign_fault_classes(self) -> None:
        self.assertEqual(catalog.SCHEMA_VERSION, 2)
        self.assertEqual(catalog._load()["schema_version"], 2)
        self.assertEqual(catalog.classes(), tuple(self.EXPECTED))
        self.assertEqual(len(catalog.catalog_sha256()), 64)
        for fault_class, expected in self.EXPECTED.items():
            with self.subTest(fault_class=fault_class):
                value = catalog.fault(fault_class)
                reconciliation = value["reconciliation"]
                observed = (
                    value["scenario_case"],
                    value["expected_provider_truth"],
                    value["end_of_day_provider_truth"],
                    value["expected_canonical_outcome"],
                    value["caller_outcome"],
                    reconciliation["classification"],
                    reconciliation["requires_read_before_mutation_retry"],
                    reconciliation["mutation_retry_policy"],
                )
                self.assertEqual(observed, expected)
                self.assertTrue(reconciliation["requirements"])

    def test_online_and_end_of_day_truth_are_distinct_only_for_recoverable_pre_side_effect_503(self) -> None:
        pre = catalog.fault("plan-503-before-side-effect")
        self.assertEqual(pre["expected_provider_truth"], "PENDING")
        self.assertEqual(pre["end_of_day_provider_truth"], "AUTHORIZED")
        self.assertEqual(catalog.end_of_day_provider_truth("plan-503-before-side-effect"), "AUTHORIZED")

        for fault_class in catalog.classes():
            value = catalog.fault(fault_class)
            if fault_class == "plan-503-before-side-effect":
                continue
            self.assertEqual(value["end_of_day_provider_truth"], value["expected_provider_truth"])

        rendered = catalog.render_plan(
            "plan-503-before-side-effect",
            "2026-11-14T00:00:00Z",
            "provider-phase-test",
        )
        self.assertEqual(rendered["expected_provider_truth"], "PENDING")
        self.assertNotIn("end_of_day_provider_truth", rendered)

    def test_every_rendered_plan_is_accepted_by_qualified_fault_adapter(self) -> None:
        for fault_class in catalog.classes():
            with self.subTest(fault_class=fault_class):
                payment_id = None if fault_class == "webhook-ordering-duplicate-drop-retry" else "provider-catalog-test"
                plan = catalog.render_plan(fault_class, "2026-11-14T00:00:00Z", payment_id)
                snapshot = adapter.RequestFaultPlanAdapter().load(plan)
                self.assertEqual(snapshot["identity"]["scenario_case"], self.EXPECTED[fault_class][0])
                self.assertNotIn(catalog.PLACEHOLDER, json.dumps(plan, sort_keys=True))

    def test_payment_bound_fault_requires_payment_identity(self) -> None:
        rendered = catalog.render_plan(
            "post-commit-response-loss",
            "2026-11-14T00:00:00Z",
            "provider-safe_1.test",
        )
        self.assertEqual(
            rendered["steps"][0]["request_match"]["path_suffix"],
            "/payments/provider-safe_1.test/authorize",
        )

        with self.assertRaisesRegex(catalog.FaultCatalogError, "provider payment id"):
            catalog.render_plan("post-commit-response-loss", "2026-11-14T00:00:00Z")

        with self.assertRaisesRegex(catalog.FaultCatalogError, "safe path segment"):
            catalog.render_plan(
                "post-commit-response-loss",
                "2026-11-14T00:00:00Z",
                "../unsafe",
            )

    def test_cli_render_is_deterministic_and_fail_closed(self) -> None:
        command = [
            sys.executable, str(HERE / "campaign_fault_catalog.py"), "render",
            "--fault-class", "async-success-stale-convergence",
            "--payment-id", "provider-cli-test",
            "--logical-business-time", "2026-11-14T00:00:00Z",
        ]
        first = subprocess.run(command, text=True, capture_output=True, check=False)
        second = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(
            json.loads(first.stdout),
            catalog.render_plan(
                "async-success-stale-convergence",
                "2026-11-14T00:00:00Z",
                "provider-cli-test",
            ),
        )
        blocked = subprocess.run(
            [
                sys.executable, str(HERE / "campaign_fault_catalog.py"), "render",
                "--fault-class", "unknown",
                "--logical-business-time", "2026-11-14T00:00:00Z",
            ],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(blocked.returncode, 75)
        self.assertEqual(json.loads(blocked.stderr)["decision"], "BLOCK")

    def test_runtime_renderer_preserves_provider_placeholder_until_first_real_provider_request(self) -> None:
        for fault_class in catalog.classes():
            with self.subTest(fault_class=fault_class):
                plan = catalog.render_runtime_plan(
                    fault_class,
                    "2026-11-14T00:00:00Z",
                )
                rendered = json.dumps(plan, sort_keys=True)
                if fault_class == "webhook-ordering-duplicate-drop-retry":
                    self.assertNotIn(catalog.PLACEHOLDER, rendered)
                    continue

                self.assertIn(catalog.PLACEHOLDER, rendered)
                runtime = adapter.RequestFaultPlanAdapter()
                runtime.load(plan)
                runtime.advance(0)
                request_point = next(
                    step["point"]
                    for step in plan["steps"]
                    if step["point"] in {
                        "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                        "REQUEST_AFTER_PROVIDER_COMMIT",
                    }
                )
                consume = (
                    runtime.consume_before
                    if request_point == "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT"
                    else runtime.consume_after_commit
                )
                decision = consume(
                    "POST",
                    "/payments/kb-runtime-catalog/authorize",
                )
                self.assertIsNotNone(decision)
                self.assertEqual(
                    "kb-runtime-catalog",
                    runtime.snapshot()["provider_payment_id"],
                )
                self.assertNotIn(
                    catalog.PLACEHOLDER,
                    json.dumps(decision["step"], sort_keys=True),
                )

    def test_runtime_cli_is_deterministic_without_inventing_provider_identity(self) -> None:
        command = [
            sys.executable,
            str(HERE / "campaign_fault_catalog.py"),
            "render-runtime",
            "--fault-class",
            "async-success-stale-convergence",
            "--logical-business-time",
            "2026-11-14T00:00:00Z",
        ]
        first = subprocess.run(command, text=True, capture_output=True, check=False)
        second = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        value = json.loads(first.stdout)
        self.assertIn(catalog.PLACEHOLDER, json.dumps(value, sort_keys=True))
        self.assertEqual(
            value,
            catalog.render_runtime_plan(
                "async-success-stale-convergence",
                "2026-11-14T00:00:00Z",
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
