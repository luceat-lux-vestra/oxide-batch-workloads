#!/usr/bin/env python3

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-workload-registry.py")
SPEC = importlib.util.spec_from_file_location("workload_registry_validator", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class WorkloadRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def cargo_project(self, name: str) -> None:
        path = self.root / name
        path.mkdir()
        (path / "Cargo.toml").write_text("[package]\nname = \"fixture\"\nversion = \"0.0.0\"\n", encoding="utf-8")
        ci_dir = path / "ci"
        ci_dir.mkdir()
        (ci_dir / "validate.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")
        (ci_dir / "msrv.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")

    def workload_entry(self, name: str, path: str | None = None) -> dict[str, object]:
        return {
            "name": name,
            "path": path or name,
            "contracts": {
                "ci": {"run": "ci/validate.sh"},
                "msrv": {"policy": "required", "toolchain": "1.95", "run": "ci/msrv.sh"},
            },
        }

    def write_registry(self, workloads: list[dict[str, object]], reserved: list[dict[str, str]] | None = None) -> None:
        (self.root / "workloads.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workloads": workloads,
                    "reserved_top_level_cargo_projects": reserved or [],
                }
            ),
            encoding="utf-8",
        )

    def assert_rejected(self, expected: str) -> None:
        with self.assertRaisesRegex(validator.RegistryError, expected):
            validator.validate_repository(self.root)

    def test_accepts_registered_workload(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([self.workload_entry("alpha")])
        self.assertEqual(validator.validate_repository(self.root), ["alpha"])

    def test_rejects_unregistered_candidate(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("beta")
        self.write_registry([self.workload_entry("alpha")])
        self.assert_rejected("unregistered top-level Cargo project")

    def test_rejects_missing_registered_workload(self) -> None:
        self.write_registry([self.workload_entry("missing")])
        self.assert_rejected("registered workload path does not exist")

    def test_rejects_duplicate_name(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("beta")
        self.write_registry(
            [
                self.workload_entry("same", path="alpha"),
                self.workload_entry("same", path="beta"),
            ]
        )
        self.assert_rejected("duplicate workload name")

    def test_rejects_duplicate_path(self) -> None:
        self.cargo_project("alpha")
        self.write_registry(
            [
                self.workload_entry("alpha", path="alpha"),
                self.workload_entry("other", path="alpha"),
            ]
        )
        self.assert_rejected("duplicate workload path")

    def test_rejects_zero_workloads(self) -> None:
        self.write_registry([])
        self.assert_rejected("workloads must be a non-empty array")

    def test_allows_explicit_reserved_cargo_project_with_reason(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("repo-tool")
        self.write_registry(
            [self.workload_entry("alpha")],
            [{"path": "repo-tool", "reason": "repository-owned tooling, not a validation workload"}],
        )
        self.assertEqual(validator.validate_repository(self.root), ["alpha"])

    def test_rejects_reserved_project_without_reason(self) -> None:
        self.cargo_project("alpha")
        self.cargo_project("repo-tool")
        self.write_registry(
            [self.workload_entry("alpha")],
            [{"path": "repo-tool", "reason": ""}],
        )
        self.assert_rejected("must include a non-empty reason")

    def test_rejects_stale_reserved_project(self) -> None:
        self.cargo_project("alpha")
        self.write_registry(
            [self.workload_entry("alpha")],
            [{"path": "missing-tool", "reason": "repository-owned tooling"}],
        )
        self.assert_rejected("reserved Cargo project must exist")

    def test_rejects_missing_contracts(self) -> None:
        self.cargo_project("alpha")
        self.write_registry([{"name": "alpha", "path": "alpha"}])
        self.assert_rejected("must define contracts.ci and contracts.msrv")

    def test_rejects_missing_ci_script(self) -> None:
        self.cargo_project("alpha")
        entry = self.workload_entry("alpha")
        entry["contracts"] = {"ci": {"run": "ci/missing.sh"}, "msrv": {"policy": "required", "toolchain": "1.95", "run": "ci/msrv.sh"}}
        self.write_registry([entry])
        self.assert_rejected("contracts.ci.run path does not exist")

    def test_rejects_msrv_required_without_run(self) -> None:
        self.cargo_project("alpha")
        entry = self.workload_entry("alpha")
        entry["contracts"] = {"ci": {"run": "ci/validate.sh"}, "msrv": {"policy": "required", "toolchain": "1.95"}}
        self.write_registry([entry])
        self.assert_rejected("must contain exactly policy, toolchain, run")

    def test_accepts_not_applicable_msrv_policy_with_reason(self) -> None:
        self.cargo_project("alpha")
        entry = self.workload_entry("alpha")
        entry["contracts"] = {
            "ci": {"run": "ci/validate.sh"},
            "msrv": {"policy": "not-applicable", "reason": "fixture workload intentionally omits rust-version"},
        }
        self.write_registry([entry])
        self.assertEqual(validator.validate_repository(self.root), ["alpha"])


if __name__ == "__main__":
    unittest.main()
