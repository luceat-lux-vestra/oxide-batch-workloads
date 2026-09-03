#!/usr/bin/env python3

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")


class WorkflowSecurityError(ValueError):
    pass


def fail(message):
    raise WorkflowSecurityError(message)


def workflow_paths(root):
    directory = root / ".github" / "workflows"
    return sorted(set(directory.glob("*.yml")) | set(directory.glob("*.yaml")))


def active_lines(path):
    return [
        line.rstrip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def active_text(path):
    return "\n".join(active_lines(path))


def validate_action_pins(root=ROOT):
    violations = []
    for path in workflow_paths(root):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
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
    text = active_text(path)
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


def job_section(lines, job_name, next_job_name=None):
    start_marker = f"  {job_name}:"
    try:
        start = lines.index(start_marker)
    except ValueError as exc:
        fail(f"missing workflow job: {job_name}")
    if next_job_name is None:
        return lines[start:]
    end_marker = f"  {next_job_name}:"
    try:
        end = lines.index(end_marker, start + 1)
    except ValueError as exc:
        fail(f"missing workflow job: {next_job_name}")
    return lines[start:end]


def validate_audit_permission_boundaries(root=ROOT):
    for filename in ("scheduled-supply-chain-audit.yml", "hardening-drift-audit.yml"):
        path = root / ".github" / "workflows" / filename
        if not path.exists():
            if filename == "hardening-drift-audit.yml":
                continue
            fail(f"missing required workflow: {filename}")
        lines = active_lines(path)
        if "permissions: {}" not in [line.strip() for line in lines]:
            fail(f"{filename} must default to no workflow permissions")
        detect = "\n".join(job_section(lines, "detect", "report"))
        report = "\n".join(job_section(lines, "report"))
        if "issues: write" in detect:
            fail(f"{filename} detection path must not have issues: write")
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
