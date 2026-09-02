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

DECLARED_MSRV = {"declared": True, "version": "1.95"}
NO_MSRV = {"declared": False, "policy_reason": "fixture: no MSRV policy"}
PROVENANCE_REQUIRED = {"required": True}
PROVENANCE_EXEMPT = {"required": False, "reason": "fixture: not an OxideBatch consumer"}


class WorkloadRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def cargo_project(self, name: str, with_entrypoint: bool = True, executable: bool = True) -> None:
        path = self.root / name
        path.mkdir()
        (path / "Cargo.toml").write_text("[package]\nname = \"fixture\"\nversion = \"0.0.0\"\n", encoding="utf-8")
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
        reserved: list[dict[str, str]] | None = None,
        schema_version: int = 2,
    ) -> None:
        (self.root / "workloads.json").write_text(
            json.dumps(
                {
                    "schema_version": schema_version,
                    "workloads": workloads,
                    "reserved_top_level_cargo_projects": reserved or [],
                }
            ),
            encoding="utf-8",
        )

    def entry(self, name: str, msrv: dict | None = None, provenance: dict | None = None) -> dict:
        return {
            "name": name,
            "path": name,
            "msrv": msrv or DECLARED_MSRV,
            "provenance": provenance or PROVENANCE_REQUIRED,
        }

    def assert_rejected(self, expected: str) -> None:
        with self.assertRaisesRegex(validator.RegistryError, expected):
            validator.validate_repository(self.root)

    def test_accepts_registered_workload(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")])
        self.assertEqual(
            validator.validate_repository(self.root),
            [{"name": "alpha", "path": "alpha", "msrv": DECLARED_MSRV, "provenance": PROVENANCE_REQUIRED}],
        )

    def test_accepts_workload_with_no_msrv_and_exempt_provenance(self) -> None:
        self.cargo_project("beta")
        self.write_registry([self.entry("beta", msrv=NO_MSRV, provenance=PROVENANCE_EXEMPT)])
        result = validator.validate_repository(self.root)
        self.assertEqual(result[0]["msrv"], NO_MSRV)
        self.assertEqual(result[0]["provenance"], PROVENANCE_EXEMPT)

    def test_rejects_wrong_schema_version(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha")], schema_version=1)
        self.assert_rejected("schema_version must be 2")

    def test_rejects_unregistered_candidate(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("beta")
        self.write_registry([self.entry("alpha")])
        self.assert_rejected("unregistered top-level Cargo project")

    def test_rejects_missing_registered_workload(self) -> None:
        self.write_registry([self.entry("missing")])
        self.assert_rejected("registered workload path does not exist")

    def test_rejects_duplicate_name(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("beta")
        self.write_registry(
            [
                {**self.entry("same"), "path": "alpha"},
                {**self.entry("same"), "path": "beta"},
            ]
        )
        self.assert_rejected("duplicate workload name")

    def test_rejects_duplicate_path(self) -> None:
        self.cargo_project("alpha")
        self.write_registry(
            [
                {**self.entry("alpha"), "path": "alpha"},
                {**self.entry("other"), "path": "alpha"},
            ]
        )
        self.assert_rejected("duplicate workload path")

    def test_rejects_zero_workloads(self) -> None:
        self.write_registry([])
        self.assert_rejected("workloads must be a non-empty array")

    def test_rejects_missing_contract_entrypoint(self) -> None:
        self.cargo_project("alpha", with_entrypoint=False)
        self.write_registry([self.entry("alpha")])
        self.assert_rejected("missing its CI contract entrypoint")

    def test_rejects_non_executable_contract_entrypoint(self) -> None:
        self.cargo_project("alpha", executable=False)
        self.write_registry([self.entry("alpha")])
        self.assert_rejected("CI contract entrypoint is not executable")

    def test_rejects_malformed_msrv_missing_version(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha", msrv={"declared": True})])
        self.assert_rejected("declared msrv must contain exactly")

    def test_rejects_declared_msrv_with_empty_version(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha", msrv={"declared": True, "version": "  "})])
        self.assert_rejected("msrv.version must be a non-empty string")

    def test_rejects_undeclared_msrv_without_policy_reason(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha", msrv={"declared": False})])
        self.assert_rejected("undeclared msrv must contain exactly")

    def test_rejects_undeclared_msrv_with_empty_policy_reason(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha", msrv={"declared": False, "policy_reason": ""})])
        self.assert_rejected("policy_reason must be a non-empty string")

    def test_rejects_provenance_exempt_without_reason(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha", provenance={"required": False})])
        self.assert_rejected("provenance-exempt entry must contain exactly")

    def test_rejects_provenance_required_with_extra_keys(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.entry("alpha", provenance={"required": True, "reason": "unexpected"})])
        self.assert_rejected("provenance-required entry must contain exactly")

    def test_rejects_entry_missing_required_key(self) -> None:
        self.cargo_project("alpha")
        entry = self.entry("alpha")
        del entry["provenance"]
        self.write_registry([entry])
        self.assert_rejected("each workload entry must contain exactly")

    def test_allows_explicit_reserved_cargo_project_with_reason(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("repo-tool", with_entrypoint=False)
        self.write_registry(
            [self.entry("alpha")],
            [{"path": "repo-tool", "reason": "repository-owned tooling, not a validation workload"}],
        )
        result = validator.validate_repository(self.root)
        self.assertEqual([entry["name"] for entry in result], ["alpha"])

    def test_rejects_reserved_project_without_reason(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("repo-tool", with_entrypoint=False)
        self.write_registry(
            [self.entry("alpha")],
            [{"path": "repo-tool", "reason": ""}],
        )
        self.assert_rejected("must include a non-empty reason")

    def test_rejects_stale_reserved_project(self) -> None:
        self.cargo_project("alpha")
        self.write_registry(
            [self.entry("alpha")],
            [{"path": "missing-tool", "reason": "repository-owned tooling"}],
        )
        self.assert_rejected("reserved Cargo project must exist")


if __name__ == "__main__":
    unittest.main()
