#!/usr/bin/env python3

import importlib.util
import pathlib
import subprocess
import unittest


def load(name, filename):
    path = pathlib.Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


RUNNER = load("scheduled_supply_chain_runner", "run-scheduled-supply-chain-audit.py")
REPORTER = load("scheduled_supply_chain_reporter", "report-supply-chain-audit.py")


class AuditRunnerTests(unittest.TestCase):
    def run_with(self, responses):
        queue = list(responses)
        original = RUNNER.run_command
        RUNNER.run_command = lambda command: queue.pop(0)
        try:
            return RUNNER.run_live_audit()
        finally:
            RUNNER.run_command = original

    def completed(self, rc, output):
        return subprocess.CompletedProcess([], rc, stdout=output)

    def test_clean_reuses_canonical_discovery_and_validator(self):
        audit = self.run_with([
            self.completed(0, '{"include":[{"name":"a"},{"name":"b"}]}'),
            self.completed(0, "advisories ok, bans ok, licenses ok, sources ok"),
            self.completed(0, "advisories ok, bans ok, licenses ok, sources ok"),
        ])
        self.assertEqual(audit["classification"], "clean")
        self.assertEqual(audit["workloads"], ["a", "b"])

    def test_confirmed_policy_failure_is_distinct_with_ansi_output(self):
        audit = self.run_with([
            self.completed(0, '{"include":[{"name":"a"}]}'),
            self.completed(4, "advisories \x1b[32mok\x1b[0m, bans \x1b[32mok\x1b[0m, licenses \x1b[31mFAILED\x1b[0m, sources \x1b[32mok\x1b[0m"),
        ])
        self.assertEqual(audit["classification"], "policy-finding")
        self.assertIn('"licenses"', audit["details"])
        self.assertNotIn("\x1b", audit["details"])

    def test_non_policy_failure_is_infrastructure(self):
        audit = self.run_with([
            self.completed(0, '{"include":[{"name":"a"}]}'),
            self.completed(1, "failed to fetch advisory database: timeout"),
        ])
        self.assertEqual(audit["classification"], "infrastructure-failure")

    def test_discovery_failure_is_infrastructure(self):
        audit = self.run_with([self.completed(1, "registry unavailable")])
        self.assertEqual(audit["classification"], "infrastructure-failure")

    def test_exit_codes_preserve_classification(self):
        self.assertEqual(RUNNER.exit_code_for("clean"), 0)
        self.assertEqual(RUNNER.exit_code_for("policy-finding"), 1)
        self.assertEqual(RUNNER.exit_code_for("infrastructure-failure"), 2)

    def test_reporting_labels_exist_in_canonical_taxonomy(self):
        REPORTER.validate_reporting_labels()


class FakeClient:
    def __init__(self):
        self.issues = []
        self.comments = []
        self.next_number = 1000

    def all_issues(self):
        return iter(self.issues)

    def create_issue(self, body):
        issue = {"number": self.next_number, "state": "open", "body": body, "title": REPORTER.TITLE}
        self.next_number += 1
        self.issues.append(issue)
        return issue

    def update_issue(self, number, **payload):
        issue = next(x for x in self.issues if x["number"] == number)
        issue.update(payload)
        return issue

    def comment(self, number, body):
        self.comments.append((number, body))
        return {"id": len(self.comments)}


class AuditIssueLifecycleTests(unittest.TestCase):
    def audit(self, classification):
        return {"classification": classification, "workloads": ["csv-postgres"], "details": classification}

    def test_finding_create_update_reopen_and_clean_close_are_owned_and_idempotent(self):
        client = FakeClient()
        first = REPORTER.reconcile_issue(client, self.audit("policy-finding"), "run-1")
        self.assertIn("created issue", first)
        self.assertEqual(len(client.issues), 1)
        self.assertIn(REPORTER.MARKER, client.issues[0]["body"])

        REPORTER.reconcile_issue(client, self.audit("policy-finding"), "run-2")
        self.assertEqual(len(client.issues), 1)
        self.assertEqual(client.issues[0]["state"], "open")

        client.issues[0]["state"] = "closed"
        REPORTER.reconcile_issue(client, self.audit("infrastructure-failure"), "run-3")
        self.assertEqual(len(client.issues), 1)
        self.assertEqual(client.issues[0]["state"], "open")
        self.assertIn("infrastructure-failure", client.issues[0]["body"])

        REPORTER.reconcile_issue(client, self.audit("clean"), "run-4")
        self.assertEqual(client.issues[0]["state"], "closed")
        self.assertTrue(any("recovered to clean" in body for _, body in client.comments))

    def test_multiple_owned_issues_fail_closed(self):
        client = FakeClient()
        client.issues = [
            {"number": 1, "state": "open", "body": REPORTER.MARKER},
            {"number": 2, "state": "closed", "body": REPORTER.MARKER},
        ]
        with self.assertRaisesRegex(RuntimeError, "multiple owned"):
            REPORTER.find_owned_issue(client)


if __name__ == "__main__":
    unittest.main()
