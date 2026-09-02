#!/usr/bin/env python3

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAXONOMY = ROOT / ".github" / "labels.json"
MARKER = "<!-- oxide-batch-workloads:supply-chain-audit -->"
TITLE = "[supply-chain-audit] scheduled audit requires attention"
LABELS = ["type:security", "area:security", "dependencies"]


def validate_reporting_labels(path=TAXONOMY):
    taxonomy = json.loads(path.read_text(encoding="utf-8"))
    known = {entry["name"] for entry in taxonomy["labels"]}
    missing = sorted(set(LABELS) - known)
    if missing:
        raise ValueError(f"scheduled audit reporting labels are missing from canonical taxonomy: {', '.join(missing)}")


class GitHubClient:
    def __init__(self, repository, token):
        self.base = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "oxide-batch-workloads-supply-chain-audit",
        }

    def request(self, method, url, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {url} failed: {exc.code}: {body}") from exc

    def all_issues(self):
        page = 1
        while True:
            items = self.request("GET", f"{self.base}/issues?state=all&per_page=100&page={page}")
            if not items:
                return
            for item in items:
                if "pull_request" not in item:
                    yield item
            if len(items) < 100:
                return
            page += 1

    def create_issue(self, body):
        return self.request("POST", f"{self.base}/issues", {"title": TITLE, "body": body, "labels": LABELS})

    def update_issue(self, number, **payload):
        return self.request("PATCH", f"{self.base}/issues/{number}", payload)

    def comment(self, number, body):
        return self.request("POST", f"{self.base}/issues/{number}/comments", {"body": body})


def find_owned_issue(client):
    matches = [issue for issue in client.all_issues() if MARKER in (issue.get("body") or "")]
    if len(matches) > 1:
        raise RuntimeError(f"multiple owned supply-chain audit issues found: {[x['number'] for x in matches]}")
    return matches[0] if matches else None


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

    owned = find_owned_issue(client)
    if classification == "clean":
        if owned and owned.get("state") == "open":
            client.comment(owned["number"], f"Audit recovered to clean. Run: {run_url}")
            client.update_issue(owned["number"], state="closed", state_reason="completed")
            return f"closed recovered issue #{owned['number']}"
        return "clean; no open owned issue"

    body = render_body(audit, run_url)
    if owned is None:
        created = client.create_issue(body)
        return f"created issue #{created['number']}"

    number = owned["number"]
    payload = {"title": TITLE, "body": body, "labels": LABELS}
    if owned.get("state") != "open":
        payload.update({"state": "open", "state_reason": "reopened"})
    client.update_issue(number, **payload)
    client.comment(number, f"Audit remains non-clean with classification `{classification}`. Run: {run_url}")
    return f"updated issue #{number}"


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
        client = GitHubClient(repository, token)
        print(reconcile_issue(client, audit, args.run_url))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"supply-chain audit reporting failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
