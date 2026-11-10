#!/usr/bin/env python3
"""Selective read-only CodeQL PR runtime compatibility contract for oxide-batch-workloads."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"[0-9a-f]{40}\Z")
ACTION_LINE = re.compile(
    r"^\s*uses:\s*github/codeql-action/(?P<action>init|analyze)@"
    r"(?P<sha>[0-9a-f]{40})(?:\s+#.*)?\s*$"
)
SIMPLE_STATUSES = frozenset({"added", "modified", "removed"})
MOVE_STATUSES = frozenset({"renamed", "copied"})
RELEVANT_PATHS = frozenset({
    ".github/workflows/codeql.yml",
    ".github/workflows/ci.yml",
    ".github/scripts/trusted-codeql-runtime-validation.py",
    ".github/scripts/release-codeql-verdict.py",
    ".github/scripts/validate-workflow-security.py",
    ".github/scripts/test-validate-workflow-security.py",
    ".github/scripts/test_aggregate_verdict.py",
    ".github/scripts/check-actions-security.sh",
    "tools/ci/release_codeql_preflight.py",
    "tools/ci/verify_release_codeql_policy.py",
    ".github/workflows/release-codeql-preflight.yml",
    ".github/release-codeql-policy.toml",

})

def reject(message: str) -> None:
    raise ValueError(message)

def safe_path(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        reject(f"unsafe repository path: {value!r}")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        reject(f"unsafe repository path: {value!r}")
    return value

def _load_sibling(filename: str, module_name: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        reject(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _flatten_pages(raw: object) -> list[dict]:
    if not isinstance(raw, list) or any(not isinstance(page, list) for page in raw):
        reject("changed-file response must be an array of page arrays")
    records = [item for page in raw for item in page]
    if any(not isinstance(item, dict) for item in records):
        reject("changed-file response contains a non-object record")
    return records

def require_supported_codeql_patch(record: dict) -> None:
    if record.get("status") != "modified":
        reject("codeql.yml compatibility classification only supports in-place modification")
    patch = record.get("patch")
    if not isinstance(patch, str) or not patch:
        reject("codeql.yml patch is missing or truncated; refusing coverage claim")
    changed: list[tuple[str, str, str]] = []
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "@@")) or not line.startswith(("+", "-")):
            continue
        match = ACTION_LINE.fullmatch(line[1:])
        if match is None:
            reject("unsupported codeql.yml mutation outside immutable init/analyze pin lines")
        changed.append((line[0], match.group("action"), match.group("sha")))
    shape = Counter((sign, action) for sign, action, _ in changed)
    expected = Counter({("-", "init"): 1, ("+", "init"): 1, ("-", "analyze"): 1, ("+", "analyze"): 1})
    if shape != expected:
        reject("codeql.yml pin-only patch must replace exactly one init and one analyze pin")

def classify(pr: object, pages: object, *, repo: str, base: str, head: str, head_repo: str) -> bool:
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        reject("expected full base/head SHA")
    if not isinstance(pr, dict):
        reject("pull request metadata must be an object")
    if pr.get("base", {}).get("sha") != base:
        reject("live pull request base SHA drifted")
    if pr.get("head", {}).get("sha") != head:
        reject("live pull request head SHA drifted")
    if pr.get("base", {}).get("repo", {}).get("full_name") != repo:
        reject("live pull request base repository drifted")
    if pr.get("head", {}).get("repo", {}).get("full_name") != head_repo:
        reject("live pull request head repository drifted")
    expected = pr.get("changed_files")
    if not isinstance(expected, int) or expected <= 0 or expected > 3000:
        reject("live pull request changed_files is outside the supported bound")
    records = _flatten_pages(pages)
    if len(records) != expected:
        reject(f"incomplete changed-file enumeration: expected {expected}, got {len(records)}")
    seen: set[tuple[str, str, str]] = set()
    relevant = False
    for record in records:
        status = record.get("status")
        filename = safe_path(record.get("filename"))
        previous_raw = record.get("previous_filename")
        if status in SIMPLE_STATUSES:
            if previous_raw not in (None, ""):
                reject(f"unexpected previous_filename for {status}: {filename}")
            previous = ""
        elif status in MOVE_STATUSES:
            previous = safe_path(previous_raw)
        else:
            reject(f"unsupported changed-file status {status!r} for {filename!r}")
        key = (status, filename, previous)
        if key in seen:
            reject(f"duplicate changed-file record: {filename}")
        seen.add(key)
        paths = (filename, previous) if previous else (filename,)
        if ".github/workflows/codeql.yml" in paths:
            require_supported_codeql_patch(record)
        if any(path in RELEVANT_PATHS for path in paths):
            relevant = True
    return relevant

def _job_block(text: str, job_id: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(job_id)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
    if match is None:
        reject(f"missing job {job_id}")
    return match.group(0)

def _action_pin(block: str, action: str, where: str) -> str:
    matches = re.findall(rf"(?m)^\s+uses:\s*github/codeql-action/{re.escape(action)}@([0-9a-f]{{40}})(?:\s+#.*)?$", block)
    if len(matches) != 1:
        reject(f"{where}: expected exactly one immutable CodeQL {action} action")
    return matches[0]

def _job_permissions(block: str) -> dict[str, str]:
    # Parse only job-local permission entries; inline comments must never consume
    # the following YAML lines, and duplicate/malformed entries fail closed.
    matches = list(re.finditer(r"(?m)^    permissions:[ \t]*\n", block))
    if len(matches) != 1:
        reject("codeql-runtime job-local permissions missing or duplicated")
    pairs: dict[str, str] = {}
    for line in block[matches[0].end():].splitlines():
        if not line.startswith("      "):
            break
        item = re.fullmatch(r" {6}([A-Za-z0-9_-]+):[ \t]*([A-Za-z-]+)[ \t]*(?:#[^\r\n]*)?", line)
        if item is None:
            reject("malformed codeql-runtime permission")
        key = item.group(1)
        if key in pairs:
            reject("duplicate codeql-runtime permission")
        pairs[key] = item.group(2)
    if not pairs:
        reject("codeql-runtime permissions are empty")
    return pairs


def check_contract(root: Path = ROOT) -> None:
    """Verify production/PR CodeQL pin parity and separated trust authorities."""
    codeql = (root / ".github/workflows/codeql.yml").read_text(encoding="utf-8")
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    if re.search(r"(?m)^  (?:pull_request|pull_request_target|push):", codeql):
        reject("production CodeQL must remain schedule/manual only")
    for fragment in ("  workflow_dispatch:", "  schedule:", "language: [actions, python, rust, java-kotlin]", "permissions: {}"):
        if fragment not in codeql:
            reject(f"production CodeQL missing trusted boundary: {fragment}")
    prod = _job_block(codeql, "analyze")
    runtime = _job_block(ci, "codeql-runtime")
    discover = _job_block(ci, "discover")
    gate = _job_block(ci, "merge-gate")
    prod_init = _action_pin(prod, "init", "production")
    prod_analyze = _action_pin(prod, "analyze", "production")
    pr_init = _action_pin(runtime, "init", "PR runtime")
    pr_analyze = _action_pin(runtime, "analyze", "PR runtime")
    if prod_init != prod_analyze:
        reject("production CodeQL init/analyze pins differ")
    if pr_init != prod_init or pr_analyze != prod_analyze:
        reject("PR runtime CodeQL pins differ from production")
    if "security-events: write" not in prod or "upload: always" not in prod:
        reject("production CodeQL security-evidence authority changed")
    if _job_permissions(runtime) != {"contents": "read", "pull-requests": "read"}:
        reject("codeql-runtime permissions must be exactly contents:read + pull-requests:read")
    for forbidden in ("security-events: write", "actions: write", "id-token: write", "packages: write", "pull_request_target"):
        if forbidden in runtime:
            reject(f"codeql-runtime gained forbidden authority: {forbidden}")
    required_runtime = (
        "needs: discover",
        "if: ${{ github.event_name == 'pull_request' && github.event.pull_request.draft == false && needs.discover.outputs.codeql_runtime_required == 'true' }}",
        "repository: ${{ github.event.pull_request.head.repo.full_name }}",
        "ref: ${{ github.event.pull_request.head.sha }}",
        "persist-credentials: false",
        "trusted-codeql-runtime-validation.py self-test",
        "trusted-codeql-runtime-validation.py check-contract",
        "languages: actions", "build-mode: none", "category: /language:actions",
        "upload: never", "output: ${{ runner.temp }}/pr-codeql-runtime",
        "trusted-codeql-runtime-validation.py verify",
    )
    for fragment in required_runtime:
        if fragment not in runtime:
            reject(f"codeql-runtime contract missing: {fragment}")
    if "upload-artifact" in runtime:
        reject("PR runtime must not retain SARIF/source as production evidence")
    required_discover = (
        "codeql_runtime_classification_outcome: ${{ steps.codeql-runtime-scope.outcome }}",
        "codeql_runtime_required: ${{ steps.codeql-runtime-scope.outputs.required }}",
        "Classify selective CodeQL runtime from trusted base",
        ".trusted-base/.github/scripts/trusted-codeql-runtime-validation.py",
        "echo 'required=true' >> \"$GITHUB_OUTPUT\"",
        "--github-output \"$GITHUB_OUTPUT\"",
        "trusted-codeql-runtime-validation.py self-test",
        "trusted-codeql-runtime-validation.py check-contract",
    )
    for fragment in required_discover:
        if fragment not in discover:
            reject(f"trusted-base CodeQL classifier contract missing: {fragment}")
    required_gate = (
        "      - codeql-runtime",
        "expected = always_required | {\"dependency-review\", \"codeql-runtime\"} | set(scoped)",
        "codeql_runtime_classification_outcome",
        "codeql_runtime_required",
        "codeql-runtime: expected",
        "codeql-runtime-scope: expected",
    )
    for fragment in required_gate:
        if fragment not in gate:
            reject(f"Merge Gate CodeQL contract missing: {fragment}")

def prove_no_production_promotion(head: str) -> None:
    """Reject complete PR scan metadata even if its SARIF and jobs are clean."""
    if not SHA.fullmatch(head):
        reject("head must be an exact immutable SHA")
    path = ROOT / "tools/ci/release_codeql_preflight.py"
    spec = importlib.util.spec_from_file_location("workloads_release_preflight", path)
    if spec is None or spec.loader is None:
        reject("trusted CodeQL preflight policy missing")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    repo = "owner/repo"
    workflow = {"id": 42}
    run = {
        "workflow_id": 42, "path": ".github/workflows/codeql.yml",
        "event": "pull_request", "head_repository": {"full_name": repo},
        "head_branch": "main", "head_sha": head, "status": "completed",
        "conclusion": "success", "run_attempt": 1, "created_at": "2026-01-01T00:00:00Z",
    }
    jobs = {"total_count": len(gate.LANGUAGES), "jobs": [
        {"name": f"codeql ({lang})", "status": "completed", "conclusion": "success"}
        for lang in gate.LANGUAGES
    ]}
    artifacts = {"total_count": len(gate.LANGUAGES) * 2, "artifacts": [
        {"name": f"{kind}-{head}-{lang}", "id": idx + 1, "size_in_bytes": 100,
         "expired": False, "workflow_run": {"head_sha": head}}
        for idx, (kind, lang) in enumerate(
            (kind, lang) for kind in ("codeql", "codeql-source") for lang in gate.LANGUAGES
        )
    ]}
    try:
        gate.metadata(run, workflow, jobs, artifacts, repo, head)
    except ValueError as exc:
        if "untrusted or mismatched CodeQL run" not in str(exc):
            raise
        return
    raise AssertionError("pull_request CodeQL evidence was accepted as production evidence")

def verify_runtime(directory: Path, head: str) -> dict:
    verdict_module = _load_sibling("release-codeql-verdict.py", "workloads_codeql_verdict")
    verdict = verdict_module.check(directory, "actions", head)
    prove_no_production_promotion(head)
    return {"schema": 1, "event": "pull_request", "source_sha": head, "trust_domain": "untrusted-pr-codeql-runtime", "production_evidence": False, **verdict}

def self_test() -> None:
    repo = "owner/repo"; base = "a" * 40; head = "b" * 40; head_repo = "contributor/repo"
    def pr(count: int = 1) -> dict:
        return {"base": {"sha": base, "repo": {"full_name": repo}}, "head": {"sha": head, "repo": {"full_name": head_repo}}, "changed_files": count}
    assert classify(pr(), [[{"status": "modified", "filename": ".github/workflows/ci.yml"}]], repo=repo, base=base, head=head, head_repo=head_repo)
    assert classify(pr(), [[{"status": "modified", "filename": ".github/scripts/test_aggregate_verdict.py"}]], repo=repo, base=base, head=head, head_repo=head_repo)
    assert not classify(pr(), [[{"status": "modified", "filename": "README.md"}]], repo=repo, base=base, head=head, head_repo=head_repo)
    allowed_patch = "\n".join([
        "@@ -1,2 +1,2 @@",
        "-        uses: github/codeql-action/init@" + "1" * 40 + " # old",
        "+        uses: github/codeql-action/init@" + "2" * 40 + " # new",
        "-        uses: github/codeql-action/analyze@" + "1" * 40 + " # old",
        "+        uses: github/codeql-action/analyze@" + "2" * 40 + " # new",
    ])
    assert classify(pr(), [[{"status": "modified", "filename": ".github/workflows/codeql.yml", "patch": allowed_patch}]], repo=repo, base=base, head=head, head_repo=head_repo)
    try:
        classify(pr(), [[{"status": "modified", "filename": ".github/workflows/codeql.yml", "patch": allowed_patch + "\n+      security-events: write"}]], repo=repo, base=base, head=head, head_repo=head_repo)
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported codeql.yml mutation was accepted")
    for pages in ([[]], [[{"status": "modified", "filename": "../unsafe"}]], [[{"status": "mystery", "filename": "README.md"}]]):
        try:
            classify(pr(), pages, repo=repo, base=base, head=head, head_repo=head_repo)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid changed-file evidence was accepted")
    permission_block = (
        "  codeql-runtime:\n"
        "    permissions:\n"
        "      contents: read # exact PR source\n"
        "      pull-requests: read # live changed-file metadata\n"
        "    steps:\n"
    )
    expected_permissions = {"contents": "read", "pull-requests": "read"}
    assert _job_permissions(permission_block) == expected_permissions
    for invalid in (
        permission_block.replace("      contents: read # exact PR source\n", "      contents: read # exact PR source\n      contents: read # duplicate\n", 1),
        permission_block.replace("      pull-requests: read # live changed-file metadata\n", "      pull-requests: read # live changed-file metadata\n      metadata: [invalid]\n", 1),
    ):
        try:
            _job_permissions(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed/duplicate CodeQL runtime permissions accepted")
    assert _job_permissions(permission_block.replace("contents: read", "contents: write", 1)) != expected_permissions
    prove_no_production_promotion(head)
    verdict_module = _load_sibling("release-codeql-verdict.py", "workloads_codeql_verdict_selftest")
    with tempfile.TemporaryDirectory(prefix="workloads-codeql-runtime-selftest-") as tmp:
        try:
            verdict_module.check(Path(tmp), "actions", head)
        except ValueError as exc:
            if "exactly one nonempty SARIF" not in str(exc):
                raise
        else:
            raise AssertionError("missing SARIF was accepted")
    # The full checked-in PR DAG must pass. Representative drift must fail.
    with tempfile.TemporaryDirectory(prefix="workloads-codeql-contract-selftest-") as tmp:
        root = Path(tmp)
        workflows = root / ".github/workflows"
        workflows.mkdir(parents=True)
        production = (ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8")
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        (workflows / "codeql.yml").write_text(production, encoding="utf-8")
        (workflows / "ci.yml").write_text(ci, encoding="utf-8")
        check_contract(root)
        mutants = (
            ("privileged runtime", ci.replace("      contents: read # exact PR head source\n", "      contents: write # exact PR head source\n", 1)),
            ("SARIF upload", ci.replace("          upload: never\n", "          upload: always\n", 1)),
            ("untrusted checkout", ci.replace("          ref: ${{ github.event.pull_request.head.sha }}\n", "          ref: ${{ github.sha }}\n", 1)),
            ("missing Merge Gate dependency", ci.replace("      - codeql-runtime\n", "", 1)),
        )
        for title, mutated in mutants:
            if mutated == ci:
                raise AssertionError(f"ineffective runtime negative fixture: {title}")
            (workflows / "ci.yml").write_text(mutated, encoding="utf-8")
            try:
                check_contract(root)
            except ValueError:
                pass
            else:
                raise AssertionError(f"runtime contract accepted: {title}")
            (workflows / "ci.yml").write_text(ci, encoding="utf-8")
    print("trusted CodeQL PR runtime adapter self-tests passed")

def write_github_output(path: Path, required: bool) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"required={'true' if required else 'false'}\n")

def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test"); sub.add_parser("check-contract")
    classify_parser = sub.add_parser("classify")
    classify_parser.add_argument("--pr-json", type=Path, required=True); classify_parser.add_argument("--files-json", type=Path, required=True)
    classify_parser.add_argument("--repo", required=True); classify_parser.add_argument("--base", required=True); classify_parser.add_argument("--head", required=True)
    classify_parser.add_argument("--head-repo", required=True); classify_parser.add_argument("--github-output", type=Path, required=True)
    verify_parser = sub.add_parser("verify"); verify_parser.add_argument("--sarif-dir", type=Path, required=True); verify_parser.add_argument("--head", required=True)
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
    elif args.command == "check-contract":
        check_contract(); print("selective CodeQL PR runtime contract: PASS")
    elif args.command == "classify":
        required = classify(json.loads(args.pr_json.read_text(encoding="utf-8")), json.loads(args.files_json.read_text(encoding="utf-8")), repo=args.repo, base=args.base, head=args.head, head_repo=args.head_repo)
        write_github_output(args.github_output, required); print(json.dumps({"required": required}, sort_keys=True))
    elif args.command == "verify":
        print(json.dumps(verify_runtime(args.sarif_dir, args.head), sort_keys=True))
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"::error::Trusted CodeQL PR runtime rejected: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
