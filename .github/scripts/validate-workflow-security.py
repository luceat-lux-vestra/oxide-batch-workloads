#!/usr/bin/env python3

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")
EPHEMERAL_WORKFLOW = re.compile(r"^(?:research|temporary|tmp|experiment)(?:[-_].*)?\.(?:ya?ml)$", re.IGNORECASE)
LIFECYCLE_OWNER = re.compile(r"^\s*#\s*lifecycle-owner:\s*(\S.*)$", re.IGNORECASE)
LIFECYCLE_RETIRE = re.compile(r"^\s*#\s*lifecycle-retire:\s*(\S.*)$", re.IGNORECASE)


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
            if value.startswith(("./", "$/")):
                continue
            if "@" not in value:
                violations.append(f"{path.relative_to(root)}:{lineno}: external action has no immutable ref: {value}")
                continue
            _action, ref = value.rsplit("@", 1)
            if not FULL_SHA.fullmatch(ref):
                violations.append(f"{path.relative_to(root)}:{lineno}: external action ref is not a full SHA: {value}")
    if violations:
        fail("\n".join(violations))


def validate_ephemeral_workflow_lifecycle(root=ROOT):
    """Require explicit ownership/retirement metadata for ephemeral workflows."""
    violations = []
    for path in workflow_paths(root):
        if not EPHEMERAL_WORKFLOW.fullmatch(path.name):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()[:40]
        owner = next(
            (match.group(1).strip() for line in lines if (match := LIFECYCLE_OWNER.match(line))),
            "",
        )
        retire = next(
            (match.group(1).strip() for line in lines if (match := LIFECYCLE_RETIRE.match(line))),
            "",
        )
        if not owner or not retire:
            violations.append(
                f"{path.relative_to(root)}: ephemeral workflow requires non-empty "
                "# lifecycle-owner: and # lifecycle-retire: comments"
            )
    if violations:
        fail("\n".join(violations))


def validate_label_automation_boundary(root=ROOT):
    path = root / ".github" / "workflows" / "label-automation.yml"
    text = active_text(path)
    required = [
        "pull_request_target:",
        "branches: [main]",
        "if: github.repository == 'luceat-lux-vestra/oxide-batch-workloads'",
        "issues: write",
        "pull-requests: write",
        "ref: ${{ github.event.repository.default_branch }}",
        "persist-credentials: false",
        "python3 .github/scripts/reconcile-labels.py",
        'DRY_RUN: ${{ inputs.dry_run }}',
        'DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}',
        'expected_ref="refs/heads/${DEFAULT_BRANCH}"',
        'if [[ "${DRY_RUN}" != "true" && "${GITHUB_REF}" != "${expected_ref}" ]]; then',
        "Mutating backlog reconciliation must run from",
    ]
    missing = [value for value in required if value not in text]
    if missing:
        fail("label automation lost trusted pull_request_target boundary: " + ", ".join(missing))

    backfill_contract = """      backfill:
        description: Reconcile all currently open issues and pull requests
        required: true
        type: boolean
        default: false"""
    dry_run_contract = """      dry_run:
        description: Report proposed changes without mutating labels
        required: true
        type: boolean
        default: true"""
    if backfill_contract not in text:
        fail("label automation backfill must be explicit opt-in (default: false)")
    if dry_run_contract not in text:
        fail("label automation dry_run must default fail-safe (default: true)")
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


def load_structured_yaml(path):
    """Use the maintained runner yq (YAML 1.2), not a line parser."""
    import json
    import subprocess

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail(f"duplicate YAML key: {key}")
            result[key] = value
        return result

    result = subprocess.run(
        ["yq", "-o=json", "-I=0", ".", str(path)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        fail(f"invalid YAML {path}: {result.stderr}")
    try:
        value = json.loads(result.stdout, object_pairs_hook=unique)
    except ValueError as exc:
        fail(f"invalid YAML object {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected one YAML mapping: {path}")
    return value


def native_updaters(root=ROOT):
    config = load_structured_yaml(root / ".github" / "dependabot.yml")
    if config.get("version") != 2 or not isinstance(config.get("updates"), list):
        fail("invalid Dependabot configuration")
    owners = {}
    for entry in config["updates"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("package-ecosystem"), str):
            fail("invalid Dependabot entry")
        ecosystem = entry["package-ecosystem"]
        if ("directory" in entry) == ("directories" in entry):
            fail("updater must declare one unambiguous directory or directories block")
        dirs = [entry["directory"]] if "directory" in entry else entry["directories"]
        if not isinstance(dirs, list) or not dirs:
            fail("updater requires explicit canonical directory paths")
        schedule = entry.get("schedule", {})
        if not isinstance(schedule, dict) or schedule.get("interval") not in {
            "daily", "weekly", "monthly", "quarterly", "semiannually", "yearly", "cron"
        }:
            fail("updater missing supported schedule")
        limit = entry.get("open-pull-requests-limit", 5)
        if type(limit) is not int or limit <= 0:
            fail("version updater disabled or invalid")
        if entry.get("target-branch", "main") != "main":
            fail("updater must target protected main")
        for directory in dirs:
            if not isinstance(directory, str) or (directory != "/" and (
                not re.fullmatch(r"/[A-Za-z0-9_./-]+", directory)
                or any(part in {"", ".", ".."} for part in directory[1:].split("/"))
            )):
                fail("updater requires explicit canonical directory paths")
            key = (ecosystem, directory)
            if key in owners:
                fail(f"duplicate {ecosystem.capitalize()} updater directory: {directory}")
            ignored = entry.get("ignore", [])
            if not isinstance(ignored, list) or any(not isinstance(rule, dict) for rule in ignored):
                fail("invalid updater ignore rules")
            if any(rule.get("dependency-name") == "*" for rule in ignored):
                fail(f"blanket {ecosystem.capitalize()} ignore: {directory}")
            owners[key] = entry
    return owners


def validate_registered_cargo_updaters(root=ROOT, owners=None):
    import json

    registry = json.loads((root / "workloads.json").read_text(encoding="utf-8"))
    if owners is None:
        owners = native_updaters(root)
    for kind in ("workloads", "fixtures"):
        for item in registry[kind]:
            directory = "/" + item["path"].strip("/")
            entry = owners.get(("cargo", directory))
            if entry is None:
                fail("missing native Cargo updater: " + directory)
            ignored = [rule.get("dependency-name") for rule in entry.get("ignore", [])]
            if kind == "workloads" and "oxide-batch*" not in ignored:
                fail("published OxideBatch subject must remain campaign-owned: " + directory)



def validate(root=ROOT):
    owners = native_updaters(root)
    validate_registered_cargo_updaters(root, owners)
    from dependency_lifecycle import validate as validate_lifecycle
    validate_lifecycle(root, owners, load_structured_yaml, fail)
    validate_action_pins(root)
    validate_ephemeral_workflow_lifecycle(root)
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
