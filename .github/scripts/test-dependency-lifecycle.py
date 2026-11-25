#!/usr/bin/env python3
"""Positive and negative controls for the structural lifecycle authority."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import dependency_lifecycle as lifecycle

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "security", pathlib.Path(__file__).with_name("validate-workflow-security.py")
)
security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security)


class LifecycleTests(unittest.TestCase):
    def test_live_tree(self):
        lifecycle.validate(ROOT, security.native_updaters(ROOT), security.load_structured_yaml, security.fail)

    def fixture(self, root):
        policy = root / ".github/dependency-update-policy.json"
        workflow = root / ".github/workflows/ci.yml"
        manifest = root / "work/Cargo.toml"
        for path in (policy, workflow, manifest):
            path.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('[package]\nname="fixture"\nversion="0.0.1"\n', encoding="utf-8")
        label = "ubuntu-42.04-arm"  # synthetic future label, no provider availability claim
        workflow.write_text("jobs:\n  test:\n    runs-on: " + label + "\n    steps: []\n", encoding="utf-8")
        common = {"owner": "fixture", "authority": "fixture", "mechanism": "fixture", "validation": "fixture"}
        entries = [
            dict(common, id="work", kind="dependency_root", strategy="DEPENDABOT",
                 source_paths=["work/Cargo.toml"], provenance={"ecosystem": "cargo", "directory": "/work"}),
            dict(common, id="runner", kind="runner", strategy="REVIEWED_FREEZE",
                 source_paths=[".github/workflows/ci.yml"], provenance={"label": label},
                 revisit={"review_by": "2999-01-01", "rationale": "fixture", "trigger": "fixture", "evidence": "fixture"}),
        ]
        def write():
            policy.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        write()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        return entries, write, workflow

    def test_new_primary_and_companion_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            entries, write, workflow = self.fixture(root)
            owners = {("cargo", "/work"): {}}
            def validate():
                lifecycle.validate(root, owners, security.load_structured_yaml, security.fail)
            validate()
            (root / "work/Cargo.lock").write_text("version = 4\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "work/Cargo.lock"], check=True)
            validate()
            with self.assertRaisesRegex(security.WorkflowSecurityError, "native owner absent"):
                lifecycle.validate(root, {}, security.load_structured_yaml, security.fail)
            entries[1]["revisit"]["review_by"] = "2000-01-01"
            write()
            with self.assertRaisesRegex(security.WorkflowSecurityError, "expired reviewed constraint"):
                validate()
            entries[1]["revisit"]["review_by"] = "2999-01-01"
            write()
            workflow.write_text("jobs: {test: {runs-on: ubuntu-latest, steps: []}}\n", encoding="utf-8")
            with self.assertRaisesRegex(security.WorkflowSecurityError, "unowned or non-native runner"):
                validate()
            workflow.write_text("jobs: {test: {runs-on: ubuntu-42.04-arm, steps: []}}\n", encoding="utf-8")
            (root / "other").mkdir()
            (root / "other/Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "other/Cargo.toml"], check=True)
            with self.assertRaisesRegex(security.WorkflowSecurityError, "missing/stale declaration owners"):
                validate()

    def test_managed_installation_failure_cannot_report_clean(self):
        for script, outcomes in (
            ("run-hardening-drift-audit.py", "success,failure,success"),
            ("run-scheduled-supply-chain-audit.py", "failure"),
        ):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "audit.json"
                run = subprocess.run(
                    [sys.executable, str(ROOT / ".github/scripts" / script),
                     "--output", str(path), "--tool-install-status", outcomes],
                    text=True, capture_output=True,
                )
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertEqual(json.loads(path.read_text())["classification"], "infrastructure-failure")


if __name__ == "__main__":
    unittest.main()
