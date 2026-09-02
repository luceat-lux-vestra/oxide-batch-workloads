#!/usr/bin/env python3

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAXONOMY_PATH = ROOT / ".github" / "labels.json"
WORKLOADS_PATH = ROOT / "workloads.json"

TYPE_RULES = (
    (r"^epic\s*:", "type:epic"),
    (r"^track(?:\([^)]*\))?\s*:", "type:track"),
    (r"^(?:workload|campaign)\s*:", "type:campaign"),
    (r"^(?:bug|fix)\s*:", "type:bug"),
    (r"^security\s*:", "type:security"),
    (r"^(?:docs?|documentation)\s*:", "type:docs"),
    (r"^research\s*:", "type:research"),
    (r"^(?:task|chore|governance|ci|evidence|deps?)\s*:", "type:task"),
    (r"^chore\(deps\)\s*:", "type:task"),
)

AREA_PREFIX_RULES = (
    (r"^ci\s*:", "area:ci"),
    (r"^(?:governance|chore)\s*:", "area:governance"),
    (r"^evidence\s*:", "area:evidence"),
    (r"^security\s*:", "area:security"),
    (r"^(?:docs?|documentation)\s*:", "area:docs"),
)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_policy():
    taxonomy = load_json(TAXONOMY_PATH)
    workloads = load_json(WORKLOADS_PATH)
    known = {entry["name"] for entry in taxonomy["labels"]}
    structural = set(taxonomy["structural_types"])
    singular_groups = taxonomy.get("singular_groups", [])
    matching_groups = [prefix for prefix in singular_groups if structural and all(name.startswith(prefix) for name in structural)]
    if len(matching_groups) != 1:
        raise ValueError("canonical taxonomy must define exactly one singular group containing all structural types")
    type_prefix = matching_groups[0]

    referenced = {label for _, label in TYPE_RULES + AREA_PREFIX_RULES}
    referenced.update(("area:benchmark", "area:interop", "area:postgres", "area:workload", "area:evidence", "area:ci", "area:governance", "area:docs", "area:security"))
    missing = sorted(referenced - known)
    if missing:
        raise ValueError(f"automation references labels missing from canonical taxonomy: {', '.join(missing)}")

    workload_paths = []
    for group in ("workloads", "fixtures"):
        for entry in workloads.get(group, []):
            workload_paths.append((entry["name"], entry["path"].rstrip("/") + "/"))
    return {
        "known": known,
        "structural": structural,
        "type_prefix": type_prefix,
        "workload_paths": workload_paths,
    }


def strong_type_from_title(title):
    value = title.strip().lower()
    for pattern, label in TYPE_RULES:
        if re.search(pattern, value):
            return label
    return None


def infer_title_areas(title):
    value = title.strip().lower()
    areas = set()
    for pattern, label in AREA_PREFIX_RULES:
        if re.search(pattern, value):
            areas.add(label)
    if "benchmark" in value:
        areas.add("area:benchmark")
    if any(term in value for term in ("interop", "scheduler", "orchestrator", "event-driven", "control-plane")):
        areas.add("area:interop")
    if "postgres" in value or "postgresql" in value:
        areas.add("area:postgres")
    return areas


def infer_path_areas(paths, policy):
    areas = set()
    for raw_path in paths:
        path = raw_path.lstrip("/")
        if path.startswith(".github/workflows/"):
            areas.update(("area:ci", "area:governance"))
        elif path.startswith(".github/scripts/"):
            areas.add("area:ci")
        if path == ".github/labels.json" or path.startswith(".github/ISSUE_TEMPLATE/"):
            areas.add("area:governance")
        if path in ("CONTRIBUTING.md", "CODE_OF_CONDUCT.md"):
            areas.update(("area:docs", "area:governance"))
        if path == "SECURITY.md":
            areas.update(("area:docs", "area:security"))
        if "/validation/" in f"/{path}" or path.endswith("/validation"):
            areas.add("area:evidence")
        for name, prefix in policy["workload_paths"]:
            if path == prefix[:-1] or path.startswith(prefix):
                areas.add("area:workload")
                if "postgres" in name.lower() or "postgres" in prefix.lower():
                    areas.add("area:postgres")
    return areas


