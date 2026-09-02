#!/usr/bin/env python3

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAXONOMY = ROOT / ".github" / "labels.json"
ISSUE_TEMPLATE_DIR = ROOT / ".github" / "ISSUE_TEMPLATE"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
LABEL_LINE = re.compile(r'^\s*labels:\s*\[(.*)\]\s*$')
QUOTED = re.compile(r'["\']([^"\']+)["\']')


def fail(message: str) -> None:
    raise ValueError(message)


def load_taxonomy(path: pathlib.Path = TAXONOMY) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        fail("labels.json schema_version must be 1")

    prefixes = data.get("managed_prefixes")
    singular = data.get("singular_groups")
    labels = data.get("labels")
    if not isinstance(prefixes, list) or not prefixes or not all(isinstance(x, str) and x for x in prefixes):
        fail("managed_prefixes must be a non-empty string list")
    if not isinstance(singular, list) or not all(x in prefixes for x in singular):
        fail("singular_groups must be a subset of managed_prefixes")
    if not isinstance(labels, list) or not labels:
        fail("labels must be a non-empty list")

    seen = set()
    for entry in labels:
        if not isinstance(entry, dict):
            fail("every label entry must be an object")
        name = entry.get("name")
        color = entry.get("color")
        description = entry.get("description")
        if not isinstance(name, str) or not name.strip():
            fail("label name must be non-empty")
        if name in seen:
            fail(f"duplicate label name: {name}")
        seen.add(name)
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            fail(f"label {name!r} must use a six-digit hex color")
        if not isinstance(description, str) or not description.strip():
            fail(f"label {name!r} must have a description")
        if name.startswith("type:") and not any(name.startswith(prefix) for prefix in prefixes):
            fail(f"managed type label {name!r} is outside managed_prefixes")
        if name.startswith("area:") and not any(name.startswith(prefix) for prefix in prefixes):
            fail(f"managed area label {name!r} is outside managed_prefixes")

    required = {
        "type:epic", "type:track", "type:campaign", "type:bug",
        "type:security", "type:docs", "type:research", "type:task",
        "area:ci", "area:governance", "area:workload", "area:evidence",
        "area:postgres", "area:benchmark", "area:interop", "area:security",
        "area:docs", "dependencies",
    }
    missing = sorted(required - seen)
    if missing:
        fail(f"canonical taxonomy is missing required labels: {', '.join(missing)}")
    return data


def extract_issue_form_labels(path: pathlib.Path) -> list[str]:
    labels = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LABEL_LINE.match(line)
        if match:
            labels.extend(QUOTED.findall(match.group(1)))
    return labels


def validate_references(data: dict) -> None:
    known = {entry["name"] for entry in data["labels"]}
    errors = []
    for path in sorted(ISSUE_TEMPLATE_DIR.glob("*.yml")):
        if path.name == "config.yml":
            continue
        for label in extract_issue_form_labels(path):
            if label not in known:
                errors.append(f"{path.relative_to(ROOT)} references unmanaged/nonexistent label {label!r}")

    dependabot_text = DEPENDABOT.read_text(encoding="utf-8")
    if re.search(r"^\s*- dependencies\s*$", dependabot_text, re.MULTILINE) and "dependencies" not in known:
        errors.append("dependabot.yml references 'dependencies' but taxonomy does not define it")

    if errors:
        fail("; ".join(errors))


def main() -> int:
    try:
        data = load_taxonomy()
        validate_references(data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"label taxonomy validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"label taxonomy valid: {len(data['labels'])} managed labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
