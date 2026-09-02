#!/usr/bin/env python3

import copy
import hashlib
import importlib.util
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-evidence.py")
SPEC = importlib.util.spec_from_file_location("evidence_validator", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class EvidenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workload_dir = self.root / "alpha"
        (self.workload_dir / "ci").mkdir(parents=True)
        (self.workload_dir / "src").mkdir()
        (self.workload_dir / "migrations").mkdir()
        (self.workload_dir / "validation").mkdir()

        entrypoint = self.workload_dir / "ci" / "validate"
        entrypoint.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        entrypoint.chmod(entrypoint.stat().st_mode | stat.S_IXUSR)

        (self.workload_dir / "Cargo.toml").write_text(
            '[package]\nname = "alpha"\nversion = "0.1.0"\nrust-version = "1.90"\n'
            '[dependencies]\noxide-batch = "=0.6.0"\n',
            encoding="utf-8",
        )
        (self.workload_dir / "Cargo.lock").write_text(
            'version = 4\n\n[[package]]\nname = "oxide-batch"\nversion = "0.6.0"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            f'checksum = "{"a" * 64}"\n',
            encoding="utf-8",
        )
        (self.workload_dir / "docker-compose.yml").write_text(
            "services:\n  db:\n    image: postgres:18\n", encoding="utf-8"
        )
        (self.workload_dir / "migrations" / "001.sql").write_text("select 1;\n", encoding="utf-8")
        (self.workload_dir / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        (self.workload_dir / "src" / "verify.rs").write_text("// producer verifier\n", encoding="utf-8")
        generation = self.workload_dir / "validation" / "generate-evidence.sh"
        generation.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        generation.chmod(generation.stat().st_mode | stat.S_IXUSR)
        self.artifact_path = self.workload_dir / "validation" / "a-run.json"
        self.artifact_path.write_text('{"passed": true}\n', encoding="utf-8")

        (self.root / "workloads.json").write_text(
            json.dumps({
                "schema_version": 3,
                "workloads": [{"name": "alpha", "path": "alpha", "msrv": {"declared": True}}],
                "fixtures": [],
                "reserved_top_level_cargo_projects": [],
            }),
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Evidence Test"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "evidence@example.test"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "producer snapshot"], cwd=self.root, check=True)
        self.producer_revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
        ).strip()

        self.verifier_path = self.workload_dir / "validation" / "verify.py"
        self.write_verifier([])
        self.workload = {"name": "alpha", "path": "alpha"}

        includes = [
            "Cargo.toml", "Cargo.lock", "docker-compose.yml", "migrations", "src",
            "validation/generate-evidence.sh",
        ]
        tree = validator.workload_tree(self.root, self.producer_revision, "alpha")
        digest = validator.closure_digest(validator.select_closure(tree, includes))
        artifact_raw = self.artifact_path.read_bytes()

        self.manifest = {
            "schema_version": 1,
            "workload": "alpha",
            "producer": {
                "base_revision": self.producer_revision,
                "revision_role": "producer-checkout",
                "run": {"kind": "local-test", "identity": None, "binding": "recorded-metadata"},
            },
            "semantic_closure": {
                "algorithm": "sha256-git-tree-entries-v1",
                "includes": includes,
                "excluded_generated_paths": ["validation/a-run.json"],
                "digest_sha256": digest,
            },
            "validation_subject": {
                "lockfile": {
                    "path": "Cargo.lock",
                    "git_blob_oid": validator.git_blob_oid(self.root, self.producer_revision, "alpha/Cargo.lock"),
                },
                "crates": [{
                    "name": "oxide-batch",
                    "version": "0.6.0",
                    "source": "registry+https://github.com/rust-lang/crates.io-index",
                    "checksum": "a" * 64,
                }],
            },
            "records": [{
                "scenario": "sample",
                "artifact": {
                    "path": "validation/a-run.json",
                    "sha256": hashlib.sha256(artifact_raw).hexdigest(),
                    "size_bytes": len(artifact_raw),
                },
                "input": {
                    "identity": {"kind": "generated-file", "sha256": "b" * 64, "size_bytes": 10},
                    "reproduction": {"seed": 7},
                },
                "parameters": {},
                "failure_point": None,
            }],
            "verifier": {
                "canonical": {
                    "path": "validation/verify.py",
                    "sha256": self.verifier_sha(),
                    "result_model": "violations-v1",
                },
                "producer": {
                    "path": "src/verify.rs",
                    "git_blob_oid": validator.git_blob_oid(self.root, self.producer_revision, "alpha/src/verify.rs"),
                },
            },
            "environment": {
                "observations": [{
                    "name": "database-image",
                    "value": "postgres:18",
                    "trust": "deterministically-recomputed",
                    "source": "producer docker-compose.yml",
                }],
                "limitations": ["fixture omits a real execution environment"],
            },
            "retention": {
                "committed_artifacts": {
                    "directory": "validation",
                    "max_count": 1,
                    "max_total_bytes": 1024,
                    "supersession": "single current artifact",
                },
                "wall_clock_freshness_merge_gate": False,
            },
            "external_artifacts": [],
        }
        self.manifest_path = self.workload_dir / "validation" / "evidence-manifest.json"
        self.write_manifest()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_verifier(self, violations: list[str]) -> None:
        self.verifier_path.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"violations = {violations!r}\n"
            'print(json.dumps({"schema_version": 1, "violations": violations}))\n'
            "raise SystemExit(0 if not violations else 1)\n",
            encoding="utf-8",
        )

    def verifier_sha(self) -> str:
        return hashlib.sha256(self.verifier_path.read_bytes()).hexdigest()

    def write_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")

    def assert_rejected(self, expected: str) -> None:
        self.write_manifest()
        with self.assertRaisesRegex(validator.EvidenceError, expected):
            validator.validate_manifest(self.root, self.workload, self.manifest_path)

    def test_accepts_machine_checkable_manifest(self) -> None:
        validator.validate_manifest(self.root, self.workload, self.manifest_path)

    def test_rejects_missing_required_provenance(self) -> None:
        del self.manifest["validation_subject"]
        self.assert_rejected("evidence manifest must contain exactly")

    def test_rejects_semantic_closure_mismatch(self) -> None:
        self.manifest["semantic_closure"]["digest_sha256"] = "0" * 64
        self.assert_rejected("semantic closure mismatch")

    def test_rejects_deterministic_retention_violation(self) -> None:
        self.manifest["retention"]["committed_artifacts"]["max_total_bytes"] = 1
        self.assert_rejected("deterministic retention violation")

    def test_generated_evidence_is_outside_deterministic_semantic_closure(self) -> None:
        closure = self.manifest["semantic_closure"]
        tree = validator.workload_tree(self.root, self.producer_revision, "alpha")
        entries = validator.select_closure(tree, closure["includes"])
        before = validator.closure_digest(entries)
        selected_paths = {path for path, _mode, _oid in entries}
        self.assertNotIn("validation/a-run.json", selected_paths)
        self.artifact_path.write_text('{"passed": false, "changed": true}\n', encoding="utf-8")
        after = validator.closure_digest(
            validator.select_closure(
                validator.workload_tree(self.root, self.producer_revision, "alpha"),
                closure["includes"],
            )
        )
        self.assertEqual(before, after)

    def test_producer_passed_true_cannot_override_canonical_violation(self) -> None:
        self.write_verifier(["forced canonical violation"])
        self.manifest["verifier"]["canonical"]["sha256"] = self.verifier_sha()
        self.assert_rejected("canonical verifier reported violation")


if __name__ == "__main__":
    unittest.main()
