#!/usr/bin/env python3

import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("discover-msrv-workloads.py")
SPEC = importlib.util.spec_from_file_location("discover_msrv_workloads", MODULE_PATH)
assert SPEC and SPEC.loader
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)

DECLARED = {"declared": True}
UNDECLARED = {"declared": False, "policy_reason": "no MSRV contract"}


class DeclaredMsrvDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def project(self, name: str, *, rust_version: str | None) -> None:
        path = self.root / name
        path.mkdir()
        manifest = '[package]\nname = "fixture"\nversion = "0.0.0"\n'
        if rust_version is not None:
            manifest += f'rust-version = "{rust_version}"\n'
        (path / "Cargo.toml").write_text(manifest, encoding="utf-8")
        ci = path / "ci"
        ci.mkdir()
        entrypoint = ci / "validate"
        entrypoint.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        entrypoint.chmod(entrypoint.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def write_registry(self, *, undeclared_count: int = 1) -> None:
        workloads = [
            {"name": "alpha", "path": "alpha", "msrv": DECLARED},
            {"name": "beta", "path": "beta", "msrv": DECLARED},
        ]
        fixtures = [
            {
                "name": f"fixture-{index}",
                "path": f"fixture-{index}",
                "msrv": UNDECLARED,
            }
            for index in range(undeclared_count)
        ]
        (self.root / "workloads.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "workloads": workloads,
                    "fixtures": fixtures,
                    "reserved_top_level_cargo_projects": [],
                }
            ),
            encoding="utf-8",
        )

    def test_execution_projection_contains_only_declared_entries(self) -> None:
        self.project("alpha", rust_version="1.90")
        self.project("beta", rust_version="1.91")
        self.project("fixture-0", rust_version=None)
        self.write_registry()
        matrix = discovery.discover(self.root)
        self.assertEqual([entry["name"] for entry in matrix["include"]], ["alpha", "beta"])
        self.assertTrue(all(entry["msrv"]["declared"] for entry in matrix["include"]))

    def test_multiple_undeclared_entries_never_create_runner_entries(self) -> None:
        self.project("alpha", rust_version="1.90")
        self.project("beta", rust_version="1.91")
        for index in range(3):
            self.project(f"fixture-{index}", rust_version=None)
        self.write_registry(undeclared_count=3)
        matrix = discovery.discover(self.root)
        self.assertEqual([entry["name"] for entry in matrix["include"]], ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
