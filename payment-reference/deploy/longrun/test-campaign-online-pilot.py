#!/usr/bin/env python3
"""Offline/strict negative controls for one-slot write-ahead Merchant pilot."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("payment_reference_online_pilot", HERE / "campaign-online-pilot.py")
assert spec is not None and spec.loader is not None
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def normal_slot(plan, skip=0):
    return next(row["slot"] for row in plan["slots"] if row["kind"] in pilot.NORMAL and row["slot"] > skip)


class FakeMerchant:
    """No network, exact response for the real normal Merchant request contract."""
    def __init__(self, plan, slot):
        self.plan = plan
        self.row = plan["slots"][slot - 1]
        self.requests = []
        self.fail_at = None
        self.override_status = None

    def post(self, base, path, body):
        self.requests.append((base, path, body))
        if self.fail_at == len(self.requests):
            raise OSError("simulated lost response after an unknown side effect")
        if self.override_status is not None and self.override_status[0] == len(self.requests):
            return self.override_status[1], b'{"error":"ambiguous"}'
        if path in ("/merchants", "/customers", "/orders"):
            return 201, b""
        if path.endswith("/payments"):
            data = {
                "merchantPaymentId": self.row["merchant_payment_id"],
                "merchantOrderId": self.row["merchant_order_id"],
                "paymentState": "CREATED",
                "amountMinor": 1000,
                "currency": "USD",
            }
            return 201, json.dumps(data).encode()
        for index, request in enumerate(self.row["mutations"]):
            if request["path"] == path and request["body"] == body:
                data = {
                    "merchantPaymentId": self.row["merchant_payment_id"],
                    "merchantOrderId": self.row["merchant_order_id"],
                    "paymentState": pilot.NORMAL[self.row["kind"]][index],
                    "operationId": body["operationId"],
                    "operationState": "SUCCEEDED",
                    "mapping": {"providerPaymentId": "test-provider"},
                }
                return 200, json.dumps(data).encode()
        raise AssertionError(f"unexpected Merchant POST {path}")

    def get(self, base, path):
        assert base == pilot.BASE
        assert path == f'/payments/{self.row["merchant_payment_id"]}'
        return {
            "merchantPaymentId": self.row["merchant_payment_id"],
            "merchantOrderId": self.row["merchant_order_id"],
            "amountMinor": 1000,
            "currency": "USD",
            "paymentState": self.row["expected_normal_terminal_state"],
        }


class OnlinePilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = pilot.traffic.build("pilot318", 1)
        self.slot = normal_slot(self.plan)
        self.fake = FakeMerchant(self.plan, self.slot)
        self.source_sha = "a" * 40
        self.provenance = {name: "b" * 64 for name in pilot.PROVENANCE_FILES}

    def execute(self, *, slot=None, fake=None, sha=None, provenance=None, get=None):
        index = self.slot if slot is None else slot
        target = self.fake if fake is None else fake
        return pilot.run_one(
            plan=self.plan, slot=index, evidence_root=self.root,
            source_sha=self.source_sha if sha is None else sha,
            provenance_hashes=self.provenance if provenance is None else provenance,
            post=target.post,
            get=target.get if get is None else get,
        )

    def day(self):
        return self.root / "pilot318-day000001"

    def test_fully_acknowledged_pilot_is_not_financial_acceptance(self):
        r = self.execute()
        self.assertEqual(r["decision"], "ONE_SLOT_EXECUTED_NOT_FINANCIALLY_VERIFIED")
        self.assertTrue(r["independent_financial_verification_required"])
        self.assertEqual(r["requests"], 2 + len(self.fake.row["mutations"]))
        self.assertEqual(len(self.fake.requests), 4 + len(self.fake.row["mutations"]))
        self.assertEqual(r["slot"], self.slot)
        item_dir = self.day() / f"slot-{self.slot:03}"
        for ordinal in range(1, 3 + len(self.fake.row["mutations"])):
            intent, response = pilot.step_files(item_dir, ordinal)
            self.assertTrue(intent.exists())
            self.assertTrue(response.exists())
            self.assertEqual(pilot.read_object(intent)["ordinal"], ordinal)
            self.assertIn("body_sha256", pilot.read_object(response))
        with self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual(len(self.fake.requests), 4 + len(self.fake.row["mutations"]))

    def test_each_of_four_normal_lifecycle_sequences(self):
        seen = set()
        for kind in sorted(pilot.NORMAL):
            slot = next(row["slot"] for row in self.plan["slots"] if row["kind"] == kind)
            self.assertNotIn(slot, seen)
            seen.add(slot)
            fake = FakeMerchant(self.plan, slot)
            result = self.execute(slot=slot, fake=fake)
            self.assertEqual(result["kind"], kind)
            self.assertEqual(result["requests"], 2 + len(fake.row["mutations"]))
            expected = 2 + len(fake.row["mutations"]) + (2 if len(seen) == 1 else 0)
            self.assertEqual(len(fake.requests), expected)
            slot_folder = self.day() / f"slot-{slot:03}"
            for ordinal, mutation in enumerate(fake.row["mutations"], start=3):
                acknowledgement = pilot.read_object(pilot.step_files(slot_folder, ordinal)[1])
                self.assertEqual(acknowledgement["status"], 200)
                self.assertEqual(
                    acknowledgement["json_body"]["operationId"],
                    mutation["body"]["operationId"],
                )
                self.assertEqual(
                    acknowledgement["json_body"]["paymentState"],
                    pilot.NORMAL[kind][ordinal - 3],
                )
        self.assertEqual(seen.__len__(), 4)

    def test_next_slot_reuses_only_complete_day_setup(self):
        self.execute()
        second = normal_slot(self.plan, skip=self.slot)
        other = FakeMerchant(self.plan, second)
        result = self.execute(slot=second, fake=other)
        self.assertEqual(result["slot"], second)
        self.assertEqual(other.requests[0][1], "/orders")
        self.assertEqual(len(other.requests), 2 + len(other.row["mutations"]))
        self.assertEqual({p.name for p in self.day().iterdir()}, {".lock", "identity.json", "setup", f"slot-{self.slot:03}", f"slot-{second:03}"})

    def test_transport_ambiguity_is_never_replayed(self):
        self.fake.fail_at = 3  # Merchant and customer 201, order request unknown.
        with self.assertRaisesRegex(pilot.Block, "transport outcome unknown"):
            self.execute()
        item_dir = self.day() / f"slot-{self.slot:03}"
        a, b = pilot.step_files(item_dir, 1)
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        before = len(self.fake.requests)
        with self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual(before, len(self.fake.requests))

    def test_incomplete_shared_day_setup_blocks_all_future_slots(self):
        self.fake.fail_at = 2  # Merchant created, customer outcome unknown.
        with self.assertRaises(pilot.Block):
            self.execute()
        second = normal_slot(self.plan, skip=self.slot)
        other = FakeMerchant(self.plan, second)
        with self.assertRaisesRegex(pilot.Block, "ambiguous/incomplete Merchant day setup"):
            self.execute(slot=second, fake=other)
        self.assertEqual(other.requests, [])

    def test_http202_is_durable_and_blocks_automatic_replay(self):
        # Setup 2 + order 1 + prepare 1 + operation 1.
        self.fake.override_status = (5, 202)
        with self.assertRaisesRegex(pilot.Block, "HTTP 202"):
            self.execute()
        item_dir = self.day() / f"slot-{self.slot:03}"
        intent, ack = pilot.step_files(item_dir, 3)
        self.assertTrue(intent.exists())
        self.assertEqual(pilot.read_object(ack)["status"], 202)
        before = len(self.fake.requests)
        with self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual(before, len(self.fake.requests))

    def test_setup_conflict_is_not_treated_as_idempotent_success(self):
        self.fake.override_status = (1, 409)
        with self.assertRaisesRegex(pilot.Block, "HTTP 409"):
            self.execute()
        next_slot = normal_slot(self.plan, skip=self.slot)
        other = FakeMerchant(self.plan, next_slot)
        with self.assertRaisesRegex(pilot.Block, "setup did not finish"):
            self.execute(slot=next_slot, fake=other)
        self.assertEqual(other.requests, [])

    def test_day_identity_drift_blocks_all_additional_writes(self):
        self.execute()
        second = normal_slot(self.plan, skip=self.slot)
        other = FakeMerchant(self.plan, second)
        with self.assertRaisesRegex(pilot.Block, "drift"):
            self.execute(slot=second, fake=other, sha="c" * 40)
        with self.assertRaisesRegex(pilot.Block, "drift"):
            self.execute(slot=second, fake=other, provenance={**self.provenance, "host.json": "c" * 64})
        self.assertEqual(other.requests, [])

    def test_get_drift_blocks_and_keeps_slot_taken(self):
        def wrong_get(base, path):
            return {**self.fake.get(base, path), "paymentState": "PAYMENT_PENDING"}
        with self.assertRaisesRegex(pilot.Block, "final Merchant GET state mismatch"):
            self.execute(get=wrong_get)
        with self.assertRaises(FileExistsError):
            self.execute()

    def test_fault_overlay_never_creates_business_writes(self):
        fault_slot = next(x["slot"] for x in self.plan["slots"] if x["kind"] == "fault_overlay")
        with self.assertRaisesRegex(pilot.Block, "fault overlay"):
            self.execute(slot=fault_slot)
        self.assertFalse(list(self.root.iterdir()))

    def test_evidence_root_symlink_is_rejected_before_http(self):
        linked = self.root / "redirect-evidence"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(pilot.Block, "must not be a symlink"):
            pilot.run_one(
                plan=self.plan, slot=self.slot, evidence_root=linked,
                source_sha=self.source_sha, provenance_hashes=self.provenance,
                post=self.fake.post, get=self.fake.get,
            )
        self.assertEqual(self.fake.requests, [])
        self.assertFalse((self.root / "pilot318-day000001").exists())

    def test_plan_default_is_explicitly_nonexecuting(self):
        p = subprocess.run([
            sys.executable, str(HERE / "campaign-online-pilot.py"), "--campaign-id", "pilot318",
            "--day", "1", "--slot", str(self.slot)
        ], text=True, capture_output=True, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result["decision"], "PLAN_ONLY")
        self.assertTrue(result["no_http_mutations"])
        self.assertFalse(list(self.root.iterdir()))

    def test_qualified_source_requires_exact_profile_and_service_set(self):
        prov_dir = self.root / "new-provenance"
        prov_dir.mkdir()
        documents = {
            "host.json": {"repository_sha": self.source_sha, "host_profile": "oci-shared-arm64-linux-4vcpu-24gb-v1"},
            "runtime.json": {"repository_sha": self.source_sha, "compose_services": sorted(pilot.COMPOSE_SERVICES)},
            "container-images.json": {"repository_sha": self.source_sha},
            "toolchains.json": {"repository_sha": self.source_sha},
        }
        for name, value in documents.items():
            (prov_dir / name).write_bytes(pilot.stable(value))

        def git(args, **kwargs):
            output = self.source_sha + "\n" if args[-2:] == ["rev-parse", "HEAD"] else ""
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with patch.object(pilot.subprocess, "run", side_effect=git):
            qualified = pilot.qualified_source(self.source_sha, prov_dir)
            self.assertEqual(set(qualified), set(pilot.PROVENANCE_FILES))
            documents["runtime.json"]["compose_services"][-1] = "unexpected-service"
            (prov_dir / "runtime.json").write_bytes(pilot.stable(documents["runtime.json"]))
            with self.assertRaisesRegex(pilot.Block, "exact eight-service"):
                pilot.qualified_source(self.source_sha, prov_dir)
            (prov_dir / "runtime.json").write_bytes(pilot.stable({
                "repository_sha": self.source_sha,
                "compose_services": sorted(pilot.COMPOSE_SERVICES),
            }))
            documents["host.json"]["host_profile"] = "oci-free-shared-arm64-linux-v1"
            (prov_dir / "host.json").write_bytes(pilot.stable(documents["host.json"]))
            with self.assertRaisesRegex(pilot.Block, "upgraded OCI host"):
                pilot.qualified_source(self.source_sha, prov_dir)

    def test_completed_slot_cannot_replay_from_a_new_python_process(self):
        self.execute()
        script = """
