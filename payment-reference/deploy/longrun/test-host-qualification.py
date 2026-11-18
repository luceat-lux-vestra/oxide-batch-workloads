#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_module("capture_provenance", "capture-provenance.py")
admission = load_module("campaign_admission", "check-admission.py")
guard = load_module("disk_guard", "disk-guard.py")

GIB = 1024 * 1024 * 1024
SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeRunner:
    def __init__(self, compose_path: Path) -> None:
        self.compose_path = str(compose_path)
        self.docker_version = "27.3.1"

    def __call__(self, command: list[str], _cwd: Path | None) -> str:
        key = tuple(command)
        values = {
            ("git", "rev-parse", "HEAD"): SHA,
            ("git", "status", "--porcelain"): "",
            ("findmnt", "-n", "-o", "FSTYPE", "--target"): "ext4",
            ("docker", "info", "--format", "{{.Architecture}}"): "aarch64",
            ("docker", "version", "--format", "{{.Server.Version}}"): self.docker_version,
            ("docker", "compose", "version", "--short"): "2.29.7",
            ("git", "--version"): "git version 2.46.0",
            ("python3", "--version"): "Python 3.12.3",
            ("rustc", "--version"): "rustc 1.91.1",
            ("cargo", "--version"): "cargo 1.91.1",
            ("java", "-version"): "openjdk version 17.0.13",
            ("mvn", "-version"): "Apache Maven 3.9.9",
        }
        if key[:5] == ("findmnt", "-n", "-o", "FSTYPE", "--target"):
            return "ext4"
        if key == ("docker", "compose", "-f", self.compose_path, "config"):
            return "services:\n  merchant:\n    image: local-merchant\n"
        if key == ("docker", "compose", "-f", self.compose_path, "config", "--services"):
            return "merchant\n"
        if key == ("docker", "compose", "-f", self.compose_path, "ps", "--format", "json"):
            return json.dumps(
                [
                    {
                        "Service": "merchant",
                        "Name": "oxide-payment-reference-merchant-1",
                        "ID": "container-abc",
                        "Image": "local-merchant",
                        "State": "running",
                        "Health": "",
                    }
                ]
            )
        if key == ("docker", "inspect", "oxide-payment-reference-merchant-1", "--format", "{{json .}}"):
            return json.dumps(
                {
                    "Id": "container-abc-full",
                    "RestartCount": 0,
                    "State": {"OOMKilled": False, "Running": True},
                }
            )
        if key == ("docker", "image", "inspect", "local-merchant", "--format", "{{json .}}"):
            return json.dumps(
                {
                    "Id": "sha256:abc",
                    "Architecture": "arm64",
                    "Os": "linux",
                    "RepoDigests": ["local-merchant@sha256:def"],
                }
            )
        if key in values:
            return values[key]
        raise capture.QualificationError(f"unexpected fake command: {command!r}")


