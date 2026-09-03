#!/usr/bin/env python3

import argparse
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAXONOMY = ROOT / ".github" / "labels.json"
MARKER = "<!-- oxide-batch-workloads:hardening-drift-audit -->"
TITLE = "[hardening-drift] repository hardening audit requires attention"
LABELS = ["type:task", "area:governance", "area:security", "area:ci"]


def load_helper():
    path = pathlib.Path(__file__).with_name("audit_issue.py")
    spec = importlib.util.spec_from_file_location("audit_issue_helper", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ISSUES = load_helper()


def validate_reporting_labels(path=TAXONOMY):
    taxonomy = json.loads(path.read_text(encoding="utf-8"))
    known = {entry["name"] for entry in taxonomy["labels"]}
    missing = sorted(set(LABELS) - known)
    if missing:
        raise ValueError(f"hardening audit reporting labels are missing from canonical taxonomy: {', '.join(missing)}")


def render_body(audit, run_url):
    classification = audit["classification"]
    findings = audit.get("policy_findings", [])
    infra = audit.get("infrastructure_failures", [])
    manual = audit.get("manual_readback", [])
    sections = [
        MARKER,
        "## Repository hardening drift audit",
        "",
        f"**Classification:** `{classification}`",
        "",
        f"**Workflow run:** {run_url}",
        "",
        "### Confirmed policy drift",
        "",
        "```json",
        json.dumps(findings, indent=2)[:40000],
        "```",
        "",
        "### Infrastructure/readback failures",
        "",
        "```json",
        json.dumps(infra, indent=2)[:20000],
        "```",
        "",
        "### Explicit manual-readback controls",
        "",
        "These controls are canonical but are not claimed as continuously monitored by the low-privilege scheduled audit.",
        "",
        "```json",
        json.dumps(manual, indent=2)[:20000],
        "```",
        "",
        "This issue is owned by the recurring hardening drift audit. Repeated non-clean runs update/reopen this same issue; a later clean run records recovery and closes it.",
    ]
    return "\n".join(sections) + "\n"


def reconcile_issue(client, audit, run_url):
    classification = audit.get("classification")
    if classification not in {"clean", "policy-drift", "infrastructure-failure"}:
        raise ValueError(f"unsupported audit classification: {classification!r}")
    return ISSUES.reconcile_owned_issue(
        client,
        marker=MARKER,
        owner_name="hardening drift audit",
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
        client = ISSUES.GitHubClient(repository, token, "oxide-batch-workloads-hardening-drift-audit")
        print(reconcile_issue(client, audit, args.run_url))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"hardening drift audit reporting failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
