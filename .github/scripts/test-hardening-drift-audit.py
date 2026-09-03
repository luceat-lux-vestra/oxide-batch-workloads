#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest


def load(name, filename):
    path = pathlib.Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


RUNNER = load("hardening_drift_runner", "run-hardening-drift-audit.py")
REPORTER = load("hardening_drift_reporter", "report-hardening-drift-audit.py")
WORKFLOW_SECURITY = load("workflow_security_validator", "validate-workflow-security.py")
ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakeReadClient:
    def __init__(self, repository, ruleset, labels, special=None, open_items=None, pr_files=None):
        self.repository = repository
        self.ruleset = ruleset
        self.labels = labels
        self.special = special or {}
        self.open_items = open_items or []
        self.pr_files = pr_files or {}

    def get(self, path):
        if path == "":
            return self.repository
        if path.startswith("/rulesets/"):
            return self.ruleset
        if path in self.special:
            return self.special[path]
        raise RUNNER.ApiFailure(f"unexpected fake API path: {path}")

    def get_all(self, path):
        if path == "/labels":
            return self.labels
        if path == "/issues?state=open":
            return self.open_items
        if path.startswith("/pulls/") and path.endswith("/files"):
            number = int(path.split("/")[2])
            return [{"filename": value} for value in self.pr_files.get(number, [])]
        raise RUNNER.ApiFailure(f"unexpected fake paged API path: {path}")

    def pr_paths(self, number):
        return list(self.pr_files.get(number, []))


def canonical_repository():
    return {
        "visibility": "public",
        "default_branch": "main",
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "delete_branch_on_merge": True,
        "allow_update_branch": True,
        "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
        "squash_merge_commit_message": "COMMIT_MESSAGES",
        "web_commit_signoff_required": False,
    }


def canonical_ruleset():
    return {
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_linear_history"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "required_review_thread_resolution": True,
                    "require_extra_approval_for_unattributed_changes": False,
                    "allowed_merge_methods": ["squash"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "dependency-review"},
                        {"context": "supply-chain"},
                        {"context": "workloads-ci"},
                        {"context": "workloads-msrv"},
                    ],
                },
            },
        ],
    }


def canonical_labels():
    taxonomy = json.loads((ROOT / ".github" / "labels.json").read_text(encoding="utf-8"))
    return [dict(entry) for entry in taxonomy["labels"]]


