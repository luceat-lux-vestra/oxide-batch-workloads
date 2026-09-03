#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-evidence.py")
SPEC = importlib.util.spec_from_file_location("evidence_validator", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)

REPO_ROOT = Path(__file__).resolve().parents[2]
CSV_VERIFIER_PATH = REPO_ROOT / "csv-postgres" / "validation" / "verify-retained-evidence.py"
CSV_SPEC = importlib.util.spec_from_file_location("csv_postgres_retained_evidence_verifier", CSV_VERIFIER_PATH)
assert CSV_SPEC and CSV_SPEC.loader
csv_verifier = importlib.util.module_from_spec(CSV_SPEC)
CSV_SPEC.loader.exec_module(csv_verifier)


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
                    "trust": "recorded-metadata",
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

    def write_verifier(
        self,
        violations: list[str],
        *,
        display_verdict: str | None = None,
        exit_code: int | None = None,
        raw_stdout: str | None = None,
    ) -> None:
        if raw_stdout is not None:
            body = f"print({raw_stdout!r})\nraise SystemExit({0 if exit_code is None else exit_code})\n"
        else:
            result = {"schema_version": 1, "violations": violations}
            if display_verdict is not None:
                result["display_verdict"] = display_verdict
            code = (0 if not violations else 1) if exit_code is None else exit_code
            body = f"import json\nprint(json.dumps({result!r}))\nraise SystemExit({code})\n"
        self.verifier_path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")

    def verifier_sha(self) -> str:
        return hashlib.sha256(self.verifier_path.read_bytes()).hexdigest()

    def refresh_verifier_sha(self) -> None:
        self.manifest["verifier"]["canonical"]["sha256"] = self.verifier_sha()

    def write_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")

    def assert_rejected(self, expected: str) -> None:
        self.write_manifest()
        with self.assertRaisesRegex(validator.EvidenceError, expected):
            validator.validate_manifest(self.root, self.workload, self.manifest_path)

    def commit_path(self, relative_path: str, message: str) -> str:
        subprocess.run(["git", "add", relative_path], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", message], cwd=self.root, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()

    def recompute_closure(self, revision: str) -> str:
        closure = self.manifest["semantic_closure"]
        tree = validator.workload_tree(self.root, revision, "alpha")
        return validator.closure_digest(validator.select_closure(tree, closure["includes"]))

    def test_accepts_machine_checkable_manifest(self) -> None:
        validator.validate_manifest(self.root, self.workload, self.manifest_path)

    def test_rejects_missing_required_provenance(self) -> None:
        del self.manifest["validation_subject"]
        self.assert_rejected("evidence manifest must contain exactly")

    def test_rejects_semantic_closure_digest_mismatch(self) -> None:
        self.manifest["semantic_closure"]["digest_sha256"] = "0" * 64
        self.assert_rejected("semantic closure mismatch")

    def test_rejects_stale_semantic_closure_after_source_revision_changes(self) -> None:
        (self.workload_dir / "src" / "main.rs").write_text("fn main() { println!(\"changed\"); }\n", encoding="utf-8")
        changed_revision = self.commit_path("alpha/src/main.rs", "mutate semantic source")
        self.manifest["producer"]["base_revision"] = changed_revision
        self.assert_rejected("semantic closure mismatch")

    def test_rejects_stale_published_subject(self) -> None:
        self.manifest["validation_subject"]["crates"][0]["version"] = "0.5.0"
        self.assert_rejected("validation_subject.crates do not match")

    def test_rejects_stale_lockfile_identity(self) -> None:
        self.manifest["validation_subject"]["lockfile"]["git_blob_oid"] = "0" * 40
        self.assert_rejected("validation subject lockfile identity mismatch")

    def test_rejects_evidence_artifact_digest_mutation(self) -> None:
        self.artifact_path.write_text('{"passed": false, "tampered": true}\n', encoding="utf-8")
        self.assert_rejected("retained evidence artifact digest mismatch")

    def test_rejects_deterministic_retention_violation(self) -> None:
        self.manifest["retention"]["committed_artifacts"]["max_total_bytes"] = 1
        self.assert_rejected("deterministic retention violation")

    def test_rejects_undeclared_retained_json(self) -> None:
        (self.workload_dir / "validation" / "extra.json").write_text("{}\n", encoding="utf-8")
        self.assert_rejected("deterministic retention/layout violation")

    def test_rejects_input_identity_without_digest_or_reference(self) -> None:
        self.manifest["records"][0]["input"]["identity"] = {"kind": "generated-file"}
        self.assert_rejected("must provide sha256 and/or reference")

    def test_rejects_canonical_verifier_identity_mismatch(self) -> None:
        self.manifest["verifier"]["canonical"]["sha256"] = "0" * 64
        self.assert_rejected("canonical verifier identity mismatch")

    def test_rejects_producer_verifier_identity_mismatch(self) -> None:
        self.manifest["verifier"]["producer"]["git_blob_oid"] = "0" * 40
        self.assert_rejected("producer verifier identity mismatch")

    def test_rejects_unimplemented_trusted_producer_binding(self) -> None:
        self.manifest["producer"]["run"]["binding"] = "trusted-producer-bound"
        self.assert_rejected("no external attestation verifier")

    def test_rejects_external_artifact_without_real_retention_guarantee(self) -> None:
        self.manifest["external_artifacts"] = [{
            "name": "raw-output",
            "sha256": "c" * 64,
            "reference": "artifact://example/raw-output",
            "storage": "example-artifact-store",
            "retention_guarantee": "",
        }]
        self.assert_rejected("retention_guarantee must be a non-empty string")

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
        self.refresh_verifier_sha()
        self.assert_rejected("canonical verifier reported violation")

    def test_canonical_violations_fail_even_when_verifier_exits_zero(self) -> None:
        self.write_verifier(["forced canonical violation"], exit_code=0)
        self.refresh_verifier_sha()
        self.assert_rejected("canonical verifier reported violation")

    def test_nonzero_verifier_exit_cannot_hide_empty_violation_set(self) -> None:
        self.write_verifier([], exit_code=7)
        self.refresh_verifier_sha()
        self.assert_rejected("canonical verifier returned nonzero with no violations")

    def test_rejects_malformed_canonical_verifier_result(self) -> None:
        self.write_verifier([], raw_stdout="not-json")
        self.refresh_verifier_sha()
        self.assert_rejected("canonical verifier returned malformed JSON")

    def test_non_authoritative_display_string_cannot_flip_machine_success(self) -> None:
        self.write_verifier([], display_verdict="fail", exit_code=0)
        self.refresh_verifier_sha()
        self.write_manifest()
        validator.validate_manifest(self.root, self.workload, self.manifest_path)

    def test_recorded_environment_value_is_not_treated_as_authoritative(self) -> None:
        self.manifest["environment"]["observations"][0]["value"] = "postgres:999"
        self.write_manifest()
        validator.validate_manifest(self.root, self.workload, self.manifest_path)

    def test_locally_coordinated_rewrite_is_outside_authenticity_guarantee(self) -> None:
        (self.workload_dir / "src" / "main.rs").write_text("fn main() { println!(\"rewritten\"); }\n", encoding="utf-8")
        rewritten_revision = self.commit_path("alpha/src/main.rs", "coordinated local rewrite")
        self.manifest["producer"]["base_revision"] = rewritten_revision
        self.manifest["semantic_closure"]["digest_sha256"] = self.recompute_closure(rewritten_revision)
        self.write_manifest()
        # This is intentionally accepted: v1 proves internal consistency, not
        # authenticity against an editor able to rewrite source and metadata.
        validator.validate_manifest(self.root, self.workload, self.manifest_path)


class CsvPostgresEvidenceMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workload_dir = Path(self.tempdir.name) / "csv-postgres"
        self.validation_dir = self.workload_dir / "validation"
        shutil.copytree(REPO_ROOT / "csv-postgres" / "validation", self.validation_dir)
        self.manifest_path = self.validation_dir / "evidence-manifest.json"
        self.verifier_script = self.validation_dir / "verify-retained-evidence.py"
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")

    def record(self, scenario: str) -> dict:
        return next(record for record in self.manifest["records"] if record["scenario"] == scenario)

    def artifact_path(self, scenario: str) -> Path:
        return self.workload_dir / self.record(scenario)["artifact"]["path"]

    def load_artifact(self, scenario: str) -> dict:
        return json.loads(self.artifact_path(scenario).read_text(encoding="utf-8"))

    def write_artifact(self, scenario: str, value: dict) -> None:
        self.artifact_path(scenario).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def assert_violation(self, expected: str) -> list[str]:
        self.write_manifest()
        violations = csv_verifier.verify(self.manifest_path)
        self.assertTrue(any(expected in item for item in violations), violations)
        return violations

    def run_cli(self) -> tuple[subprocess.CompletedProcess[str], dict]:
        completed = subprocess.run(
            [sys.executable, str(self.verifier_script), "--manifest", str(self.manifest_path)],
            cwd=self.workload_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        result = json.loads(completed.stdout)
        return completed, result

    def test_current_retained_evidence_passes_canonical_verifier(self) -> None:
        self.assertEqual(csv_verifier.verify(self.manifest_path), [])
        completed, result = self.run_cli()
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["display_verdict"], "pass")

    def test_rejects_malformed_truncated_evidence(self) -> None:
        self.artifact_path("clean_run").write_text('{"scenario":', encoding="utf-8")
        self.write_manifest()
        violations = csv_verifier.verify(self.manifest_path)
        self.assertTrue(any("cannot read" in item for item in violations), violations)
        completed, result = self.run_cli()
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["violations"])
        self.assertEqual(result["display_verdict"], "fail")

    def test_rejects_artifact_scenario_mismatch(self) -> None:
        clean = self.load_artifact("clean_run")
        clean["scenario"] = "wrong_scenario"
        self.write_artifact("clean_run", clean)
        self.assert_violation("clean_run artifact scenario mismatch")

    def test_rejects_dataset_seed_mismatch(self) -> None:
        self.record("clean_run")["input"]["reproduction"]["seed"] += 1
        self.assert_violation("dataset.seed does not match manifest input identity")

    def test_rejects_cross_scenario_input_identity_mismatch(self) -> None:
        self.record("crash_run")["input"]["identity"]["sha256"] = "0" * 64
        self.assert_violation("all three scenarios must bind the same exact input identity")

    def test_rejects_failure_point_mismatch(self) -> None:
        self.record("crash_run")["failure_point"]["fail_at"] = "chunk:51"
        self.assert_violation("failure_injection.fail_at does not match manifest")

    def test_rejects_restart_lineage_mismatch(self) -> None:
        self.record("restart_run")["parameters"]["import_name"] = "different_import"
        self.assert_violation("restart_run import_name must resume the crash_run instance")

    def test_producer_success_boolean_cannot_hide_final_state_failure(self) -> None:
        restart = self.load_artifact("restart_run")
        restart["recovered_run_full_content_digest_sha256"] = "0" * 64
        restart["full_content_digests_match"] = True
        self.write_artifact("restart_run", restart)
        self.write_manifest()
        violations = csv_verifier.verify(self.manifest_path)
        self.assertTrue(any("full-content digests must match exactly" in item for item in violations), violations)
        completed, result = self.run_cli()
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["violations"])
        self.assertEqual(result["display_verdict"], "fail")

    def test_producer_boolean_cannot_force_failure_when_canonical_relations_pass(self) -> None:
        restart = self.load_artifact("restart_run")
        restart["full_content_digests_match"] = False
        self.write_artifact("restart_run", restart)
        self.write_manifest()
        self.assertEqual(csv_verifier.verify(self.manifest_path), [])
        completed, result = self.run_cli()
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["display_verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