import importlib.util, pathlib, sys
source, root, slot, revision = sys.argv[1:]
spec = importlib.util.spec_from_file_location('pilot_fresh', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
plan = module.traffic.build('pilot318', 1)
def forbid(*args):
    raise AssertionError('unexpected network on restart')
try:
    module.run_one(
        plan=plan, slot=int(slot), evidence_root=pathlib.Path(root),
        source_sha=revision, provenance_hashes={name: 'b'*64 for name in module.PROVENANCE_FILES},
        post=forbid, get=forbid,
    )
except FileExistsError:
    raise SystemExit(0)
raise SystemExit(2)
"""
        r = subprocess.run([
            sys.executable, "-c", script, str(HERE / "campaign-online-pilot.py"),
            str(self.root), str(self.slot), self.source_sha,
        ], text=True, capture_output=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_live_topology_rejects_stale_images_restarts_and_unhealthy(self):
        prov_dir = self.root / "live-provenance"
        prov_dir.mkdir()
        services = sorted(pilot.COMPOSE_SERVICES)
        expected = [
            {
                "service": name,
                "container_id": f"container-{name}",
                "image_id": f"sha256:{i+1:064x}",
                "image_ref": f"ghcr.io/test/{name}@sha256:{i+101:064x}",
            }
            for i, name in enumerate(services)
        ]
        (prov_dir / "container-images.json").write_bytes(
            pilot.stable({"images": expected})
        )
        def inspection(overrides=None):
            overrides = overrides or {}
            values = []
            for source in expected:
                name = source["service"]
                entry = {
                    "Id": source["container_id"], "Image": source["image_id"],
                    "RestartCount": 0,
                    "Config": {"Labels": {
                        "com.docker.compose.project": "oxide-payment-reference",
                        "com.docker.compose.service": name,
                    }},
                    "State": {
                        "Running": True, "OOMKilled": False,
                        "Health": {"Status": "healthy"},
                    },
                }
                if name == "merchant":
                    entry["NetworkSettings"] = {"Ports": {
                        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18083"}],
                    }}
                entry.update(overrides.get(name, {}))
                values.append(entry)
            return values

        def run_for(containers):
            calls = []
            def fake_run(args, **kwargs):
                calls.append(args)
                if args[:3] == ["docker", "ps", "-aq"]:
                    return subprocess.CompletedProcess(args, 0, stdout="\n".join(
                        f"container-{name}" for name in services
                    ), stderr="")
                if args[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(args, 0, stdout=json.dumps(containers), stderr="")
                raise AssertionError("unexpected docker command")
            with patch.object(pilot.subprocess, "run", side_effect=fake_run):
                result = pilot.attest_live_topology(prov_dir)
            return result, calls

        identities, calls = run_for(inspection())
        self.assertEqual(set(identities), set(services))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], "label=com.docker.compose.project=oxide-payment-reference")
        self.assertEqual(calls[1][:2], ["docker", "inspect"])
        for scenario in (
            {"merchant": {"Image": "sha256:" + "e"*64}},
            {"merchant": {"RestartCount": 1}},
            {"merchant": {"State": {"Running": True, "OOMKilled": True, "Health": {"Status": "healthy"}}}},
            {"gateway-db": {"State": {"Running": True, "OOMKilled": False, "Health": {"Status": "unhealthy"}}}},
            {"merchant": {"NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18083"}]}}}},
            {"merchant": {"NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18084"}]}}}},
            {"merchant": {"NetworkSettings": {"Ports": {"8080/tcp": []}}}},
            {"merchant": {"Id": "foreign-container"}},
            {"merchant": {"Config": {"Labels": {
                "com.docker.compose.project": "unrelated-project",
                "com.docker.compose.service": "merchant",
            }}}},
        ):
            with self.subTest(scenario=scenario), self.assertRaises(pilot.Block):
                run_for(inspection(scenario))

        bad = json.loads((prov_dir / "container-images.json").read_text())
        bad["images"][-1]["image_ref"] = "unversioned:latest"
        (prov_dir / "container-images.json").write_bytes(pilot.stable(bad))
        with self.assertRaisesRegex(pilot.Block, "mutable provenance image"):
            pilot.attest_live_topology(prov_dir)

    def test_no_provenance_cannot_enable_execution(self):
        p = subprocess.run([
            sys.executable, str(HERE / "campaign-online-pilot.py"), "--campaign-id", "pilot318",
            "--day", "1", "--slot", str(self.slot), "--execute-one",
        ], text=True, capture_output=True, check=False)
        self.assertEqual(p.returncode, 75)
        self.assertEqual(json.loads(p.stderr)["decision"], "BLOCK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