class AuditClassifierTests(unittest.TestCase):
    def test_representative_negative_controls_reach_audit_level_policy_drift(self):
        controls = [
            "workload-registry",
            "ruleset.required_status_contexts",
            "workflow-security",
            "oxidebatch-provenance",
            "supply-chain",
            "managed-labels",
            "label-automation",
            "evidence-contract",
            "repository.default_branch",
        ]
        for control in controls:
            with self.subTest(control=control):
                self.assertEqual(RUNNER.classify([RUNNER.finding(control, "safe negative fixture")], []), "policy-drift")

    def canonical_run_fixture(self, failing_script=None, supply_classification="clean"):
        def fake_run(argv):
            script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
            if script == "run-scheduled-supply-chain-audit.py":
                output = pathlib.Path(argv[argv.index("--output") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "classification": supply_classification,
                            "workloads": ["fixture"],
                            "details": f"safe {supply_classification} fixture",
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0 if supply_classification == "clean" else 1, stdout="supply fixture")
            if script == failing_script:
                return subprocess.CompletedProcess(argv, 1, stdout=f"safe negative fixture from {script}")
            return subprocess.CompletedProcess(argv, 0, stdout="ok")

        return fake_run

    def test_canonical_validator_failures_are_promoted_to_hardening_findings(self):
        cases = {
            "validate-workload-registry.py": "workload-registry",
            "validate-oxidebatch-provenance.py": "oxidebatch-provenance",
            "validate-evidence.py": "evidence-contract",
            "validate-label-taxonomy.py": "label-taxonomy",
            "validate-workflow-security.py": "workflow-security",
        }
        for script, control in cases.items():
            with self.subTest(script=script):
                findings, infrastructure = RUNNER.run_canonical_checks(self.canonical_run_fixture(failing_script=script))
                self.assertFalse(infrastructure)
                self.assertTrue(any(item["control"] == control for item in findings))
                self.assertEqual(RUNNER.classify(findings, infrastructure), "policy-drift")

    def test_supply_chain_policy_finding_is_promoted_to_hardening_drift(self):
        findings, infrastructure = RUNNER.run_canonical_checks(
            self.canonical_run_fixture(supply_classification="policy-finding")
        )
        self.assertFalse(infrastructure)
        self.assertTrue(any(item["control"] == "supply-chain" for item in findings))
        self.assertEqual(RUNNER.classify(findings, infrastructure), "policy-drift")

    def test_supply_chain_infrastructure_failure_remains_distinct(self):
        findings, infrastructure = RUNNER.run_canonical_checks(
            self.canonical_run_fixture(supply_classification="infrastructure-failure")
        )
        self.assertFalse(findings)
        self.assertTrue(any(item["control"] == "supply-chain" for item in infrastructure))
        self.assertEqual(RUNNER.classify(findings, infrastructure), "infrastructure-failure")

    def test_infrastructure_failure_has_precedence_over_confirmed_drift(self):
        self.assertEqual(
            RUNNER.classify([RUNNER.finding("supply-chain", "policy")], [RUNNER.finding("live-readback", "timeout")]),
            "infrastructure-failure",
        )

    def test_live_required_context_drift_is_detected_from_canonical_policy(self):
        policy = json.loads(RUNNER.POLICY_PATH.read_text(encoding="utf-8"))
        ruleset = canonical_ruleset()
        required = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
        required["parameters"]["required_status_checks"] = [{"context": "workloads-ci"}]
        client = FakeReadClient(
            canonical_repository(),
            ruleset,
            canonical_labels(),
            {
                "/dependency-graph/sbom": {"sbom": {}},
                "/private-vulnerability-reporting": {"enabled": True},
            },
        )
        findings, manual = RUNNER.run_live_policy_checks(client, policy)
        ids = {item["control"] for item in findings}
        self.assertIn("ruleset.required_status_contexts", ids)
        self.assertTrue(manual)

    def test_live_repository_setting_drift_is_detected(self):
        policy = json.loads(RUNNER.POLICY_PATH.read_text(encoding="utf-8"))
        repository = canonical_repository()
        repository["default_branch"] = "develop"
        client = FakeReadClient(
            repository,
            canonical_ruleset(),
            canonical_labels(),
            {
                "/dependency-graph/sbom": {"sbom": {}},
                "/private-vulnerability-reporting": {"enabled": True},
            },
        )
        findings, _manual = RUNNER.run_live_policy_checks(client, policy)
        self.assertTrue(any(item["control"] == "repository.default_branch" for item in findings))

    def test_missing_automated_repository_field_is_infrastructure_failure_not_policy_drift(self):
        repository = canonical_repository()
        del repository["default_branch"]
        client = FakeReadClient(
            repository,
            canonical_ruleset(),
            canonical_labels(),
            {
                "/dependency-graph/sbom": {"sbom": {}},
                "/private-vulnerability-reporting": {"enabled": True},
            },
        )
        audit = RUNNER.run_audit(client, self.canonical_run_fixture())
        self.assertEqual(audit["classification"], "infrastructure-failure")
        self.assertFalse(any(item["control"] == "repository.default_branch" for item in audit["policy_findings"]))
        self.assertTrue(
            any("omitted field 'default_branch'" in item["details"] for item in audit["infrastructure_failures"])
        )

    def test_live_managed_label_metadata_drift_is_detected(self):
        labels = canonical_labels()
        labels[0]["color"] = "ffffff"
        client = FakeReadClient({}, {}, labels)
        findings = RUNNER.run_live_label_checks(client)
        self.assertTrue(any(item["control"] == "managed-labels" for item in findings))

    def test_live_label_automation_backlog_drift_is_detected_read_only(self):
        item = {
            "number": 999,
            "title": "governance: fixture missing canonical labels",
            "labels": [],
        }
        client = FakeReadClient({}, {}, canonical_labels(), open_items=[item])
        findings = RUNNER.run_live_label_automation_checks(client)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["control"], "label-automation")
        self.assertIn("type:task", findings[0]["details"])
        self.assertIn("area:governance", findings[0]["details"])

    def test_manual_readback_controls_are_explicit_in_synthetic_result(self):
        audit = RUNNER.synthetic("clean")
        self.assertEqual(audit["classification"], "clean")
        self.assertTrue(audit["manual_readback"])
        self.assertTrue(all("id" in entry and "expected" in entry for entry in audit["manual_readback"]))
        manual_ids = {entry["id"] for entry in audit["manual_readback"]}
        self.assertTrue(
            {
                "repository.allow_squash_merge",
                "repository.allow_merge_commit",
                "repository.allow_rebase_merge",
                "repository.delete_branch_on_merge",
                "repository.allow_update_branch",
                "repository.squash_merge_commit_title",
                "repository.squash_merge_commit_message",
            }
            <= manual_ids
        )


