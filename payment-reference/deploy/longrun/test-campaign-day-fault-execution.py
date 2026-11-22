#!/usr/bin/env python3
"""Offline contract tests for #318 day-wide fault execution."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

import campaign_day_fault_execution as fault_day

HERE = Path(__file__).resolve().parent
SHA40 = "a" * 40
PROVENANCE = {
    "host.json": "1" * 64,
    "runtime.json": "2" * 64,
    "container-images.json": "3" * 64,
    "source.json": "4" * 64,
}


def plan(day: int = 8) -> dict:
    return fault_day.pilot.traffic.build("test318", day)


def prepare_normal_lane(root: Path, value: dict) -> Path:
    day = fault_day.pilot.day_path(root, value)
    day.mkdir(mode=0o700)
    fault_day.pilot.exclusive_write(
        day / "identity.json",
        fault_day.pilot.day_identity(value, SHA40, PROVENANCE),
    )
    normal, faults = fault_day.normal_day.partition(value)
    fault_day.pilot.exclusive_write(
        day / "normal-traffic.summary.json",
        fault_day.normal_day._summary(
            plan=value,
            source_sha=SHA40,
            provenance_hashes=PROVENANCE,
            normal=normal,
            faults=faults,
        ),
    )
    return day


class CampaignDayFaultExecutionTests(unittest.TestCase):
    def test_fault_partition_and_runtime_plans_come_from_catalog(self) -> None:
        value = plan()
        faults = fault_day.fault_slots(value)
        self.assertEqual(len(faults), value["traffic_counts"]["fault_overlay"])
        self.assertEqual(
            {item["fault_class"] for item in faults},
            set(fault_day.fault_catalog.classes()),
        )
        for item in faults:
            rendered = fault_day._runtime_plan(value, item)
            self.assertEqual(
                rendered,
                fault_day.fault_catalog.render_runtime_plan(
                    item["fault_class"],
                    fault_day.logical_business_time(value),
                ),
            )
            if item["fault_class"] != "webhook-ordering-duplicate-drop-retry":
                self.assertIn(
                    fault_day.fault_catalog.PLACEHOLDER,
                    json.dumps(rendered, sort_keys=True),
                )

    def test_fault_proxy_endpoint_is_explicit_loopback_without_fixed_port(self) -> None:
        self.assertEqual(
            fault_day.validate_proxy_base("http://127.0.0.1:28081"),
            "http://127.0.0.1:28081",
        )
        for invalid in (
            "http://0.0.0.0:28081",
            "http://localhost:28081",
            "https://127.0.0.1:28081",
            "http://127.0.0.1",
            "http://127.0.0.1:28081/path",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(fault_day.pilot.Block):
                    fault_day.validate_proxy_base(invalid)

    def test_provider_binding_uses_proxy_for_payment_bound_plan_and_never_rebinds_from_merchant(self) -> None:
        runtime = fault_day.fault_catalog.render_runtime_plan(
            "async-success-stale-convergence",
            "2026-11-18T00:00:00Z",
        )
        stats = {"plan": {"provider_payment_id": "kb-provider-runtime"}}
        provider_id, authority = fault_day._provider_binding(
            runtime_plan=runtime,
            stats=stats,
            merchant_view={"providerPaymentId": "kb-provider-runtime"},
        )
        self.assertEqual(provider_id, "kb-provider-runtime")
        self.assertEqual(authority, "fault-proxy-runtime-binding")

        with self.assertRaisesRegex(fault_day.pilot.Block, "conflict"):
            fault_day._provider_binding(
                runtime_plan=runtime,
                stats=stats,
                merchant_view={"providerPaymentId": "kb-different"},
            )

    def test_webhook_lane_uses_observed_merchant_provider_mapping(self) -> None:
        runtime = fault_day.fault_catalog.render_runtime_plan(
            "webhook-ordering-duplicate-drop-retry",
            "2026-11-18T00:00:00Z",
        )
        provider_id, authority = fault_day._provider_binding(
            runtime_plan=runtime,
            stats={"plan": {"provider_payment_id": None}},
            merchant_view={"providerPaymentId": "kb-webhook-runtime"},
        )
        self.assertEqual(provider_id, "kb-webhook-runtime")
        self.assertEqual(authority, "merchant-payment-view")

    def test_day_coordinator_executes_only_missing_fault_slots_and_never_claims_day_completion(self) -> None:
        value = plan()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = prepare_normal_lane(root, value)
            faults = fault_day.fault_slots(value)
            first = faults[0]
            (day / f"slot-{first['slot']:03}").mkdir()

            calls: list[tuple[str, int]] = []

            def validate_slot(**kwargs):
                item = kwargs["item"]
                calls.append(("validate", item["slot"]))
                return {
                    "slot": item["slot"],
                    "provider_payment_id": f"provider-{item['slot']}",
                    "recovery_reconciliation_required": True,
                }

            def run_slot(**kwargs):
                item = kwargs["item"]
                calls.append(("run", item["slot"]))
                (day / f"slot-{item['slot']:03}").mkdir()
                return {
                    "slot": item["slot"],
                    "provider_payment_id": f"provider-{item['slot']}",
                    "recovery_reconciliation_required": True,
                }

            result = fault_day.execute_fault_day(
                plan=value,
                evidence_root=root,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
                fault_proxy_base="http://127.0.0.1:28081",
                fault_control_timeout_seconds=1,
                provider_control_timeout_seconds=1,
                run_slot=run_slot,
                validate_slot=validate_slot,
            )
            self.assertEqual(result["fault_slot_count"], len(faults))
            self.assertEqual(result["reused_completed_slots"], [first["slot"]])
            self.assertEqual(
                result["executed_this_invocation"],
                [item["slot"] for item in faults[1:]],
            )
            self.assertFalse(result["day_completion_authorized"])
            summary = fault_day.pilot.read_object(day / "fault-traffic.summary.json")
            self.assertFalse(summary["combined_normal_fault_ordering_authorized"])
            self.assertFalse(summary["day_completion_authorized"])
            self.assertEqual(
                summary["scheduled_hard_crash"]["owner"],
                "recovery",
            )

    def test_existing_incomplete_fault_slot_blocks_before_later_mutation(self) -> None:
        value = plan()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = prepare_normal_lane(root, value)
            faults = fault_day.fault_slots(value)
            (day / f"slot-{faults[0]['slot']:03}").mkdir()
            mutated: list[int] = []

            def validate_slot(**kwargs):
                raise fault_day.pilot.Block("incomplete retained fault slot")

            def run_slot(**kwargs):
                mutated.append(kwargs["item"]["slot"])
                raise AssertionError("later fault mutation must not run")

            with self.assertRaisesRegex(fault_day.pilot.Block, "incomplete"):
                fault_day.execute_fault_day(
                    plan=value,
                    evidence_root=root,
                    source_sha=SHA40,
                    provenance_hashes=PROVENANCE,
                    fault_proxy_base="http://127.0.0.1:28081",
                    fault_control_timeout_seconds=1,
                    provider_control_timeout_seconds=1,
                    run_slot=run_slot,
                    validate_slot=validate_slot,
                )
            self.assertEqual(mutated, [])

    def test_provider_failure_control_build_is_bound_to_same_upstream_revision_as_provider_authority(self) -> None:
        dockerfile = (HERE.parent / "payment-sandbox" / "Dockerfile").read_text(encoding="utf-8")
        provider_source = (HERE / "campaign-provider-cardinality.py").read_text(encoding="utf-8")
        docker_match = re.search(
            r"ARG PAYMENT_SANDBOX_SOURCE_REVISION=([0-9a-f]{40})",
            dockerfile,
        )
        authority_match = re.search(
            r'PAYMENT_SANDBOX_SOURCE_REVISION = "([0-9a-f]{40})"',
            provider_source,
        )
        self.assertIsNotNone(docker_match)
        self.assertIsNotNone(authority_match)
        self.assertEqual(docker_match.group(1), authority_match.group(1))

        helper = (
            HERE.parent / "payment-sandbox" / "campaign-control" / "main.go"
        ).read_text(encoding="utf-8")
        self.assertIn("NewFailPaymentWithPublisher", helper)
        self.assertIn("NewEventLogRepository", helper)
        self.assertNotIn("UPDATE payments", helper)
        self.assertNotIn("INSERT INTO event_log", helper)


if __name__ == "__main__":
    unittest.main(verbosity=2)
