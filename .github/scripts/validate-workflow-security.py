#!/usr/bin/env python3

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")


class WorkflowSecurityError(ValueError):
    pass


def fail(message):
    raise WorkflowSecurityError(message)


def validate_action_pins(root=ROOT):
    violations = []
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = USES.match(line)
            if not match:
                continue
            value = match.group(1)
            if value.startswith("./"):
                continue
            if "@" not in value:
                violations.append(f"{path.relative_to(root)}:{lineno}: external action has no immutable ref: {value}")
                continue
            _action, ref = value.rsplit("@", 1)
            if not FULL_SHA.fullmatch(ref):
                violations.append(f"{path.relative_to(root)}:{lineno}: external action ref is not a full SHA: {value}")
    if violations:
        fail("\n".join(violations))


def validate_label_automation_boundary(root=ROOT):
    path = root / ".github" / "workflows" / "label-automation.yml"
    text = path.read_text(encoding="utf-8")
    required = [
        "pull_request_target:",
        "issues: write",
        "pull-requests: write",
        "ref: ${{ github.event.repository.default_branch }}",
        "persist-credentials: false",
        "python3 .github/scripts/reconcile-labels.py",
    ]
    missing = [value for value in required if value not in text]
    if missing:
        fail("label automation lost trusted pull_request_target boundary: " + ", ".join(missing))
    if "ref: ${{ github.event.pull_request.head.sha }}" in text or "ref: ${{ github.head_ref }}" in text:
        fail("label automation must not check out pull-request head code under write permissions")


def validate_audit_permission_boundaries(root=ROOT):
    for filename in ("scheduled-supply-chain-audit.yml", "hardening-drift-audit.yml"):
        path = root / ".github" / "workflows" / filename
        if not path.exists():
            if filename == "hardening-drift-audit.yml":
                continue
            fail(f"missing required workflow: {filename}")
        text = path.read_text(encoding="utf-8")
        if "permissions: {}" not in text:
            fail(f"{filename} must default to no workflow permissions")
        detect = text.split("report:", 1)[0]
        if "issues: write" in detect:
            fail(f"{filename} detection path must not have issues: write")
        if "report:" in text:
            report = text.split("report:", 1)[1]
            if "issues: write" not in report:
                fail(f"{filename} reporting job must isolate issues: write")


def validate(root=ROOT):
    validate_action_pins(root)
    validate_label_automation_boundary(root)
    validate_audit_permission_boundaries(root)


def main():
    try:
        validate()
        print("workflow security invariants: ok")
        return 0
    except (OSError, WorkflowSecurityError) as exc:
        print(f"workflow security invariant violation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