class WorkflowSecurityFixtureTests(unittest.TestCase):
    def copy_workflows(self):
        temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(temp.name)
        shutil.copytree(ROOT / ".github" / "workflows", root / ".github" / "workflows")
        return temp, root

    def test_mutable_action_ref_fixture_is_rejected(self):
        temp, root = self.copy_workflows()
        try:
            path = root / ".github" / "workflows" / "fixture.yml"
            path.write_text("name: fixture\njobs:\n  test:\n    steps:\n      - uses: actions/checkout@v7\n", encoding="utf-8")
            with self.assertRaisesRegex(WORKFLOW_SECURITY.WorkflowSecurityError, "not a full SHA"):
                WORKFLOW_SECURITY.validate_action_pins(root)
        finally:
            temp.cleanup()

    def test_write_capable_pull_request_target_head_checkout_fixture_is_rejected(self):
        temp, root = self.copy_workflows()
        try:
            path = root / ".github" / "workflows" / "label-automation.yml"
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                "ref: ${{ github.event.repository.default_branch }}",
                "ref: ${{ github.event.pull_request.head.sha }}",
            )
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(WORKFLOW_SECURITY.WorkflowSecurityError, "trusted pull_request_target boundary|must not check out"):
                WORKFLOW_SECURITY.validate_label_automation_boundary(root)
        finally:
            temp.cleanup()


class FakeIssueClient:
    def __init__(self):
        self.issues = []
        self.comments = []
        self.next_number = 2000

    def all_issues(self):
        return iter(self.issues)

    def create_issue(self, title, body, labels):
        issue = {"number": self.next_number, "state": "open", "title": title, "body": body, "labels": labels}
        self.next_number += 1
        self.issues.append(issue)
        return issue

    def update_issue(self, number, **payload):
        issue = next(item for item in self.issues if item["number"] == number)
        issue.update(payload)
        return issue

    def comment(self, number, body):
        self.comments.append((number, body))
        return {"id": len(self.comments)}


class HardeningIssueLifecycleTests(unittest.TestCase):
    def audit(self, classification):
        return {
            "classification": classification,
            "policy_findings": [{"control": "fixture", "details": classification}] if classification == "policy-drift" else [],
            "infrastructure_failures": [{"control": "fixture", "details": classification}] if classification == "infrastructure-failure" else [],
            "manual_readback": [{"id": "manual.fixture", "classification": "required", "expected": True}],
        }

    def test_create_update_reopen_and_recovery_close(self):
        client = FakeIssueClient()
        REPORTER.reconcile_issue(client, self.audit("policy-drift"), "run-1")
        self.assertEqual(len(client.issues), 1)
        REPORTER.reconcile_issue(client, self.audit("infrastructure-failure"), "run-2")
        self.assertEqual(len(client.issues), 1)
        client.issues[0]["state"] = "closed"
        REPORTER.reconcile_issue(client, self.audit("policy-drift"), "run-3")
        self.assertEqual(client.issues[0]["state"], "open")
        REPORTER.reconcile_issue(client, self.audit("clean"), "run-4")
        self.assertEqual(client.issues[0]["state"], "closed")
        self.assertTrue(any("recovered to clean" in body for _, body in client.comments))

    def test_duplicate_owned_issue_markers_fail_closed(self):
        client = FakeIssueClient()
        client.issues = [
            {"number": 1, "state": "open", "body": REPORTER.MARKER},
            {"number": 2, "state": "closed", "body": REPORTER.MARKER},
        ]
        with self.assertRaisesRegex(RuntimeError, "multiple owned"):
            REPORTER.reconcile_issue(client, self.audit("policy-drift"), "run")


if __name__ == "__main__":
    unittest.main()
