#!/usr/bin/env python3

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-oxidebatch-provenance.py")
SPEC = importlib.util.spec_from_file_location("oxidebatch_provenance", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)

CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
CHECKSUM = "a" * 64


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workload = self.root / "workload"
        self.workload.mkdir()
        (self.root / "workloads.json").write_text(
            json.dumps({"workloads": [{"name": "workload", "path": "workload"}]}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def set_provenance_required(self, required: bool, reason: str = "fixture") -> None:
        entry = {"name": "workload", "path": "workload", "provenance": {"required": required}}
        if not required:
            entry["provenance"]["reason"] = reason
        (self.root / "workloads.json").write_text(
            json.dumps({"workloads": [entry]}),
            encoding="utf-8",
        )

    def write_manifest(self, body: str) -> None:
        (self.workload / "Cargo.toml").write_text(body, encoding="utf-8")

    def write_lock(self, packages: list[dict[str, str]]) -> None:
        lines = ["version = 4", ""]
        for package in packages:
            lines.extend(
                [
                    "[[package]]",
                    f'name = "{package["name"]}"',
                    f'version = "{package["version"]}"',
                    f'source = "{package.get("source", CRATES_IO)}"',
                    f'checksum = "{package.get("checksum", CHECKSUM)}"',
                    "",
                ]
            )
        (self.workload / "Cargo.lock").write_text("\n".join(lines), encoding="utf-8")

    def valid_lock(self, extra: list[dict[str, str]] | None = None) -> None:
        packages = [
            {"name": "oxide-batch", "version": "0.6.0"},
            {"name": "oxide-batch-test", "version": "0.6.0"},
        ]
        packages.extend(extra or [])
        self.write_lock(packages)

    def assert_rejected(self, expected: str) -> None:
        with self.assertRaisesRegex(validator.ProvenanceError, expected):
            validator.validate_repository(self.root)

    def test_accepts_exact_published_subjects_and_alias(self) -> None:
        self.write_manifest(
            '[dependencies]\noxide-batch = "=0.6.0"\n'
            '[dev-dependencies]\nob-test = { package = "oxide-batch-test", version = "=0.6.0" }\n'
        )
        self.valid_lock()
        result = validator.validate_repository(self.root)
        self.assertEqual(result["workload"], {"oxide-batch": "0.6.0", "oxide-batch-test": "0.6.0"})

    def test_rejects_missing_subject_when_provenance_required_by_default(self) -> None:
        self.write_manifest('[dependencies]\nserde = "1"\n')
        self.write_lock([])
        self.assert_rejected("declares no first-party OxideBatch validation subject")

    def test_allows_missing_subject_when_provenance_not_required(self) -> None:
        self.set_provenance_required(False)
        self.write_manifest('[dependencies]\nserde = "1"\n')
        self.write_lock([{"name": "serde", "version": "1.0.0"}])
        result = validator.validate_repository(self.root)
        self.assertEqual(result["workload"], {})

    def test_still_enforces_exact_provenance_when_not_required_but_subject_present(self) -> None:
        self.set_provenance_required(False)
        self.write_manifest('[dependencies]\noxide-batch = "0.6"\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        self.assert_rejected("must use an exact")

    def test_ignores_non_first_party_workspace_dependency(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = "=0.6.0"\nserde = { workspace = true }\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        validator.validate_repository(self.root)

    def test_rejects_relaxed_version(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = "0.6"\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        self.assert_rejected("must use an exact")

    def test_rejects_path_dependency(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = { version = "=0.6.0", path = "../oxide-batch" }\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        self.assert_rejected("forbidden provenance field")

    def test_rejects_git_dependency(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = { version = "=0.6.0", git = "https://example.test/repo" }\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        self.assert_rejected("forbidden provenance field")

    def test_rejects_workspace_inheritance(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = { workspace = true }\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        self.assert_rejected("forbidden provenance field")

    def test_rejects_patch_override(self) -> None:
        self.write_manifest(
            '[dependencies]\noxide-batch = "=0.6.0"\n'
            '[patch.crates-io]\noxide-batch = { path = "../oxide-batch" }\n'
        )
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        self.assert_rejected("must not be overridden")

    def test_rejects_legacy_replace_override(self) -> None:
        self.write_manifest(
            '[dependencies]\noxide-batch = "=0.6.0"\n'
            '[replace]\n"oxide-batch:0.6.0" = { path = "../oxide-batch" }\n'
        )
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        self.assert_rejected("must not be overridden")

    def test_rejects_target_specific_alias_bypass(self) -> None:
        self.write_manifest(
            '[dependencies]\noxide-batch = "=0.6.0"\n'
            '[target.\'cfg(unix)\'.dev-dependencies]\nob-test = { package = "oxide-batch-test", version = "0.6" }\n'
        )
        self.valid_lock()
        self.assert_rejected("must use an exact")

    def test_rejects_non_crates_io_lock_source(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = "=0.6.0"\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0", "source": "git+https://example.test/oxide-batch"}])
        self.assert_rejected("does not resolve from canonical crates.io")

    def test_rejects_missing_checksum(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = "=0.6.0"\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0", "checksum": "bad"}])
        self.assert_rejected("missing a valid crates.io checksum")

    def test_rejects_workload_source_replacement_config(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = "=0.6.0"\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        cargo = self.workload / ".cargo"
        cargo.mkdir()
        (cargo / "config.toml").write_text(
            '[source.crates-io]\nreplace-with = "mirror"\n[source.mirror]\nregistry = "https://example.test/index"\n',
            encoding="utf-8",
        )
        self.assert_rejected("source replacement is forbidden")

    def test_rejects_repository_root_source_replacement_config(self) -> None:
        self.write_manifest('[dependencies]\noxide-batch = "=0.6.0"\n')
        self.write_lock([{"name": "oxide-batch", "version": "0.6.0"}])
        cargo = self.root / ".cargo"
        cargo.mkdir()
        (cargo / "config.toml").write_text(
            '[source.crates-io]\nreplace-with = "mirror"\n[source.mirror]\nregistry = "https://example.test/index"\n',
            encoding="utf-8",
        )
        self.assert_rejected("source replacement is forbidden")


if __name__ == "__main__":
    unittest.main()
