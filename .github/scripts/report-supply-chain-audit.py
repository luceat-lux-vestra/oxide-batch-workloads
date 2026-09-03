#!/usr/bin/env python3

import argparse
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAXONOMY = ROOT / ".github" / "labels.json"
MARKER = "<!-- oxide-batch-workloads:supply-chain-audit -->"
TITLE = "[supply-chain-audit] scheduled audit requires attention"
LABELS = ["type:security", "area:security", "dependencies"]


def load_helper():
    path = pathlib.Path(__file__).with_name("audit_issue.py")
    spec = importlib.util.spec_from_file_location("audit_issue_helper_for_supply_chain", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ISSUES = load_helper()
GitHubClient = ISSUES.GitHubClient


def validate_reporting_labels(path=TAXONOMY):
    taxonomy = json.loads(path.read_text(encoding="utf-8"))
    known = {entry["name"] for entry in taxonomy["labels"]}
    missing = sorted(set(LABELS) - known)
    if missing:
        raise ValueError(f"scheduled audit reporting labels are missing from canonical taxonomy: {', '.join(missing)}")


def find_owned_issue(client):
    return ISSUES.find_owned_issue(client, MARKER, "supply-chain audit")


def render_body(audit, run_url):
    classification = audit["classification"]
    details = audit.get("details", "")
    workloads = audit.get("workloads", [])
    return (
        f"{MARKER}\n"
        "## Scheduled supply-chain audit\n\n"
        f"**Classification:** `{classification}`\n\n"
        f"**Scanned workloads:** {', '.join(workloads) if workloads else '(none)'}\n\n"
        f"**Workflow run:** {run_url}\n\n"
        "### Details\n\n"
        f"```text\n{details[:50000]}\n```\n\n"
        "This issue is owned by the scheduled supply-chain audit. Repeated non-clean runs update/reopen this same issue; a later clean run adds a recovery comment and closes it.\n"
    )


def reconcile_issue(client, audit, run_url):
    classification = audit.get("classification")
    if classification not in {"clean", "policy-finding", "infrastructure-failure"}:
        raise ValueError(f"unsupported audit classification: {classification!r}")
    return ISSUES.reconcile_owned_issue(
        client,
        marker=MARKER,
        owner_name="supply-chain audit",
        title=TITLE,
        labels=LABELS,
        classification=classification,
        clean_classification="clean",
        body=render_body(audit, run_url),
        run_url=run_url,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=pathlib.Path, required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()

    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repository or not token:
        print("GITHUB_REPOSITORY and GITHUB_TOKEN are required", file=sys.stderr)
        return 2

    try:
        validate_reporting_labels()
        audit = json.loads(args.result.read_text(encoding="utf-8"))
        client = GitHubClient(repository, token, "oxide-batch-workloads-supply-chain-audit")
        print(reconcile_issue(client, audit, args.run_url))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"supply-chain audit reporting failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
