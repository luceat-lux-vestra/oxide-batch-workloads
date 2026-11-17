#!/usr/bin/env python3

import importlib.util
import contextlib
import io
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

    def add_maven_manifest(self, name: str) -> Path:
        path = self.root / name / "benchmark" / "java"
        path.mkdir(parents=True, exist_ok=True)
        pom = path / "pom.xml"
        pom.write_text("<project/>\n", encoding="utf-8")
        return pom

    def add_supply_chain_hook(self, name: str, exit_code: int) -> Path:
        hook = self.root / name / "ci" / "validate-supply-chain"
        hook.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n", encoding="utf-8")
        mode = hook.stat().st_mode
        hook.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return hook

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

    def write_advisory_exceptions(self, exceptions: list[dict]) -> None:
        path = self.root / sc.ADVISORY_EXCEPTIONS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema_version": 1, "exceptions": exceptions}),
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


class WorkloadScopedAdvisoryExceptionTests(RegistryFixtureTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cargo_project("alpha")
        self.cargo_project("beta")
        self.write_registry([self.entry("alpha"), self.entry("beta")])
        (self.root / "alpha" / "Cargo.lock").write_text(
            'version = 4\n\n[[package]]\nname = "chrono"\nversion = "0.4.45"\n',
            encoding="utf-8",
        )

    @staticmethod
    def exception(workload: str = "alpha") -> dict:
        return {
            "workload": workload,
            "advisory_id": "RUSTSEC-2026-0543",
            "package": "chrono",
            "classification": "informational/unmaintained",
            "owner": "#297",
            "reason": "bounded fixture reason",
            "remove_when": "fixture no longer resolves chrono",
        }

    def test_exception_is_scoped_to_declared_workload_only(self) -> None:
        self.write_advisory_exceptions([self.exception()])
        grouped = sc.load_advisory_exceptions(self.root)

        self.assertEqual(
            [item["advisory_id"] for item in grouped["alpha"]],
            ["RUSTSEC-2026-0543"],
        )
        self.assertNotIn("beta", grouped)

    def test_rejects_unknown_workload(self) -> None:
        self.write_advisory_exceptions([self.exception("unknown")])
        with self.assertRaisesRegex(sc.SupplyChainError, "unknown real workload"):
            sc.load_advisory_exceptions(self.root)

    def test_rejects_package_not_present_in_workload_lock(self) -> None:
        self.write_advisory_exceptions([self.exception("beta")])
        with self.assertRaisesRegex(sc.SupplyChainError, "is not resolved by workload 'beta'"):
            sc.load_advisory_exceptions(self.root)

    def test_rejects_non_informational_exception_classification(self) -> None:
        item = self.exception()
        item["classification"] = "vulnerability"
        self.write_advisory_exceptions([item])
        with self.assertRaisesRegex(sc.SupplyChainError, "classification must be one of"):
            sc.load_advisory_exceptions(self.root)

    def test_rejects_duplicate_workload_advisory_pair(self) -> None:
        item = self.exception()
        self.write_advisory_exceptions([item, dict(item)])
        with self.assertRaisesRegex(sc.SupplyChainError, "duplicate advisory exception"):
            sc.load_advisory_exceptions(self.root)

    def test_effective_policy_injects_only_selected_advisory(self) -> None:
        canonical = self.root / "deny.toml"
        canonical.write_text(
            '[advisories]\n'
            'unmaintained = "all"\n'
            'unused-ignored-advisory = "deny"\n'
            'ignore = []\n',
            encoding="utf-8",
        )
        output = self.root / "effective.toml"
        effective = sc.materialize_effective_deny_config(
            canonical,
            [self.exception()],
            output,
        )

        self.assertEqual(effective, output)
        self.assertEqual(
            sc.tomllib.loads(output.read_text(encoding="utf-8"))["advisories"]["ignore"],
            ["RUSTSEC-2026-0543"],
        )
        self.assertEqual(
            sc.tomllib.loads(canonical.read_text(encoding="utf-8"))["advisories"]["ignore"],
            [],
        )

    def test_effective_policy_rejects_global_base_ignore(self) -> None:
        canonical = self.root / "deny.toml"
        canonical.write_text(
            '[advisories]\n'
            'unused-ignored-advisory = "deny"\n'
            'ignore = ["RUSTSEC-2026-0543"]\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(sc.SupplyChainError, "must remain exception-free"):
            sc.materialize_effective_deny_config(
                canonical,
                [self.exception()],
                self.root / "effective.toml",
            )


    def test_run_scan_applies_exception_only_to_affected_workload(self) -> None:
        canonical = self.root / "deny.toml"
        canonical.write_text(
            '[advisories]\n'
            'unmaintained = "all"\n'
            'unused-ignored-advisory = "deny"\n'
            'ignore = []\n',
            encoding="utf-8",
        )
        self.write_advisory_exceptions([self.exception()])

        capture = self.root / "captured-deny.toml"
        fake = self.root / "capture-cargo-deny"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"--config\" ]; then\n"
            f"    cp \"$2\" \"{capture}\"\n"
            "    exit 0\n"
            "  fi\n"
            "  shift\n"
            "done\n"
            "exit 9\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        self.assertEqual(
            sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake)),
            0,
        )
        affected = capture.read_text(encoding="utf-8")
        self.assertIn('ignore = ["RUSTSEC-2026-0543"]', affected)

        self.assertEqual(
            sc.run_supply_chain_scan(self.root, "beta", cargo_deny_bin=str(fake)),
            0,
        )
        unaffected = capture.read_text(encoding="utf-8")
        self.assertIn("ignore = []", unaffected)
        self.assertNotIn("RUSTSEC-2026-0543", unaffected)


