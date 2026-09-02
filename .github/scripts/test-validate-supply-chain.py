#!/usr/bin/env python3

import importlib.util
import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-supply-chain.py")
SPEC = importlib.util.spec_from_file_location("validate_supply_chain", MODULE_PATH)
assert SPEC and SPEC.loader
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)

REPO_ROOT = Path(__file__).resolve().parents[2]
DECLARED_MSRV = {"declared": True}
NO_MSRV = {"declared": False, "policy_reason": "fixture: no MSRV policy"}


class RegistryFixtureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def cargo_project(self, name: str, with_lockfile: bool = True, rust_version: str | None = "1.90") -> None:
        path = self.root / name
        path.mkdir()
        manifest = '[package]\nname = "fixture"\nversion = "0.0.0"\n'
        if rust_version is not None:
            manifest += f'rust-version = "{rust_version}"\n'
        (path / "Cargo.toml").write_text(manifest, encoding="utf-8")
        if with_lockfile:
            (path / "Cargo.lock").write_text('version = 4\n', encoding="utf-8")
        ci_dir = path / "ci"
        ci_dir.mkdir()
        entrypoint = ci_dir / "validate"
        entrypoint.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        mode = entrypoint.stat().st_mode
        entrypoint.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def write_registry(self, workloads: list[dict], fixtures: list[dict] | None = None) -> None:
        (self.root / "workloads.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "workloads": workloads,
                    "fixtures": fixtures or [],
                    "reserved_top_level_cargo_projects": [],
                }
            ),
            encoding="utf-8",
        )

    def entry(self, name: str, msrv: dict | None = None) -> dict:
        return {"name": name, "path": name, "msrv": msrv or DECLARED_MSRV}


class ResolveWorkloadTests(RegistryFixtureTestCase):
    def test_resolves_registered_real_workload(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")])
        entry = sc.resolve_workload(self.root, "alpha")
        self.assertEqual(entry["name"], "alpha")
        self.assertEqual(entry["path"], "alpha")

    def test_rejects_fixture_name_with_specific_diagnostic(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("beta", with_lockfile=False, rust_version=None)
        self.write_registry([self.entry("alpha")], fixtures=[self.entry("beta", msrv=NO_MSRV)])
        with self.assertRaisesRegex(sc.SupplyChainError, "registered as a fixture, not a real workload"):
            sc.resolve_workload(self.root, "beta")

    def test_rejects_unknown_name(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")])
        with self.assertRaisesRegex(sc.SupplyChainError, "not a registered real workload"):
            sc.resolve_workload(self.root, "does-not-exist")

    def test_rejects_invalid_registry(self) -> None:
        (self.root / "workloads.json").write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(sc.SupplyChainError, "registry validation failed"):
            sc.resolve_workload(self.root, "anything")


class VerifyLockedGraphTests(RegistryFixtureTestCase):
    def test_accepts_present_manifest_and_lockfile(self) -> None:
        self.cargo_project("alpha")
        entry = self.entry("alpha")
        manifest = sc.verify_locked_graph(self.root, entry)
        self.assertEqual(manifest, self.root / "alpha" / "Cargo.toml")

    def test_rejects_missing_lockfile(self) -> None:
        self.cargo_project("alpha", with_lockfile=False)
        entry = self.entry("alpha")
        with self.assertRaisesRegex(sc.SupplyChainError, "missing Cargo.lock"):
            sc.verify_locked_graph(self.root, entry)

    def test_rejects_missing_manifest(self) -> None:
        (self.root / "alpha").mkdir()
        entry = self.entry("alpha")
        with self.assertRaisesRegex(sc.SupplyChainError, "missing Cargo.toml"):
            sc.verify_locked_graph(self.root, entry)


class BuildCargoDenyCommandTests(unittest.TestCase):
    def test_preserves_required_semantics(self) -> None:
        command = sc.build_cargo_deny_command("cargo-deny", Path("/repo/deny.toml"), Path("/repo/alpha/Cargo.toml"))
        self.assertEqual(
            command,
            [
                "cargo-deny",
                "--config",
                "/repo/deny.toml",
                "--manifest-path",
                "/repo/alpha/Cargo.toml",
                "--locked",
                "--all-features",
                "check",
                "advisories",
                "licenses",
                "bans",
                "sources",
            ],
        )


class RunSupplyChainScanTests(RegistryFixtureTestCase):
    """Exercises run_supply_chain_scan end-to-end against a fake cargo-deny
    stand-in, proving exit-code propagation without depending on network
    access or a real cargo-deny install for this fast unit-test path."""

    def install_fake_cargo_deny(self, exit_code: int) -> Path:
        fake = self.root / "fake-cargo-deny"
        fake.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n", encoding="utf-8")
        mode = fake.stat().st_mode
        fake.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return fake

    def test_propagates_success_exit_code(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")])
        (self.root / "deny.toml").write_text("", encoding="utf-8")
        fake_bin = self.install_fake_cargo_deny(0)
        exit_code = sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake_bin))
        self.assertEqual(exit_code, 0)

    def test_propagates_failure_exit_code(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")])
        (self.root / "deny.toml").write_text("", encoding="utf-8")
        fake_bin = self.install_fake_cargo_deny(1)
        exit_code = sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake_bin))
        self.assertEqual(exit_code, 1)

    def test_missing_deny_config_fails_closed(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")])
        fake_bin = self.install_fake_cargo_deny(0)
        with self.assertRaisesRegex(sc.SupplyChainError, "missing canonical supply-chain policy"):
            sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake_bin))

    def test_fixture_name_is_rejected_before_any_scan_runs(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("beta", with_lockfile=False, rust_version=None)
        self.write_registry([self.entry("alpha")], fixtures=[self.entry("beta", msrv=NO_MSRV)])
        (self.root / "deny.toml").write_text("", encoding="utf-8")
        fake_bin = self.install_fake_cargo_deny(0)
        with self.assertRaisesRegex(sc.SupplyChainError, "registered as a fixture"):
            sc.run_supply_chain_scan(self.root, "beta", cargo_deny_bin=str(fake_bin))


@unittest.skipUnless(shutil.which("cargo-deny"), "cargo-deny is not installed in this environment")
class RealCargoDenyAgainstRealRepositoryTests(unittest.TestCase):
    """The one test in this file that is deliberately NOT hermetic: proves
    the actual repository-owned validator, against the actual root
    deny.toml, actually passes for the actual currently-registered real
    workload(s) -- not merely that the script's internal wiring is correct.
    """

    def test_every_registered_real_workload_passes_production_policy(self) -> None:
        import json as _json

        registry = _json.loads((REPO_ROOT / "workloads.json").read_text(encoding="utf-8"))
        for entry in registry["workloads"]:
            with self.subTest(workload=entry["name"]):
                exit_code = sc.run_supply_chain_scan(REPO_ROOT, entry["name"])
                self.assertEqual(exit_code, 0, f"{entry['name']} failed the production supply-chain policy")


if __name__ == "__main__":
    unittest.main()
