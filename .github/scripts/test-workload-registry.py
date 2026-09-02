#!/usr/bin/env python3

import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-workload-registry.py")
SPEC = importlib.util.spec_from_file_location("workload_registry_validator", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)

DECLARED_MSRV = {"declared": True}
NO_MSRV = {"declared": False, "policy_reason": "fixture: no MSRV policy"}


class WorkloadRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def cargo_project(
        self,
        name: str,
        rust_version: str | None = "1.90",
        with_entrypoint: bool = True,
        executable: bool = True,
    ) -> None:
        path = self.root / name
        path.mkdir()
        manifest = '[package]\nname = "fixture"\nversion = "0.0.0"\n'
        if rust_version is not None:
            manifest += f'rust-version = "{rust_version}"\n'
        (path / "Cargo.toml").write_text(manifest, encoding="utf-8")
        if with_entrypoint:
            ci_dir = path / "ci"
            ci_dir.mkdir()
            entrypoint = ci_dir / "validate"
            entrypoint.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            mode = entrypoint.stat().st_mode
            if executable:
                entrypoint.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            else:
                entrypoint.chmod(mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    def write_registry(
        self,
        workloads: list[dict],
        fixtures: list[dict] | None = None,
        reserved: list[dict[str, str]] | None = None,
        schema_version: int = 3,
    ) -> None:
        (self.root / "workloads.json").write_text(
            json.dumps(
                {
                    "schema_version": schema_version,
                    "workloads": workloads,
                    "fixtures": fixtures or [],
                    "reserved_top_level_cargo_projects": reserved or [],
                }
            ),
            encoding="utf-8",
        )

    def entry(self, name: str, msrv: dict | None = None) -> dict:
        return {"name": name, "path": name, "msrv": msrv or DECLARED_MSRV}

    def assert_rejected(self, expected: str) -> None:
        with self.assertRaisesRegex(validator.RegistryError, expected):
            validator.validate_repository(self.root)

    def test_accepts_registered_workload_and_resolves_msrv_from_cargo_toml(self) -> None:
        self.cargo_project("alpha", rust_version="1.90")
        self.write_registry([self.entry("alpha")])
        result = validator.validate_repository(self.root)
        self.assertEqual(
            result["workloads"],
            [{"kind": "workload", "name": "alpha", "path": "alpha", "msrv": {"declared": True, "version": "1.90", "policy_reason": None}}],
        )
        self.assertEqual(result["fixtures"], [])

    def test_accepts_fixture_with_no_msrv_and_no_rust_version(self) -> None:
        self.cargo_project("alpha", rust_version="1.90")
        self.cargo_project("beta", rust_version=None)
        self.write_registry([self.entry("alpha")], fixtures=[self.entry("beta", msrv=NO_MSRV)])
        result = validator.validate_repository(self.root)
        self.assertEqual(result["fixtures"][0]["msrv"], {"declared": False, "version": None, "policy_reason": NO_MSRV["policy_reason"]})

    def test_rejects_wrong_schema_version(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")], schema_version=2)
        self.assert_rejected("schema_version must be 3")

    def test_rejects_unregistered_candidate(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("beta")
        self.write_registry([self.entry("alpha")])
        self.assert_rejected("unregistered top-level Cargo project")

    def test_rejects_missing_registered_workload(self) -> None:
        self.write_registry([self.entry("missing")])
        self.assert_rejected("registered workload path does not exist")

    def test_rejects_duplicate_name_across_workloads_and_fixtures(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("beta")
        self.write_registry(
            [{**self.entry("same"), "path": "alpha"}],
            fixtures=[{**self.entry("same"), "path": "beta"}],
        )
        self.assert_rejected("duplicate name across workloads/fixtures")

    def test_rejects_duplicate_path_across_workloads_and_fixtures(self) -> None:
        self.cargo_project("alpha")
        self.write_registry(
            [{**self.entry("alpha"), "path": "alpha"}],
            fixtures=[{**self.entry("other"), "path": "alpha"}],
        )
        self.assert_rejected("duplicate path across workloads/fixtures")

    def test_rejects_zero_workloads(self) -> None:
        self.write_registry([])
        self.assert_rejected("workloads must be a non-empty array")

    def test_accepts_zero_fixtures(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")], fixtures=[])
        result = validator.validate_repository(self.root)
        self.assertEqual(result["fixtures"], [])

    def test_rejects_missing_contract_entrypoint(self) -> None:
        self.cargo_project("alpha", with_entrypoint=False)
        self.write_registry([self.entry("alpha")])
        self.assert_rejected("missing its CI contract entrypoint")

    def test_rejects_non_executable_contract_entrypoint(self) -> None:
        self.cargo_project("alpha", executable=False)
        self.write_registry([self.entry("alpha")])
        self.assert_rejected("CI contract entrypoint is not executable")

    def test_rejects_declared_msrv_with_version_field_in_registry(self) -> None:
        # The version must come only from Cargo.toml; duplicating it in the
        # registry (even if consistent today) is exactly the drift the
        # single-source-of-truth design forbids.
        self.cargo_project("alpha", rust_version="1.90")
        self.write_registry([self.entry("alpha", msrv={"declared": True, "version": "1.90"})])
        self.assert_rejected("declared msrv must contain exactly declared")

    def test_rejects_declared_msrv_when_cargo_toml_has_no_rust_version(self) -> None:
        self.cargo_project("alpha", rust_version=None)
        self.write_registry([self.entry("alpha")])
        self.assert_rejected("declares msrv.declared=true but Cargo.toml has no package.rust-version")

    def test_rejects_undeclared_msrv_when_cargo_toml_has_rust_version(self) -> None:
        # Prevents the exact drift a strict reviewer flagged: a package that
        # actually declares an MSRV must not be registered as policy-exempt.
        self.cargo_project("alpha", rust_version="1.90")
        self.write_registry([self.entry("alpha", msrv=NO_MSRV)])
        self.assert_rejected("msrv.declared is false but Cargo.toml declares package.rust-version")

    def test_rejects_undeclared_msrv_without_policy_reason(self) -> None:
        self.cargo_project("alpha", rust_version=None)
        self.write_registry([self.entry("alpha", msrv={"declared": False})])
        self.assert_rejected("undeclared msrv must contain exactly")

    def test_rejects_undeclared_msrv_with_empty_policy_reason(self) -> None:
        self.cargo_project("alpha", rust_version=None)
        self.write_registry([self.entry("alpha", msrv={"declared": False, "policy_reason": ""})])
        self.assert_rejected("policy_reason must be a non-empty string")

    def test_rejects_entry_missing_required_key(self) -> None:
        self.cargo_project("alpha")
        entry = self.entry("alpha")
        del entry["msrv"]
        self.write_registry([entry])
        self.assert_rejected("each workload entry must contain exactly")

    def test_rejects_manifest_missing_package_table(self) -> None:
        self.cargo_project("alpha")
        (self.root / "alpha" / "Cargo.toml").write_text("[dependencies]\n", encoding="utf-8")
        self.write_registry([self.entry("alpha")])
        self.assert_rejected(r"Cargo\.toml is missing a \[package\] table")

    def test_allows_explicit_reserved_cargo_project_with_reason(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("repo-tool", with_entrypoint=False)
        self.write_registry(
            [self.entry("alpha")],
            reserved=[{"path": "repo-tool", "reason": "repository-owned tooling, not a validation workload"}],
        )
        result = validator.validate_repository(self.root)
        self.assertEqual([entry["name"] for entry in result["workloads"]], ["alpha"])

    def test_rejects_reserved_project_without_reason(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("repo-tool", with_entrypoint=False)
        self.write_registry(
            [self.entry("alpha")],
            reserved=[{"path": "repo-tool", "reason": ""}],
        )
        self.assert_rejected("must include a non-empty reason")

    def test_rejects_stale_reserved_project(self) -> None:
        self.cargo_project("alpha")
        self.write_registry(
            [self.entry("alpha")],
            reserved=[{"path": "missing-tool", "reason": "repository-owned tooling"}],
        )
        self.assert_rejected("reserved Cargo project must exist")


if __name__ == "__main__":
    unittest.main()
