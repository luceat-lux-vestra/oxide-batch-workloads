#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE / "oci_host_qualification.py"
SPEC = importlib.util.spec_from_file_location("oci_host_qualification", MODULE_PATH)
assert SPEC and SPEC.loader
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


class OciHostQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = qualification.load_contract()
        self.instance = {"shape": "VM.Standard.A1.Flex"}
        self.shape = {"ocpus": 4.0, "memoryInGBs": 24.0}
        self.disk = {
            "filesystem": "ext4",
            "total_bytes": 200 * 1024**3,
            "available_bytes": 150 * 1024**3,
        }

    def test_contract_freezes_current_campaign_subject_and_candidates(self) -> None:
        self.assertEqual(self.contract["campaign"], 323)
        self.assertEqual(self.contract["subject"]["version"], "0.7.0")
        self.assertEqual(
            self.contract["subject"]["checksum"],
            "cb884d99629b92fc6740a187a2ebb65350f8924a57f0c8103bad26192f17652e",
        )
        self.assertEqual(
            self.contract["allocators"],
            ["system", "jemalloc", "mimalloc", "snmalloc", "tcmalloc"],
        )

    def test_contract_requires_existing_oci_a1_host_geometry(self) -> None:
        self.assertEqual(self.contract["oci"]["required_shape"], "VM.Standard.A1.Flex")
        self.assertEqual(self.contract["oci"]["min_ocpus"], 4)
        self.assertEqual(self.contract["oci"]["min_memory_gbs"], 24)
        self.assertEqual(self.contract["host"]["required_machine"], "aarch64")
        self.assertEqual(self.contract["host"]["required_root_filesystem"], "ext4")
        self.assertEqual(self.contract["host"]["max_swap_bytes"], 0)

    def test_validate_host_accepts_contract_geometry(self) -> None:
        old_system = qualification.platform.system
        old_machine = qualification.platform.machine
        qualification.platform.system = lambda: "Linux"
        qualification.platform.machine = lambda: "aarch64"
        try:
            qualification.validate_host(
                self.contract,
                self.instance,
                self.shape,
                self.disk,
                0,
            )
        finally:
            qualification.platform.system = old_system
            qualification.platform.machine = old_machine

    def test_validate_host_rejects_shape_resource_filesystem_and_swap_drift(self) -> None:
        old_system = qualification.platform.system
        old_machine = qualification.platform.machine
        qualification.platform.system = lambda: "Linux"
        qualification.platform.machine = lambda: "aarch64"
        try:
            cases = (
                ({"shape": "VM.Standard.A2.Flex"}, self.shape, self.disk, 0, "OCI shape"),
                (self.instance, {"ocpus": 2.0, "memoryInGBs": 24.0}, self.disk, 0, "OCPUs"),
                (self.instance, {"ocpus": 4.0, "memoryInGBs": 12.0}, self.disk, 0, "memory"),
                (
                    self.instance,
                    self.shape,
                    {**self.disk, "filesystem": "xfs"},
                    0,
                    "root filesystem",
                ),
                (
                    self.instance,
                    self.shape,
                    {**self.disk, "total_bytes": 100 * 1024**3},
                    0,
                    "root disk",
                ),
                (self.instance, self.shape, self.disk, 4096, "swap"),
            )
            for instance, shape, disk, swap, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(qualification.QualificationError, message):
                        qualification.validate_host(
                            self.contract,
                            instance,
                            shape,
                            disk,
                            swap,
                        )
        finally:
            qualification.platform.system = old_system
            qualification.platform.machine = old_machine

    def test_smoke_validation_requires_all_five_correct_current_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for allocator in self.contract["allocators"]:
                path = root / allocator
                path.mkdir()
                (path / "manifest.json").write_text(
                    json.dumps(
                        {
                            "campaign": 323,
                            "subject": {
                                "oxide_batch": {
                                    "version": "0.7.0",
                                    "checksum": self.contract["subject"]["checksum"],
                                }
                            },
                            "allocator": {"identity": allocator},
                            "correctness": {"verdict": "PASS"},
                        }
                    ),
                    encoding="utf-8",
                )
            qualification.validate_smoke_outputs(root, self.contract)

            bad = root / "jemalloc" / "manifest.json"
            document = json.loads(bad.read_text(encoding="utf-8"))
            document["correctness"]["verdict"] = "FAIL"
            bad.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(qualification.QualificationError, "correctness"):
                qualification.validate_smoke_outputs(root, self.contract)

    def test_claim_boundary_does_not_infer_billing(self) -> None:
        limitations = " ".join(self.contract["claim_boundary"]).lower()
        self.assertIn("billing", limitations)
        self.assertIn("not inferred", limitations)


if __name__ == "__main__":
    unittest.main()
