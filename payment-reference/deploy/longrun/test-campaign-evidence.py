#!/usr/bin/env python3
"""Deterministic, local-only contract and evidence negative controls for #318."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("campaign_evidence", HERE / "campaign-evidence.py")
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
m = module

SHA40 = "a" * 40
SHA64 = "b" * 64
SERVICES = ("merchant", "gateway", "killbill", "payment-sandbox", "payment-sandbox-fault-proxy", "merchant-db", "gateway-db", "killbill-db")
CHECKS = ("recovery", "reconciliation", "settlement_roster", "settlement_close", "financial_invariants", "side_effect_cardinality")


def fixture(root):
    provenance = root / "source-provenance"
    provenance.mkdir()
    services = {service: f"ghcr.io/oxide/{service}@sha256:{SHA64}" for service in SERVICES}
    documents = {
        "host.json": {"repository_sha": SHA40},
        "runtime.json": {"repository_sha": SHA40, "compose_services": list(SERVICES)},
        "container-images.json": {"repository_sha": SHA40, "images": [{"service": s, "image_ref": ref} for s, ref in services.items()]},
        "toolchains.json": {"repository_sha": SHA40},
    }
    hashes = {}
    for name, doc in documents.items():
        path = provenance / name
        path.write_bytes(m.canonical_bytes(doc))
        hashes[name] = m.sha256(path)
    identity = {
        "campaign_id": "test318",
        "source_revision": SHA40,
        "oxide_batch": {"version": "0.7.0", "registry": "crates.io", "lockfile_sha256": SHA64, "artifact_sha256": SHA64},
        "batch_binaries": {name: {"sha256": SHA64, "size_bytes": 1024} for name in m.BINARIES},
        "images": {"manifest_sha256": SHA64, "compose_sha256": SHA64, "services": services},
        "provenance_sha256": hashes,
    }
    return provenance, identity


def write_terminal(root, sequence, *, started=True):
    if started:
        m.append_event(root, sequence, "day-started", {})
    verification = {
        "schema": "oxide-batch-workloads.payment-reference-day-independent-verification",
        "schema_version": 1, "campaign_id": root.name,
        "logical_day_sequence": sequence, "verdict": "PASS",
        "checks": {c: {"verdict": "PASS", "artifact_sha256": SHA64} for c in CHECKS}
    }
    path = root / "days" / f"{sequence:06}.verification.json"
    m.create_only(path, m.canonical_bytes(verification))
    day = {
        "schema": m.DAY_SCHEMA,
        "schema_version": 1, "campaign_id": root.name,
        "logical_day_sequence": sequence, "plan": m.plan_day(sequence),
        "verdict": "PASS", "independent_verification_sha256": m.sha256(path),
    }
    day_path = root / "days" / f"{sequence:06}.json"
    m.create_only(day_path, m.canonical_bytes(day))
    m.append_event(root, sequence, "day-terminal-success", {"day_sha256": m.sha256(day_path)})


class CampaignEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.provenance, self.identity = fixture(self.base)

    def init(self):
        return m.initialize(self.base / "evidence", self.identity, self.provenance)

    def test_plan_geometry_seed_calendar_crash_and_quiet_schedule(self):
        for seq in range(1, 31):
            plan = m.plan_day(seq)
            self.assertEqual(sum(plan["traffic"].values()), 100)
            self.assertEqual(plan["primary_lifecycles"], 100)
            self.assertEqual(plan["logical_day_sequence"], seq)
            self.assertEqual(plan, m.plan_day(seq))
            self.assertEqual(plan["minimum_quiet_seconds"], 600 if seq % 10 == 0 else 60)
        self.assertEqual(len({m.plan_day(s)["day_seed"] for s in range(1, 31)}), 30)
        self.assertEqual(m.plan_day(1)["logical_business_date"], "2026-11-18")
        self.assertEqual(m.plan_day(2)["logical_business_date"], "2026-11-19")
        self.assertIsNone(m.plan_day(1)["scheduled_hard_crash"])
        self.assertEqual(m.plan_day(3)["scheduled_hard_crash"], "reconciliation-during-write")
        self.assertEqual(m.plan_day(6)["scheduled_hard_crash"], "reconciliation-after-business-commit")
        self.assertEqual(m.plan_day(8)["scheduled_hard_crash"], "recovery-after-external-convergence-before-local-commit")
        self.assertEqual(m.plan_day(10)["scheduled_hard_crash"], "reconciliation-finalize-after-commit")
        self.assertEqual(m.plan_day(13)["scheduled_hard_crash"], m.plan_day(3)["scheduled_hard_crash"])
        for bad in (0, -1, 100001, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(m.EvidenceError):
                m.plan_day(bad)

    def test_initialize_is_create_only_and_provenance_is_immutable(self):
        root = self.init()
        self.assertEqual(m.inspect(root)["decision"], "READY")
        self.assertEqual(m.inspect(root)["next_day"], m.plan_day(1))
        for name in m.ARTIFACTS:
            self.assertEqual(m.sha256(root / "provenance" / name), self.identity["provenance_sha256"][name])
        self.assertEqual((root / "events.jsonl").read_text().count("\n"), 1)
        with self.assertRaises(FileExistsError):
            self.init()
        (root / "provenance" / "host.json").write_text("corruption")
        with self.assertRaises(m.EvidenceError):
            m.inspect(root)

    def test_invalid_identity_and_cross_source_provenance_fail_before_creation(self):
        cases = [
            ("bad campaign ID", ("campaign_id", "../../escape")),
            ("wrong release", ("oxide_batch", {**self.identity["oxide_batch"], "version": "0.6.0"})),
            ("wrong registry", ("oxide_batch", {**self.identity["oxide_batch"], "registry": "git"})),
            ("mutable image", ("images", {**self.identity["images"], "services": {**self.identity["images"]["services"], "gateway": "gateway:latest"}})),
            ("missing binary", ("batch_binaries", {})),
            ("wrong provenance hash", ("provenance_sha256", {**self.identity["provenance_sha256"], "host.json": SHA64})),
        ]
        for label, (key, val) in cases:
            with self.subTest(label=label):
                modified = copy.deepcopy(self.identity)
                modified[key] = val
                with self.assertRaises((m.EvidenceError, ValueError)):
                    m.initialize(self.base / label.replace(" ", "-"), modified, self.provenance)
        corrupted = self.provenance / "runtime.json"
        d = json.loads(corrupted.read_text())
        d["repository_sha"] = "c" * 40
        corrupted.write_bytes(m.canonical_bytes(d))
        changed = copy.deepcopy(self.identity)
        changed["provenance_sha256"]["runtime.json"] = m.sha256(corrupted)
        with self.assertRaisesRegex(m.EvidenceError, "repository SHA"):
            m.initialize(self.base / "changed-revision", changed, self.provenance)

    def test_one_then_two_completed_days_advance_frontier(self):
        root = self.init()
        write_terminal(root, 1)
        self.assertEqual(m.inspect(root)["frontier_sequence"], 1)
        self.assertEqual(m.inspect(root)["next_day"], m.plan_day(2))
        write_terminal(root, 2)
        self.assertEqual(m.inspect(root)["frontier_sequence"], 2)
        self.assertEqual(m.inspect(root)["next_day"], m.plan_day(3))
        with self.assertRaises(FileExistsError):
            write_terminal(root, 2, started=False)

    def test_started_but_not_terminal_is_blocked_across_new_process(self):
        root = self.init()
        m.append_event(root, 1, "day-started", {})
        self.assertEqual(m.inspect(root)["decision"], "BLOCK")
        out = subprocess.run([sys.executable, str(HERE / "campaign-evidence.py"), "status", "--campaign-dir", str(root)], text=True, capture_output=True, check=False)
        self.assertEqual(out.returncode, 75)
        self.assertEqual(json.loads(out.stdout)["frontier_sequence"], 0)

    def test_missing_terminal_event_or_orphan_verification_blocks(self):
        root = self.init()
        m.create_only(root / "days" / "000001.json", b"{}\n")
        with self.assertRaisesRegex(m.EvidenceError, "orphan"):
            m.inspect(root)

    def test_plan_corruption_is_detected_even_if_terminal_hash_recomputed(self):
        root = self.init()
        write_terminal(root, 1)
        day_path = root / "days/000001.json"
        value = m.load_json(day_path)
        value["plan"]["primary_lifecycles"] = 99
        day_path.write_bytes(m.canonical_bytes(value))
        events = m.read_events(root / "events.jsonl", root.name)
        events[-1]["detail"]["day_sha256"] = m.sha256(day_path)
        (root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        with self.assertRaisesRegex(m.EvidenceError, "plan identity mismatch"):
            m.inspect(root)

    def test_verification_corruption_and_missing_checks_block(self):
        root = self.init()
        write_terminal(root, 1)
        verification_path = root / "days/000001.verification.json"
        record = m.load_json(verification_path)
        record["checks"].pop("settlement_close")
        verification_path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "missing verification coverage"):
            m.inspect(root)

    def test_out_of_order_gap_duplicate_terminal_and_truncated_jsonl_block(self):
        root = self.init()
        m.append_event(root, 2, "day-started", {})
        with self.assertRaisesRegex(m.EvidenceError, "gap"):
            m.inspect(root)
        other = self.base / "independent"
        changed_identity = {**self.identity, "campaign_id": "test319"}
        root2 = m.initialize(other, changed_identity, self.provenance)
        m.append_event(root2, 1, "day-started", {})
        m.append_event(root2, 1, "day-started", {})
        with self.assertRaisesRegex(m.EvidenceError, "gap/duplicate"):
            m.inspect(root2)
        (root2 / "events.jsonl").write_bytes((root2 / "events.jsonl").read_bytes() + b'{"truncated":')
        with self.assertRaisesRegex(m.EvidenceError, "truncated JSONL"):
            m.inspect(root2)

    def test_lifecycle_phases_and_controller_restart_boundary(self):
        root = self.init()
        m.append_event(root, 0, "controller-started", {})
        m.append_event(root, 1, "day-started", {})
        for phase in ("traffic", "fault", "batch", "crash", "recovery", "reconciliation", "statement", "close", "quiescence"):
            m.append_event(root, 1, phase, {})
        self.assertEqual(m.inspect(root)["decision"], "BLOCK")
        write_terminal(root, 1, started=False)
        m.append_event(root, 0, "controller-stopped", {})
        m.append_event(root, 0, "controller-started", {})
        self.assertEqual(m.inspect(root)["frontier_sequence"], 1)
        with self.assertRaisesRegex(m.EvidenceError, "unrecognized"):
            m.append_event(root, 2, "skip-verification", {})

    def test_failure_artifact_and_terminal_failure_fail_closed(self):
        root = self.init()
        m.append_event(root, 1, "day-started", {})
        m.append_event(root, 1, "day-terminal-failure", {})
        self.assertEqual(m.inspect(root)["decision"], "BLOCK")
        other = self.base / "other"
        root2 = m.initialize(other, {**self.identity, "campaign_id": "fail318"}, self.provenance)
        (root2 / "failures" / "000001.json").write_text("{}\n")
        self.assertEqual(m.inspect(root2)["decision"], "BLOCK")

    def test_cli_plan_output_is_not_runtime_execution(self):
        out = subprocess.run([sys.executable, str(HERE / "campaign-evidence.py"), "plan", "--day", "10"], text=True, capture_output=True, check=False)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout), m.plan_day(10))
        self.assertEqual(list(self.base.iterdir()), [self.provenance])


if __name__ == "__main__":
    unittest.main(verbosity=2)
