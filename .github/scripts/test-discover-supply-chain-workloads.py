#!/usr/bin/env python3

import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("discover-supply-chain-workloads.py")
SPEC = importlib.util.spec_from_file_location("discover_supply_chain_workloads", MODULE_PATH)
assert SPEC and SPEC.loader
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)

DECLARED_MSRV = {"declared": True}
NO_MSRV = {"declared": False, "policy_reason": "fixture: no MSRV policy"}


class SupplyChainDiscoveryTests(unittest.TestCase):
    """Proves the supply-chain discovery projection scales to N registered
    real workloads with zero source/workflow edits, and structurally
    excludes fixtures -- using synthetic temporary registries so a fake
    second workload is never permanently registered in the real
    workloads.json (#32 acceptance criterion 8).
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def cargo_project(self, name: str, rust_version: str | None = "1.90") -> None:
        path = self.root / name
        path.mkdir()
        manifest = '[package]\nname = "fixture"\nversion = "0.0.0"\n'
        if rust_version is not None:
            manifest += f'rust-version = "{rust_version}"\n'
        (path / "Cargo.toml").write_text(manifest, encoding="utf-8")
        ci_dir = path / "ci"
        ci_dir.mkdir()
        entrypoint = ci_dir / "validate"
        entrypoint.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        mode = entrypoint.stat().st_mode
        entrypoint.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def write_registry(self, workloads: list[dict], fixtures: list[dict] | None = None, schema_version: int = 3) -> None:
        (self.root / "workloads.json").write_text(
            json.dumps(
                {
                    "schema_version": schema_version,
                    "workloads": workloads,
                    "fixtures": fixtures or [],
                    "reserved_top_level_cargo_projects": [],
                }
            ),
            encoding="utf-8",
        )

    def entry(self, name: str, msrv: dict | None = None) -> dict:
        return {"name": name, "path": name, "msrv": msrv or DECLARED_MSRV}

    def test_second_real_workload_appears_automatically(self) -> None:
        # Two independent real workloads plus one fixture, registered only
        # in a synthetic temporary workloads.json -- proving the projection
        # is registry-driven, not hardcoded to today's single real workload.
        self.cargo_project("workload-a")
        self.cargo_project("workload-b")
        self.cargo_project("fixture-c", rust_version=None)
        self.write_registry(
            [self.entry("workload-a"), self.entry("workload-b")],
            fixtures=[self.entry("fixture-c", msrv=NO_MSRV)],
        )

        matrix = discovery.discover(self.root)

        names = {entry["name"] for entry in matrix["include"]}
        self.assertEqual(names, {"workload-a", "workload-b"})
        self.assertNotIn("fixture-c", names)

    def test_fixture_never_appears_as_a_supply_chain_subject(self) -> None:
        self.cargo_project("workload-a")
        self.cargo_project("fixture-only", rust_version=None)
        self.write_registry([self.entry("workload-a")], fixtures=[self.entry("fixture-only", msrv=NO_MSRV)])

        matrix = discovery.discover(self.root)

        self.assertEqual([entry["name"] for entry in matrix["include"]], ["workload-a"])

    def test_zero_real_workloads_fails_closed(self) -> None:
        self.write_registry([])
        with self.assertRaises(discovery.validator.RegistryError):
            discovery.discover(self.root)

    def test_invalid_registry_fails_closed(self) -> None:
        (self.root / "workloads.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(discovery.validator.RegistryError):
            discovery.discover(self.root)


if __name__ == "__main__":
    unittest.main()
