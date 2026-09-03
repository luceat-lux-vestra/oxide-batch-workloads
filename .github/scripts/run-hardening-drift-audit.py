#!/usr/bin/env python3

import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / ".github" / "repository-settings-policy.json"
LABELS_PATH = ROOT / ".github" / "labels.json"


class ApiFailure(RuntimeError):
    pass


class GitHubReadClient:
    def __init__(self, repository, token):
        self.repository = repository
        self.base = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "oxide-batch-workloads-hardening-drift-audit",
        }

    def get(self, path):
        request = urllib.request.Request(self.base + path, headers=self.headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise ApiFailure(f"GET {path} failed: {exc}") from exc

    def get_all(self, path):
        page = 1
        result = []
        while True:
            separator = "&" if "?" in path else "?"
            items = self.get(f"{path}{separator}per_page=100&page={page}")
            if not isinstance(items, list):
                raise ApiFailure(f"GET {path} returned non-list pagination payload")
            result.extend(items)
            if len(items) < 100:
                return result
            page += 1


def command(command):
    return subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def finding(control, details):
    return {"control": control, "details": details}


def run_canonical_checks(run=command):
    checks = [
        ("workload-registry", [sys.executable, ".github/scripts/validate-workload-registry.py"]),
        ("oxidebatch-provenance", [sys.executable, ".github/scripts/validate-oxidebatch-provenance.py"]),
        ("evidence-contract", [sys.executable, ".github/scripts/validate-evidence.py"]),
        ("label-taxonomy", [sys.executable, ".github/scripts/validate-label-taxonomy.py"]),
        ("workflow-security", [sys.executable, ".github/scripts/validate-workflow-security.py"]),
    ]
    findings = []
    for control, argv in checks:
        completed = run(argv)
        if completed.returncode != 0:
            findings.append(finding(control, completed.stdout.strip() or f"exit {completed.returncode}"))

    supply = run([sys.executable, ".github/scripts/run-scheduled-supply-chain-audit.py", "--output", "/tmp/hardening-supply-chain.json"])
    try:
        supply_result = json.loads(pathlib.Path("/tmp/hardening-supply-chain.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return findings, [finding("supply-chain", f"cannot read canonical supply-chain audit result: {exc}; output={supply.stdout}")]
    classification = supply_result.get("classification")
    if classification == "policy-finding":
        findings.append(finding("supply-chain", supply_result.get("details", "confirmed supply-chain policy finding")))
        return findings, []
    if classification == "infrastructure-failure":
        return findings, [finding("supply-chain", supply_result.get("details", "supply-chain infrastructure failure"))]
    if classification != "clean" or supply.returncode != 0:
        return findings, [finding("supply-chain", f"unexpected canonical supply-chain result: {supply_result!r}")]
    return findings, []


def rules_by_type(ruleset):
    return {rule.get("type"): rule for rule in ruleset.get("rules", []) if isinstance(rule, dict)}


def actual_ruleset_value(control_id, ruleset):
    rules = rules_by_type(ruleset)
    if control_id == "ruleset.enforcement":
        return ruleset.get("enforcement")
    if control_id == "ruleset.target_default_branch":
        return ruleset.get("conditions", {}).get("ref_name", {}).get("include") == ["~DEFAULT_BRANCH"]
    if control_id == "ruleset.deletion_protection":
        return "deletion" in rules
    if control_id == "ruleset.non_fast_forward_protection":
        return "non_fast_forward" in rules
    if control_id == "ruleset.required_linear_history":
        return "required_linear_history" in rules
    if control_id == "ruleset.pull_request_required":
        return "pull_request" in rules
    if control_id == "ruleset.allowed_merge_methods":
        return rules.get("pull_request", {}).get("parameters", {}).get("allowed_merge_methods")
    if control_id == "ruleset.review_thread_resolution":
        return rules.get("pull_request", {}).get("parameters", {}).get("required_review_thread_resolution")
    if control_id == "ruleset.required_approving_review_count":
        return rules.get("pull_request", {}).get("parameters", {}).get("required_approving_review_count")
    if control_id == "ruleset.require_extra_approval_for_unattributed_changes":
        return rules.get("pull_request", {}).get("parameters", {}).get("require_extra_approval_for_unattributed_changes")
    if control_id == "ruleset.strict_required_status_checks":
        return rules.get("required_status_checks", {}).get("parameters", {}).get("strict_required_status_checks_policy")
    if control_id == "ruleset.required_status_contexts":
        entries = rules.get("required_status_checks", {}).get("parameters", {}).get("required_status_checks", [])
        return sorted(entry.get("context") for entry in entries if isinstance(entry, dict))
    if control_id == "ruleset.bypass_actors":
        return ruleset.get("bypass_actors", [])
    if control_id == "security.signed_commits":
        return "required_signatures" in rules
    raise KeyError(control_id)


def actual_repository_value(control_id, repository, client):
    mapping = {
        "repository.visibility": "visibility",
        "repository.default_branch": "default_branch",
        "repository.allow_squash_merge": "allow_squash_merge",
        "repository.allow_merge_commit": "allow_merge_commit",
        "repository.allow_rebase_merge": "allow_rebase_merge",
        "repository.delete_branch_on_merge": "delete_branch_on_merge",
        "repository.allow_update_branch": "allow_update_branch",
        "repository.squash_merge_commit_title": "squash_merge_commit_title",
        "repository.squash_merge_commit_message": "squash_merge_commit_message",
        "repository.web_commit_signoff_required": "web_commit_signoff_required",
    }
    if control_id in mapping:
        return repository.get(mapping[control_id])
    if control_id == "security.dependency_graph":
        sbom = client.get("/dependency-graph/sbom")
        return isinstance(sbom, dict) and isinstance(sbom.get("sbom"), dict)
    if control_id == "security.private_vulnerability_reporting":
        value = client.get("/private-vulnerability-reporting")
        return value.get("enabled") if isinstance(value, dict) else None
    raise KeyError(control_id)


def compare_expected(control_id, expected, actual):
    if control_id == "ruleset.required_status_contexts":
        return sorted(expected) == sorted(actual or [])
    return actual == expected


def run_live_policy_checks(client, policy):
    repository = client.get("")
    ruleset = client.get(f"/rulesets/{policy['ruleset_id']}")
    findings = []
    manual = []
    for control in policy["controls"]:
        control_id = control["id"]
        readback = control["readback"]
        if readback == "manual-readback":
            manual.append({"id": control_id, "classification": control["classification"], "expected": control["expected"]})
            continue
        try:
            if readback == "repository-api":
                actual = actual_repository_value(control_id, repository, client)
            elif readback == "ruleset-api":
                actual = actual_ruleset_value(control_id, ruleset)
            else:
                raise KeyError(f"unsupported readback {readback}")
        except KeyError as exc:
            raise ApiFailure(f"canonical policy control has no implemented readback: {control_id}: {exc}") from exc
        if not compare_expected(control_id, control["expected"], actual):
            findings.append(finding(control_id, f"expected {control['expected']!r}, live readback {actual!r}"))
    return findings, manual


def run_live_label_checks(client):
    taxonomy = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    expected = {entry["name"]: entry for entry in taxonomy["labels"]}
    live = {entry["name"]: entry for entry in client.get_all("/labels")}
    findings = []
    for name, wanted in expected.items():
        actual = live.get(name)
        if actual is None:
            findings.append(finding("managed-labels", f"canonical label missing from repository: {name}"))
            continue
        live_shape = {
            "name": actual.get("name"),
            "color": str(actual.get("color", "")).lower(),
            "description": actual.get("description") or "",
        }
        wanted_shape = {"name": wanted["name"], "color": wanted["color"].lower(), "description": wanted["description"]}
        if live_shape != wanted_shape:
            findings.append(finding("managed-labels", f"label {name} metadata drift: expected {wanted_shape!r}, live {live_shape!r}"))
    return findings


def result(classification, policy_findings, infrastructure_failures, manual_readback):
    return {
        "schema_version": 1,
        "classification": classification,
        "policy_findings": policy_findings,
        "infrastructure_failures": infrastructure_failures,
        "manual_readback": manual_readback,
    }


def classify(policy_findings, infrastructure_failures):
    if infrastructure_failures:
        return "infrastructure-failure"
    if policy_findings:
        return "policy-drift"
    return "clean"


def run_audit(client, run=command):
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy_findings, infrastructure = run_canonical_checks(run)
    manual = [
        {"id": c["id"], "classification": c["classification"], "expected": c["expected"]}
        for c in policy["controls"] if c["readback"] == "manual-readback"
    ]
    try:
        live_findings, manual = run_live_policy_checks(client, policy)
        policy_findings.extend(live_findings)
        policy_findings.extend(run_live_label_checks(client))
    except (ApiFailure, OSError, json.JSONDecodeError) as exc:
        infrastructure.append(finding("live-readback", str(exc)))
    return result(classify(policy_findings, infrastructure), policy_findings, infrastructure, manual)


def synthetic(mode):
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    manual = [{"id": c["id"], "classification": c["classification"], "expected": c["expected"]} for c in policy["controls"] if c["readback"] == "manual-readback"]
    if mode == "clean":
        return result("clean", [], [], manual)
    if mode == "policy-drift":
        return result("policy-drift", [finding("synthetic", "safe synthetic policy drift")], [], manual)
    if mode == "infrastructure-failure":
        return result("infrastructure-failure", [], [finding("synthetic", "safe synthetic readback failure")], manual)
    raise ValueError(mode)


def exit_code_for(classification):
    return {"clean": 0, "policy-drift": 1, "infrastructure-failure": 2}[classification]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--test-mode", choices=("live", "clean", "policy-drift", "infrastructure-failure"), default="live")
    args = parser.parse_args()
    try:
        if args.test_mode == "live":
            repository = os.environ.get("GITHUB_REPOSITORY")
            token = os.environ.get("GITHUB_TOKEN")
            if not repository or not token:
                raise ApiFailure("GITHUB_REPOSITORY and GITHUB_TOKEN are required for live audit")
            audit = run_audit(GitHubReadClient(repository, token))
        else:
            audit = synthetic(args.test_mode)
    except (ApiFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        audit = result("infrastructure-failure", [], [finding("audit-runner", str(exc))], [])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(audit, indent=2, sort_keys=True))
    return exit_code_for(audit["classification"])


if __name__ == "__main__":
    raise SystemExit(main())