class AdditionalEcosystemTests(RegistryFixtureTestCase):
    def test_no_nested_maven_requires_no_hook(self) -> None:
        self.cargo_project("alpha")
        self.assertIsNone(sc.resolve_additional_hook(self.root / "alpha"))

    def test_nested_maven_without_hook_fails_closed(self) -> None:
        self.cargo_project("alpha")
        self.add_maven_manifest("alpha")
        with self.assertRaisesRegex(sc.SupplyChainError, "nested Maven manifests require"):
            sc.resolve_additional_hook(self.root / "alpha")

    def test_target_named_maven_manifest_still_requires_hook(self) -> None:
        self.cargo_project("alpha")
        target = self.root / "alpha" / "target"
        target.mkdir()
        (target / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        with self.assertRaisesRegex(sc.SupplyChainError, "nested Maven manifests require"):
            sc.resolve_additional_hook(self.root / "alpha")

    def test_non_executable_hook_fails_closed(self) -> None:
        self.cargo_project("alpha")
        self.add_maven_manifest("alpha")
        hook = self.root / "alpha" / "ci" / "validate-supply-chain"
        hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        with self.assertRaisesRegex(sc.SupplyChainError, "not executable"):
            sc.resolve_additional_hook(self.root / "alpha")

    def test_symlink_hook_fails_closed(self) -> None:
        self.cargo_project("alpha")
        self.add_maven_manifest("alpha")
        target = self.root / "real-hook"
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        mode = target.stat().st_mode
        target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        hook = self.root / "alpha" / "ci" / "validate-supply-chain"
        hook.symlink_to(target)
        with self.assertRaisesRegex(sc.SupplyChainError, "must not be a symlink"):
            sc.resolve_additional_hook(self.root / "alpha")


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
    """Exercises orchestration against fake cargo-deny and hook stand-ins."""

    def install_fake_cargo_deny(self, exit_code: int) -> Path:
        fake = self.root / "fake-cargo-deny"
        fake.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n", encoding="utf-8")
        mode = fake.stat().st_mode
        fake.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return fake

    def ready(self) -> Path:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")])
        (self.root / "deny.toml").write_text("", encoding="utf-8")
        return self.install_fake_cargo_deny(0)

    def test_propagates_success_exit_code(self) -> None:
        fake_bin = self.ready()
        exit_code = sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake_bin))
        self.assertEqual(exit_code, 0)

    def test_propagates_cargo_failure_exit_code(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")])
        (self.root / "deny.toml").write_text("", encoding="utf-8")
        fake_bin = self.install_fake_cargo_deny(1)
        exit_code = sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake_bin))
        self.assertEqual(exit_code, 1)

    def test_propagates_additional_hook_failure(self) -> None:
        fake_bin = self.ready()
        self.add_maven_manifest("alpha")
        self.add_supply_chain_hook("alpha", 7)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake_bin))
        self.assertEqual(exit_code, 7)
        self.assertIn("additional-ecosystem FAILED", output.getvalue())

    def test_runs_successful_additional_hook(self) -> None:
        fake_bin = self.ready()
        self.add_maven_manifest("alpha")
        self.add_supply_chain_hook("alpha", 0)
        exit_code = sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake_bin))
        self.assertEqual(exit_code, 0)

    def test_nested_maven_without_hook_fails_before_scan(self) -> None:
        fake_bin = self.ready()
        self.add_maven_manifest("alpha")
        with self.assertRaisesRegex(sc.SupplyChainError, "nested Maven manifests require"):
            sc.run_supply_chain_scan(self.root, "alpha", cargo_deny_bin=str(fake_bin))

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
    def test_every_registered_real_workload_passes_production_policy(self) -> None:
        registry = json.loads((REPO_ROOT / "workloads.json").read_text(encoding="utf-8"))
        for entry in registry["workloads"]:
            with self.subTest(workload=entry["name"]):
                exit_code = sc.run_supply_chain_scan(REPO_ROOT, entry["name"])
                self.assertEqual(exit_code, 0, f"{entry['name']} failed the production supply-chain policy")


if __name__ == "__main__":
    unittest.main()