class HostQualificationTests(unittest.TestCase):
    def test_contract_freezes_issue_318_resource_and_guard_floors(self) -> None:
        contract = capture.load_contract(HERE / "host-qualification-contract.json")
        self.assertEqual(contract["campaign_issue"], 318)
        self.assertEqual(contract["campaign_contract"], "payment-reference-longrun-v1")
        self.assertEqual(contract["schema_version"], 2)
        self.assertEqual(contract["host_profile"], "oci-free-shared-arm64-linux-v1")
        self.assertEqual(contract["minimum_vcpu"], 2)
        self.assertEqual(contract["minimum_memory_bytes"], 11 * GIB)
        self.assertEqual(contract["minimum_disk_total_bytes"], 120 * GIB)
        self.assertEqual(contract["minimum_disk_free_bytes"], 40 * GIB)
        self.assertEqual(contract["campaign_admission"]["minimum_memory_available_bytes"], 7 * GIB)
        self.assertEqual(contract["campaign_admission"]["maximum_swap_used_bytes"], 512 * 1024 * 1024)
        self.assertEqual(contract["disk_guard"]["minimum_free_percent"], 20)
        self.assertEqual(contract["disk_guard"]["minimum_free_bytes"], 20 * GIB)
        self.assertEqual(contract["qualification"]["consecutive_smoke_runs"], 3)
        self.assertEqual(contract["qualification"]["stack_restart_smoke_runs"], 1)
        self.assertNotIn("post_reboot_smoke_runs", contract["qualification"])
        self.assertEqual(contract["qualification"]["pre_campaign_quiescence_seconds"], 1800)

    def test_resource_contract_is_fail_closed(self) -> None:
        contract = capture.load_contract(HERE / "host-qualification-contract.json")
        capture.validate_host_resources(
            contract,
            architecture="aarch64",
            vcpu=2,
            memory_total_bytes=11 * GIB,
            disk_total_bytes=120 * GIB,
            disk_free_bytes=40 * GIB,
            host_sleep_disabled_attested=True,
        )
        cases = [
            dict(architecture="x86_64", vcpu=2, memory_total_bytes=11 * GIB, disk_total_bytes=120 * GIB, disk_free_bytes=40 * GIB, host_sleep_disabled_attested=True),
            dict(architecture="aarch64", vcpu=1, memory_total_bytes=11 * GIB, disk_total_bytes=120 * GIB, disk_free_bytes=40 * GIB, host_sleep_disabled_attested=True),
            dict(architecture="aarch64", vcpu=2, memory_total_bytes=11 * GIB - 1, disk_total_bytes=120 * GIB, disk_free_bytes=40 * GIB, host_sleep_disabled_attested=True),
            dict(architecture="aarch64", vcpu=2, memory_total_bytes=11 * GIB, disk_total_bytes=120 * GIB - 1, disk_free_bytes=40 * GIB, host_sleep_disabled_attested=True),
            dict(architecture="aarch64", vcpu=2, memory_total_bytes=11 * GIB, disk_total_bytes=120 * GIB, disk_free_bytes=40 * GIB - 1, host_sleep_disabled_attested=True),
            dict(architecture="aarch64", vcpu=2, memory_total_bytes=11 * GIB, disk_total_bytes=120 * GIB, disk_free_bytes=40 * GIB, host_sleep_disabled_attested=False),
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(capture.QualificationError):
                    capture.validate_host_resources(contract, **case)

    def test_compose_ps_parser_accepts_array_and_ndjson(self) -> None:
        items = [
            {"Service": "merchant", "Name": "p-merchant-1", "ID": "c2", "Image": "m", "State": "running", "Health": ""},
            {"Service": "gateway", "Name": "p-gateway-1", "ID": "c1", "Image": "g", "State": "running", "Health": "healthy"},
        ]
        array = capture.parse_compose_ps(json.dumps(items))
        ndjson = capture.parse_compose_ps("\n".join(json.dumps(item) for item in items))
        self.assertEqual(array, ndjson)
        self.assertEqual([item["service"] for item in array], ["gateway", "merchant"])
        stopped = [{"Service": "merchant", "Name": "p-merchant-1", "ID": "c2", "Image": "m", "State": "exited"}]
        with self.assertRaises(capture.QualificationError):
            capture.parse_compose_ps(json.dumps(stopped))

    def test_immutable_write_replays_exact_bytes_and_conflicts_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host.json"
            first = {"schema": "test", "value": 1}
            self.assertEqual(capture.immutable_write(path, first), "created")
            before = path.read_bytes()
            self.assertEqual(capture.immutable_write(path, first), "replay")
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(capture.ProvenanceConflict):
                capture.immutable_write(path, {"schema": "test", "value": 2})
            self.assertEqual(path.read_bytes(), before)

    def test_capture_writes_four_stable_documents_and_detects_runtime_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repo"
            deploy = repo_root / "payment-reference" / "deploy"
            longrun = deploy / "longrun"
            evidence = root / "evidence"
            longrun.mkdir(parents=True)
            compose = deploy / "compose.yaml"
            compose.write_text("services: {}\n", encoding="utf-8")
            (deploy / "third-party.lock.json").write_text('{"schema_version":3}\n', encoding="utf-8")
            contract_source = HERE / "host-qualification-contract.json"
            contract = longrun / "host-qualification-contract.json"
            contract.write_bytes(contract_source.read_bytes())
            runner = FakeRunner(compose.resolve())

            with (
                patch.object(capture.platform, "machine", return_value="aarch64"),
                patch.object(capture.platform, "release", return_value="6.8.0-test"),
                patch.object(capture.os, "cpu_count", return_value=8),
                patch.object(capture, "read_meminfo", return_value=(32 * GIB, 4 * GIB)),
                patch.object(capture, "parse_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}),
                patch.object(capture.shutil, "disk_usage", return_value=SimpleNamespace(total=160 * GIB, used=80 * GIB, free=80 * GIB)),
            ):
                first = capture.capture(
                    repo_root=repo_root,
                    evidence_dir=evidence,
                    contract_path=contract,
                    compose_path=compose,
                    host_sleep_disabled_attested=True,
                    runner=runner,
                )
                self.assertEqual(set(first.values()), {"created"})
                second = capture.capture(
                    repo_root=repo_root,
                    evidence_dir=evidence,
                    contract_path=contract,
                    compose_path=compose,
                    host_sleep_disabled_attested=True,
                    runner=runner,
                )
                self.assertEqual(set(second.values()), {"replay"})
                self.assertEqual(
                    sorted(path.name for path in evidence.iterdir()),
                    ["container-images.json", "host.json", "runtime.json", "toolchains.json"],
                )
                host = json.loads((evidence / "host.json").read_text())
                self.assertEqual(host["repository_sha"], SHA)
                self.assertEqual(host["host_profile"], "oci-free-shared-arm64-linux-v1")
                self.assertEqual(host["vcpu"], 8)
                runtime = json.loads((evidence / "runtime.json").read_text())
                self.assertEqual(runtime["compose_services"], ["merchant"])
                images = json.loads((evidence / "container-images.json").read_text())
                self.assertEqual(images["images"][0]["service"], "merchant")
                self.assertEqual(images["images"][0]["container_id"], "container-abc-full")
                self.assertEqual(images["images"][0]["restart_count"], 0)
                self.assertFalse(images["images"][0]["oom_killed"])
                self.assertEqual(images["images"][0]["image_ref"], "local-merchant")
                self.assertEqual(images["images"][0]["image_id"], "sha256:abc")
                self.assertEqual(images["images"][0]["architecture"], "arm64")

                runner.docker_version = "27.3.2"
                with self.assertRaises(capture.ProvenanceConflict):
                    capture.capture(
                        repo_root=repo_root,
                        evidence_dir=evidence,
                        contract_path=contract,
                        compose_path=compose,
                        host_sleep_disabled_attested=True,
                        runner=runner,
                    )

    def test_container_restart_or_oom_blocks_provenance(self) -> None:
        services = [
            {
                "service": "merchant",
                "container_name": "merchant-1",
                "container_id": "short",
                "image_ref": "local-merchant",
                "state": "running",
                "health": "",
            }
        ]

        def runner_with(restart_count: int, oom_killed: bool):
            def run(command, _cwd):
                if command[:2] == ["docker", "inspect"]:
                    return json.dumps(
                        {
                            "Id": "container-full",
                            "RestartCount": restart_count,
                            "State": {"OOMKilled": oom_killed, "Running": True},
                        }
                    )
                if command[:3] == ["docker", "image", "inspect"]:
                    return json.dumps(
                        {
                            "Id": "sha256:abc",
                            "Architecture": "arm64",
                            "Os": "linux",
                            "RepoDigests": [],
                        }
                    )
                raise capture.QualificationError(f"unexpected command: {command!r}")

            return run

        with self.assertRaises(capture.QualificationError):
            capture.inspect_images(services, ["merchant"], Path("."), runner_with(1, False))
        with self.assertRaises(capture.QualificationError):
            capture.inspect_images(services, ["merchant"], Path("."), runner_with(0, True))

    def test_persistent_smoke_namespace_preserves_default_and_fault_matrix_contract(self) -> None:
        smoke = (HERE.parent / "smoke.sh").read_text(encoding="utf-8")
        fault = (HERE.parent / "fault-matrix-smoke.sh").read_text(encoding="utf-8")
        self.assertIn('smoke_namespace="${PAYMENT_REFERENCE_SMOKE_NAMESPACE:-}"', smoke)
        self.assertIn("namespace_request()", smoke)
        self.assertIn("qualified_id()", smoke)
        self.assertIn('PAYMENT_REFERENCE_SMOKE_NAMESPACE="$smoke_namespace"', smoke)
        self.assertIn('reuse_tenant="${PAYMENT_REFERENCE_REUSE_TENANT:-false}"', smoke)
        self.assertIn('skip_build="${PAYMENT_REFERENCE_SKIP_BUILD:-false}"', smoke)
        self.assertIn('if [ "$skip_build" = "true" ]; then', smoke)
        self.assertIn('if [ "$reuse_tenant" = "false" ]; then', smoke)
        self.assertIn('--arg smoke_namespace "$smoke_namespace"', smoke)
        self.assertIn('smoke_namespace="${PAYMENT_REFERENCE_SMOKE_NAMESPACE:-}"', fault)
        self.assertIn('pre_id="$(qualified_id provider-fault-plan-503)"', fault)
        self.assertIn('drop_id="$(qualified_id provider-fault-plan-drop)"', fault)
        self.assertNotIn('pre_id="provider-fault-plan-503"', fault)
        self.assertNotIn('drop_id="provider-fault-plan-drop"', fault)

    def test_shared_host_campaign_admission_is_fail_closed(self) -> None:
        contract = admission.load_contract(HERE / "host-qualification-contract.json")
        allowed = admission.evaluate(
            contract,
            architecture="aarch64",
            vcpu=2,
            memory_total_bytes=12 * GIB,
            memory_available_bytes=8 * GIB,
            swap_total_bytes=2 * GIB,
            swap_free_bytes=2 * GIB,
            disk_total_bytes=150 * GIB,
            disk_free_bytes=80 * GIB,
            data_path="/srv/oxide-batch-workloads",
        )
        self.assertEqual(allowed["decision"], "ALLOW")

        low_memory = admission.evaluate(
            contract,
            architecture="aarch64",
            vcpu=2,
            memory_total_bytes=12 * GIB,
            memory_available_bytes=6 * GIB,
            swap_total_bytes=2 * GIB,
            swap_free_bytes=2 * GIB,
            disk_total_bytes=150 * GIB,
            disk_free_bytes=80 * GIB,
            data_path="/srv/oxide-batch-workloads",
        )
        self.assertEqual(low_memory["decision"], "BLOCK")

        swap_pressure = admission.evaluate(
            contract,
            architecture="aarch64",
            vcpu=2,
            memory_total_bytes=12 * GIB,
            memory_available_bytes=8 * GIB,
            swap_total_bytes=2 * GIB,
            swap_free_bytes=1 * GIB,
            disk_total_bytes=150 * GIB,
            disk_free_bytes=80 * GIB,
            data_path="/srv/oxide-batch-workloads",
        )
        self.assertEqual(swap_pressure["decision"], "BLOCK")

    def test_disk_guard_matches_203_policy(self) -> None:
        allowed = guard.evaluate(100 * GIB, 25 * GIB, 20, 20 * GIB)
        self.assertEqual(allowed["decision"], "ALLOW")
        percent_block = guard.evaluate(100 * GIB, 19 * GIB, 20, 10 * GIB)
        self.assertEqual(percent_block["decision"], "BLOCK")
        absolute_block = guard.evaluate(200 * GIB, 30 * GIB, 10, 40 * GIB)
        self.assertEqual(absolute_block["decision"], "BLOCK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
