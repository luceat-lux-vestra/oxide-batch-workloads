#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

import campaign_day_online_canonical as canonical

pilot = canonical.pilot
SHA40 = "a" * 40
PROVENANCE = {
    "host.json": "1" * 64,
    "runtime.json": "2" * 64,
    "container-images.json": "3" * 64,
    "toolchains.json": "4" * 64,
}


def fake_setup(*, plan, evidence_root, source_sha, provenance_hashes):
    day = pilot.day_path(evidence_root, plan)
    if not day.exists():
        day.mkdir(mode=0o700)
        pilot.exclusive_write(day / "identity.json", pilot.day_identity(plan, source_sha, provenance_hashes))
    return day


class CanonicalOnlineDayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = pilot.traffic.build("payment-reference-longrun-v1", 3)

    def fake_callbacks(self, calls):
        def run_normal(**kwargs):
            slot = kwargs["slot"]
            calls.append(slot)
            day = pilot.day_path(kwargs["evidence_root"], kwargs["plan"])
            folder = day / f"slot-{slot:03d}"
            folder.mkdir()
            pilot.exclusive_write(folder / "result.json", {"slot": slot, "kind": kwargs["plan"]["slots"][slot - 1]["kind"]})
            return {"slot": slot}

        def validate_normal(**kwargs):
            slot = kwargs["slot"]
            day = pilot.day_path(kwargs["evidence_root"], kwargs["plan"])
            return pilot.read_object(day / f"slot-{slot:03d}" / "result.json")

        def run_fault(**kwargs):
            item = kwargs["item"]
            calls.append(item["slot"])
            day = pilot.day_path(kwargs["evidence_root"], kwargs["plan"])
            folder = day / f"slot-{item['slot']:03d}"
            folder.mkdir()
            pilot.exclusive_write(folder / "result.json", {
                "slot": item["slot"],
                "kind": item["kind"],
                "provider_payment_id": f"provider-{item['slot']}",
                "recovery_reconciliation_required": True,
            })
            return {"slot": item["slot"]}

        def validate_fault(**kwargs):
            item = kwargs["item"]
            day = pilot.day_path(kwargs["evidence_root"], kwargs["plan"])
            return pilot.read_object(day / f"slot-{item['slot']:03d}" / "result.json")

        return run_normal, validate_normal, run_fault, validate_fault

    def execute(self, root: Path, calls: list[int]):
        run_normal, validate_normal, run_fault, validate_fault = self.fake_callbacks(calls)
        return canonical.execute_canonical_day(
            plan=self.plan,
            evidence_root=root,
            source_sha=SHA40,
            provenance_hashes=PROVENANCE,
            fault_proxy_base="http://127.0.0.1:28081",
            fault_control_timeout_seconds=1,
            provider_control_timeout_seconds=1,
            ensure_setup=fake_setup,
            run_normal=run_normal,
            validate_normal=validate_normal,
            run_fault=run_fault,
            validate_fault=validate_fault,
        )

    def test_executes_all_primary_slots_in_exact_plan_order_and_replays(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[int] = []
            first = self.execute(root, calls)
            self.assertEqual(calls, [item["slot"] for item in self.plan["slots"]])
            self.assertEqual(first["executed_this_invocation"], calls)
            self.assertTrue(first["combined_normal_fault_ordering_authorized"])
            self.assertTrue(first["online_completion_authorized"])
            self.assertFalse(first["day_completion_authorized"])

            day = pilot.day_path(root, self.plan)
            value = pilot.read_object(day / canonical.SUMMARY_FILE)
            self.assertEqual(value["decision"], canonical.DECISION)
            self.assertEqual(value["slot_order"], list(range(1, self.plan["primary_lifecycles"] + 1)))
            self.assertEqual(len(value["slot_completion_sha256"]), self.plan["primary_lifecycles"])
            self.assertEqual(
                value["order_journal_identity_sha256"],
                pilot.sha((day / canonical.ORDER_DIR / "identity.json").read_bytes()),
            )
            self.assertEqual(
                value["order_chain_head_sha256"],
                value["slot_completion_sha256"][-1]["sha256"],
            )
            self.assertTrue(value["combined_normal_fault_ordering_authorized"])
            self.assertTrue(value["online_completion_authorized"])
            self.assertFalse(value["day_completion_authorized"])

            calls.clear()
            second = self.execute(root, calls)
            self.assertEqual(calls, [])
            self.assertEqual(second["executed_this_invocation"], [])
            self.assertEqual(second["reused_completed_slots"], list(range(1, self.plan["primary_lifecycles"] + 1)))
            self.assertEqual(second["summary_retention"], "replay")

    def test_preexisting_lane_slot_cannot_be_retroactively_promoted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            day = fake_setup(
                plan=self.plan,
                evidence_root=root,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
            )
            (day / "slot-001").mkdir()
            calls: list[int] = []
            with self.assertRaisesRegex(pilot.Block, "retroactively ordered"):
                self.execute(root, calls)
            self.assertEqual(calls, [])

    def test_durable_intent_without_completion_blocks_replay(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            day = fake_setup(
                plan=self.plan,
                evidence_root=root,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
            )
            order_dir = canonical.ensure_order_journal(
                day=day,
                plan=self.plan,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
            )
            first = self.plan["slots"][0]
            intent, _ = canonical.order_files(order_dir, first["slot"])
            journal_sha = pilot.sha((order_dir / "identity.json").read_bytes())
            pilot.exclusive_write(intent, canonical.expected_intent(first, journal_sha))
            calls: list[int] = []
            with self.assertRaisesRegex(pilot.Block, "replay is forbidden"):
                self.execute(root, calls)
            self.assertEqual(calls, [])

    def test_lane_summary_blocks_canonical_promotion(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            day = fake_setup(
                plan=self.plan,
                evidence_root=root,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
            )
            pilot.exclusive_write(day / "normal-traffic.summary.json", {"legacy": True})
            calls: list[int] = []
            with self.assertRaisesRegex(pilot.Block, "cannot be retroactively promoted"):
                self.execute(root, calls)
            self.assertEqual(calls, [])

    def test_low_level_canonical_gate_requires_exact_retained_intent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            day = fake_setup(
                plan=self.plan,
                evidence_root=root,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
            )
            order_dir = canonical.ensure_order_journal(
                day=day,
                plan=self.plan,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
            )
            first = self.plan["slots"][0]

            with self.assertRaisesRegex(pilot.Block, "requires retained exact order intent"):
                pilot.require_canonical_slot_intent(
                    day=day,
                    plan=self.plan,
                    item=first,
                    source_sha=SHA40,
                    provenance_hashes=PROVENANCE,
                )

            journal_sha = pilot.sha((order_dir / "identity.json").read_bytes())
            intent, _ = canonical.order_files(order_dir, first["slot"])
            pilot.exclusive_write(intent, canonical.expected_intent(first, journal_sha))
            self.assertTrue(
                pilot.require_canonical_slot_intent(
                    day=day,
                    plan=self.plan,
                    item=first,
                    source_sha=SHA40,
                    provenance_hashes=PROVENANCE,
                )
            )

            intent.write_text('{"forged":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(pilot.Block, "differs from exact plan/chain authority"):
                pilot.require_canonical_slot_intent(
                    day=day,
                    plan=self.plan,
                    item=first,
                    source_sha=SHA40,
                    provenance_hashes=PROVENANCE,
                )

    def test_second_slot_requires_durable_predecessor_completion(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            day = fake_setup(
                plan=self.plan,
                evidence_root=root,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
            )
            order_dir = canonical.ensure_order_journal(
                day=day,
                plan=self.plan,
                source_sha=SHA40,
                provenance_hashes=PROVENANCE,
            )
            first = self.plan["slots"][0]
            second = self.plan["slots"][1]
            first_intent, _ = canonical.order_files(order_dir, first["slot"])
            journal_sha = pilot.sha((order_dir / "identity.json").read_bytes())
            pilot.exclusive_write(first_intent, canonical.expected_intent(first, journal_sha))
            second_intent, _ = canonical.order_files(order_dir, second["slot"])
            pilot.exclusive_write(second_intent, canonical.expected_intent(second, "0" * 64))

            with self.assertRaisesRegex(pilot.Block, "predecessor is not durably complete"):
                pilot.require_canonical_slot_intent(
                    day=day,
                    plan=self.plan,
                    item=second,
                    source_sha=SHA40,
                    provenance_hashes=PROVENANCE,
                )

    def test_tampered_completion_chain_blocks_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[int] = []
            self.execute(root, calls)
            day = pilot.day_path(root, self.plan)
            _, complete = canonical.order_files(day / canonical.ORDER_DIR, 1)
            complete.write_text('{"tampered":true}\n', encoding="utf-8")
            calls.clear()
            with self.assertRaisesRegex(pilot.Block, "completion drift"):
                self.execute(root, calls)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