def reconcile_labels(current_labels, inferred_type, inferred_areas, policy):
    current = list(dict.fromkeys(current_labels))
    type_prefix = policy["type_prefix"]
    type_labels = [label for label in current if label.startswith(type_prefix)]
    structural = [label for label in type_labels if label in policy["structural"]]

    if len(structural) > 1:
        raise ValueError(f"multiple structural type labels require manual resolution: {structural}")

    if structural:
        selected_type = structural[0]
    elif inferred_type:
        selected_type = inferred_type
    elif len(type_labels) <= 1:
        selected_type = type_labels[0] if type_labels else None
    else:
        raise ValueError(f"multiple managed type labels without an authoritative signal: {type_labels}")

    desired = [label for label in current if not label.startswith(type_prefix)]
    if selected_type:
        desired.append(selected_type)

    for area in sorted(inferred_areas):
        if area in policy["known"] and area not in desired:
            desired.append(area)

    return list(dict.fromkeys(desired))


class GitHubClient:
    def __init__(self, repository, token):
        self.base = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "oxide-batch-workloads-label-reconciler",
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

    def paged(self, url):
        page = 1
        while True:
            separator = "&" if "?" in url else "?"
            items = self.request("GET", f"{url}{separator}per_page=100&page={page}")
            if not items:
                return
            for item in items:
                yield item
            if len(items) < 100:
                return
            page += 1

    def issue(self, number):
        return self.request("GET", f"{self.base}/issues/{number}")

    def pr_paths(self, number):
        return [entry["filename"] for entry in self.paged(f"{self.base}/pulls/{number}/files")]

    def replace_labels(self, number, labels):
        return self.request("PUT", f"{self.base}/issues/{number}/labels", {"labels": labels})

    def open_items(self):
        return self.paged(f"{self.base}/issues?state=open")


def labels_from_item(item):
    return [label["name"] for label in item.get("labels", [])]


def classify_item(item, policy, client=None):
    title = item.get("title", "")
    inferred_type = strong_type_from_title(title)
    inferred_areas = infer_title_areas(title)
    if "pull_request" in item:
        if client is None:
            raise ValueError("PR classification requires a GitHub client")
        inferred_areas |= infer_path_areas(client.pr_paths(item["number"]), policy)
    return reconcile_labels(labels_from_item(item), inferred_type, inferred_areas, policy)


def apply_item(client, item, policy, dry_run=False):
    desired = classify_item(item, policy, client)
    current = labels_from_item(item)
    if len(desired) == len(current) and set(desired) == set(current):
        print(f"#{item['number']}: labels already reconciled")
        return False
    print(f"#{item['number']}: {current} -> {desired}")
    if not dry_run:
        client.replace_labels(item["number"], desired)
    return True


def load_event(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))
    args = parser.parse_args()

    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repository or not token:
        print("GITHUB_REPOSITORY and GITHUB_TOKEN are required", file=sys.stderr)
        return 2

    try:
        policy = load_policy()
        client = GitHubClient(repository, token)
        if args.backfill:
            changed = 0
            for item in client.open_items():
                changed += int(apply_item(client, item, policy, args.dry_run))
            print(f"backfill complete: {changed} item(s) changed")
            return 0

        if not args.event_path:
            print("event path is required outside --backfill mode", file=sys.stderr)
            return 2
        event = load_event(args.event_path)
        item = event.get("issue") or event.get("pull_request")
        if not item:
            print("event has no issue or pull_request payload; nothing to do")
            return 0
        fresh = client.issue(item["number"])
        apply_item(client, fresh, policy, args.dry_run)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"label reconciliation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
