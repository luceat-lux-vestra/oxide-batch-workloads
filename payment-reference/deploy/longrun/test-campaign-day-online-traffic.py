#!/usr/bin/env python3
"""Offline controls for #318 day-wide non-fault online traffic coordination."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "payment_reference_day_online_traffic",
    HERE / "campaign_day_online_traffic.py",
)
assert spec is not None and spec.loader is not None
day_online = importlib.util.module_from_spec(spec)
spec.loader.exec_module(day_online)
pilot = day_online.pilot


class FakeMerchant:
    def __init__(self, plan, slot):
        self.row = plan["slots"][slot - 1]
        self.requests = []

    def post(self, base, path, body):
        self.requests.append((base, path, body))
        if path in ("/merchants", "/customers", "/orders"):
            return 201, b""
        if path.endswith("/payments"):
            return 201, json.dumps({
                "merchantPaymentId": self.row["merchant_payment_id"],
                "merchantOrderId": self.row["merchant_order_id"],
                "paymentState": "CREATED",
                "amountMinor": 1000,
                "currency": "USD",
            }).encode()
        for index, request in enumerate(self.row["mutations"]):
            if request["path"] == path and request["body"] == body:
                return 200, json.dumps({
                    "merchantPaymentId": self.row["merchant_payment_id"],
                    "merchantOrderId": self.row["merchant_order_id"],
                    "paymentState": pilot.NORMAL[self.row["kind"]][index],
                    "operationId": body["operationId"],
                    "operationState": "SUCCEEDED",
                }).encode()
        raise AssertionError(path)

    def get(self, base, path):
        assert base == pilot.BASE
        assert path == f'/payments/{self.row["merchant_payment_id"]}'
        return {
            "merchantPaymentId": self.row["merchant_payment_id"],
            "merchantOrderId": self.row["merchant_order_id"],
            "paymentState": self.row["expected_normal_terminal_state"],
            "amountMinor": 1000,
            "currency": "USD",
        }


class DayOnlineTrafficTests(unittest.TestCase):
    def setUp(self):
        self.plan = pilot.traffic.build("dayonline318", 1)
        self.source_sha = "a" * 40
        self.provenance = {name: "b" * 64 for name in pilot.PROVENANCE_FILES}

    def test_partition_is_exact_and_fault_lane_remains_external(self):
        normal, faults = day_online.partition(self.plan)
        self.assertEqual(len(normal), sum(
            self.plan["traffic_counts"][kind] for kind in pilot.NORMAL
        ))
        self.assertEqual(len(faults), self.plan["traffic_counts"]["fault_overlay"])
        self.assertEqual(
            {row["slot"] for row in normal} | {row["slot"] for row in faults},
            set(range(1, self.plan["primary_lifecycles"] + 1)),
        )
        self.assertFalse({row["slot"] for row in normal} & {row["slot"] for row in faults})
        self.assertTrue(all(row["fault_class"] is None for row in normal))
        self.assertTrue(all(row["fault_class"] for row in faults))

    def test_completed_pilot_slot_can_be_reverified_and_tamper_blocks_resume(self):
        normal, _ = day_online.partition(self.plan)
        slot = normal[0]["slot"]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = FakeMerchant(self.plan, slot)
            pilot.run_one(
                plan=self.plan,
                slot=slot,
                evidence_root=root,
                source_sha=self.source_sha,
                provenance_hashes=self.provenance,
                post=fake.post,
                get=fake.get,
            )
            verified = pilot.validate_completed_slot(
                plan=self.plan,
                slot=slot,
                evidence_root=root,
                source_sha=self.source_sha,
                provenance_hashes=self.provenance,
            )
            self.assertEqual(verified["slot"], slot)

            result_path = pilot.day_path(root, self.plan) / f"slot-{slot:03}" / "result.json"
            result = pilot.read_object(result_path)
            result["merchant_payment_id"] = "tampered"
            result_path.write_bytes(pilot.stable(result))
            with self.assertRaisesRegex(pilot.Block, "retained result drift"):
                pilot.validate_completed_slot(
                    plan=self.plan,
                    slot=slot,
                    evidence_root=root,
                    source_sha=self.source_sha,
                    provenance_hashes=self.provenance,
                )

    def test_coordinator_executes_only_missing_normal_slots_and_replays_summary(self):
        normal, faults = day_online.partition(self.plan)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            day = pilot.day_path(root, self.plan)
            executed = []
            validated = []

            def run_slot(**kwargs):
                slot = kwargs["slot"]
                if not day.exists():
                    day.mkdir()
                target = day / f"slot-{slot:03}"
                target.mkdir()
                executed.append(slot)
                return {"slot": slot}

            def validate_slot(**kwargs):
                slot = kwargs["slot"]
                if not (day / f"slot-{slot:03}").is_dir():
                    raise pilot.Block("missing fake slot")
                validated.append(slot)
                return {"slot": slot}

            first = day_online.execute_normal_day(
                plan=self.plan,
                evidence_root=root,
                source_sha=self.source_sha,
                provenance_hashes=self.provenance,
                run_slot=run_slot,
                validate_slot=validate_slot,
            )
            self.assertEqual(executed, [row["slot"] for row in normal])
            self.assertEqual(validated, [row["slot"] for row in normal])
            self.assertEqual(first["summary_retention"], "created")
            self.assertFalse(first["day_completion_authorized"])
            self.assertEqual(first["fault_slot_count"], len(faults))
            self.assertFalse(set(executed) & {row["slot"] for row in faults})

            executed.clear()
            validated.clear()
            second = day_online.execute_normal_day(
                plan=self.plan,
                evidence_root=root,
                source_sha=self.source_sha,
                provenance_hashes=self.provenance,
                run_slot=run_slot,
                validate_slot=validate_slot,
            )
            self.assertEqual(executed, [])
            self.assertEqual(validated, [row["slot"] for row in normal])
            self.assertEqual(second["summary_retention"], "replay")
            self.assertEqual(second["reused_completed_slots"], [row["slot"] for row in normal])

            summary = pilot.read_object(day / "normal-traffic.summary.json")
            self.assertEqual(summary["decision"], day_online.DECISION)
            self.assertFalse(summary["day_completion_authorized"])
            self.assertEqual(summary["normal_slot_count"], len(normal))
            self.assertEqual(summary["fault_slot_count"], len(faults))
            self.assertEqual(
                summary["normal_request_count"],
                sum(len(pilot.slot_requests(row)) for row in normal),
            )
            self.assertEqual(
                [row["slot"] for row in summary["pending_fault_slots"]],
                [row["slot"] for row in faults],
            )

    def test_existing_ambiguous_slot_blocks_before_later_business_mutation(self):
        normal, _ = day_online.partition(self.plan)
        first_slot = normal[0]["slot"]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            day = pilot.day_path(root, self.plan)
            day.mkdir()
            (day / f"slot-{first_slot:03}").mkdir()
            executed = []

            def run_slot(**kwargs):
                executed.append(kwargs["slot"])
                raise AssertionError("must not execute after ambiguous retained slot")

            def validate_slot(**kwargs):
                raise pilot.Block("ambiguous/incomplete retained slot")

            with self.assertRaisesRegex(pilot.Block, "ambiguous/incomplete"):
                day_online.execute_normal_day(
                    plan=self.plan,
                    evidence_root=root,
                    source_sha=self.source_sha,
                    provenance_hashes=self.provenance,
                    run_slot=run_slot,
                    validate_slot=validate_slot,
                )
            self.assertEqual(executed, [])

    def test_summary_conflict_fails_closed(self):
        normal, _ = day_online.partition(self.plan)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            day = pilot.day_path(root, self.plan)
            day.mkdir()
            for row in normal:
                (day / f"slot-{row['slot']:03}").mkdir()

            def no_run(**kwargs):
                raise AssertionError("all fake slots already exist")

            def validate(**kwargs):
                return {"slot": kwargs["slot"]}

            day_online.execute_normal_day(
                plan=self.plan,
                evidence_root=root,
                source_sha=self.source_sha,
                provenance_hashes=self.provenance,
                run_slot=no_run,
                validate_slot=validate,
            )
            summary_path = day / "normal-traffic.summary.json"
            summary_path.write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(pilot.Block, "immutable conflict"):
                day_online.execute_normal_day(
                    plan=self.plan,
                    evidence_root=root,
                    source_sha=self.source_sha,
                    provenance_hashes=self.provenance,
                    run_slot=no_run,
                    validate_slot=validate,
                )

    def test_plan_only_never_claims_day_completion(self):
        normal, faults = day_online.partition(self.plan)
        self.assertGreater(len(normal), 0)
        self.assertGreater(len(faults), 0)
        summary = day_online._summary(
            plan=self.plan,
            source_sha=self.source_sha,
            provenance_hashes=self.provenance,
            normal=normal,
            faults=faults,
        )
        self.assertEqual(summary["decision"], "NORMAL_TRAFFIC_COMPLETE_FAULT_OVERLAYS_PENDING")
        self.assertTrue(summary["independent_financial_verification_required"])
        self.assertFalse(summary["day_completion_authorized"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
