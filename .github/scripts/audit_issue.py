#!/usr/bin/env python3

"""Shared owned-issue lifecycle for scheduled repository audits."""

import json
import urllib.error
import urllib.request


class GitHubClient:
    def __init__(self, repository, token, user_agent="oxide-batch-workloads-audit"):
        self.base = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": user_agent,
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

    def create_issue(self, title, body, labels):
        return self.request("POST", f"{self.base}/issues", {"title": title, "body": body, "labels": labels})

    def update_issue(self, number, **payload):
        return self.request("PATCH", f"{self.base}/issues/{number}", payload)

    def comment(self, number, body):
        return self.request("POST", f"{self.base}/issues/{number}/comments", {"body": body})


def find_owned_issue(client, marker, owner_name):
    matches = [issue for issue in client.all_issues() if marker in (issue.get("body") or "")]
    if len(matches) > 1:
        raise RuntimeError(f"multiple owned {owner_name} issues found: {[x['number'] for x in matches]}")
    return matches[0] if matches else None


def reconcile_owned_issue(
    client,
    *,
    marker,
    owner_name,
    title,
    labels,
    classification,
    clean_classification,
    body,
    run_url,
):
    owned = find_owned_issue(client, marker, owner_name)
    if classification == clean_classification:
        if owned and owned.get("state") == "open":
            client.comment(owned["number"], f"Audit recovered to clean. Run: {run_url}")
            client.update_issue(owned["number"], state="closed", state_reason="completed")
            return f"closed recovered issue #{owned['number']}"
        return "clean; no open owned issue"

    if owned is None:
        created = client.create_issue(title, body, labels)
        return f"created issue #{created['number']}"

    number = owned["number"]
    payload = {"title": title, "body": body, "labels": labels}
    if owned.get("state") != "open":
        payload.update({"state": "open", "state_reason": "reopened"})
    client.update_issue(number, **payload)
    client.comment(number, f"Audit remains non-clean with classification `{classification}`. Run: {run_url}")
    return f"updated issue #{number}"
