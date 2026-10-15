#!/usr/bin/env python3
"""Verify oxide-batch-workloads' nonpublishing CodeQL prepublication contract.

The current repository has no managed GitHub Release publisher. This verifier
keeps the reusable preflight executable and blocks a future checked-in publisher
from introducing a recognized external release mutation unless a dedicated
CodeQL preflight job precedes and gates that mutation.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import textwrap
import tomllib
from pathlib import Path
from typing import Any

POLICY = ".github/release-codeql-policy.toml"
CODEQL = ".github/workflows/codeql.yml"
PREFLIGHT_WORKFLOW = ".github/workflows/release-codeql-preflight.yml"
PREFLIGHT_SCRIPT = "tools/ci/release_codeql_preflight.py"
VERDICT_SCRIPT = ".github/scripts/release-codeql-verdict.py"
CI = ".github/workflows/ci.yml"

EXPECTED_MUTATIONS = [
    "draft-to-public",
    "github-release-create",
    "github-release-upload",
    "crates-io-publish",
    "package-registry-publish",
]

# Deliberately conservative patterns for repository-managed GitHub Release
# mutations. New publication mechanisms must extend this inventory before use.
MUTATION_PATTERNS = (
    re.compile(r"\bgh\s+release\s+create\b"),
    re.compile(r"\bgh\s+release\s+upload\b"),
    re.compile(r"\bgh\s+release\s+edit\b"),
    re.compile(r"\bsoftprops/action-gh-release@"),
    re.compile(r"\bncipollo/release-action@"),
    re.compile(r"\bactions/create-release@"),
    re.compile(r"\bactions/upload-release-asset@"),
    re.compile(r"\bcargo\s+publish\b"),
    re.compile(r"\bmaturin\s+publish\b"),
    re.compile(r"\b(?:twine\s+upload|npm\s+publish|docker\s+push)\b"),
)


class ReleaseCodeQLPolicyError(RuntimeError):
    pass


def reject(message: str) -> None:
    raise ReleaseCodeQLPolicyError(message)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseCodeQLPolicyError(f"cannot read {path}: {exc}") from exc


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseCodeQLPolicyError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        reject(f"{path}: expected TOML table")
    return value


def exact_keys(table: dict[str, Any], expected: set[str], context: str) -> None:
    actual = set(table)
    if actual != expected:
        reject(
            f"{context}: key set drift; missing={sorted(expected - actual)!r} "
            f"unknown={sorted(actual - expected)!r}"
        )


def top_level_job_blocks(source: str) -> list[tuple[str, str, int]]:
    """Return job id, text, and source offset for ordinary two-space YAML jobs."""
    jobs_match = re.search(r"(?m)^jobs:\s*$", source)
    if not jobs_match:
        return []
    start = jobs_match.end()
    matches = list(re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", source[start:]))
    result: list[tuple[str, str, int]] = []
    for index, match in enumerate(matches):
        absolute = start + match.start()
        end = start + matches[index + 1].start() if index + 1 < len(matches) else len(source)
        result.append((match.group(1), source[absolute:end], absolute))
    return result


def contains_release_mutation(source: str) -> bool:
    if any(pattern.search(source) for pattern in MUTATION_PATTERNS):
        return True
    # Inspect a *single gh api command*, including explicit backslash continuations.
    # Checking an entire shell block misclassifies sccache binary downloads under
    # github.com/.../releases plus unrelated POSTs as package publications.
    for match in re.finditer(r"\bgh\s+api\b[^\n]*(?:\\\r?\n[^\n]*)*", source):
        command = match.group()
        if re.search(r"/releases(?:/|\b)", command) and re.search(
            r"(?:-X|--method)\s+(?:POST|PATCH|DELETE)\b", command
        ):
            return True
    return False


def needs_preflight(block: str) -> bool:
    """Inspect one job's needs dependency using bounded line-by-line parsing.

    Unsupported/ambiguous YAML forms are rejected, never assumed safe.
    """
    lines = block.splitlines()
    indices = [i for i, line in enumerate(lines) if line.startswith("    needs:")]
    if len(indices) != 1:
        return False
    index = indices[0]
    line = lines[index]
    if line.startswith("     "):
        return False
    value = line[len("    needs:"):].strip()
    if value:
        if value.startswith("[") and value.endswith("]"):
            deps = [item.strip() for item in value[1:-1].split(",")]
        else:
            deps = [value]
    else:
        deps = []
        for following in lines[index + 1:]:
            if not following.startswith("      "):
                break
            if not following.startswith("      - "):
                return False
            deps.append(following[len("      - "):].strip())
    if not deps or any(re.fullmatch(r"[A-Za-z0-9_-]+", d) is None for d in deps):
        return False
    return len(deps) == len(set(deps)) and "codeql-preflight" in deps


def validate_policy(root: Path) -> str:
    policy = load_toml(root / POLICY)
    exact_keys(
        policy,
        {
            "schema_version",
            "channel",
            "managed_publisher",
            "prepublication_required",
            "preflight_entrypoint",
            "operator_bypass_scope",
            "first_external_mutations",
        },
        "release-codeql policy",
    )
    if policy["schema_version"] != 1:
        reject("release-codeql policy schema_version must be 1")
    if policy["channel"] != "github-releases":
        reject("release-codeql policy channel must be github-releases")
    if policy["prepublication_required"] is not True:
        reject("release-codeql prepublication gate must remain required")
    if policy["preflight_entrypoint"] != PREFLIGHT_SCRIPT:
        reject("release-codeql preflight entrypoint drifted")
    if policy["operator_bypass_scope"] != "owner-ui-api-governance":
        reject("owner UI/API governance limitation must remain explicit")
    if policy["first_external_mutations"] != EXPECTED_MUTATIONS:
        reject("first external mutation inventory drifted")
    publisher = policy["managed_publisher"]
    if not isinstance(publisher, str) or not publisher:
        reject("managed_publisher must be a non-empty string")
    return publisher


def validate_distribution_contract(root: Path) -> None:
    # This repository is a workload consumer, with no checked-in managed publisher.
    # The future publisher's first mutation is validated by validate_publishers.
    return None


def validate_scanner(root: Path) -> None:
    source = read(root / CODEQL)
    if re.search(r"(?m)^  (?:pull_request|pull_request_target|push):", source):
        reject("routine PR/push CodeQL must stay retired")
    required = (
        "run-name: CodeQL source ${{ inputs.candidate_sha || github.sha }}",
        "candidate_sha:",
        "schedule:",
        "language: [actions, python, rust, java-kotlin]",
        "build-mode: none",
        "security-events: write",
        "cancel-in-progress: false",
        "compare/${EXPECTED_SHA}...${remote_main}",
        "merge_base_commit.sha",
        "base_commit.sha",
        "source-identity.json",
        "codeql-source-${{ env.EXPECTED_SHA }}-${{ matrix.language }}",
        "release-codeql-verdict.py",
        "retention-days: 90",
    )
    for token in required:
        if token not in source:
            reject(f"canonical CodeQL scanner missing {token!r}")


def validate_preflight(root: Path) -> None:
    source = read(root / PREFLIGHT_WORKFLOW)
    required = (
        "workflow_dispatch:",
        "candidate_sha:",
        "actions: write",
        "contents: read",
        "cancel-in-progress: false",
        "Release CodeQL preflight",
        PREFLIGHT_SCRIPT,
        "--timeout-seconds 4200",
    )
    for token in required:
        if token not in source:
            reject(f"nonpublishing preflight workflow missing {token!r}")
    if contains_release_mutation(source):
        reject("nonpublishing CodeQL preflight gained a GitHub Release mutation")


def validate_ci_regressions(root: Path) -> None:
    source = read(root / CI)
    for token in (
        "release-codeql-verdict.py --self-test",
        "release_codeql_preflight.py --self-test",
        "verify_release_codeql_policy.py --self-test",
        "verify_release_codeql_policy.py",
    ):
        if token not in source:
            reject(f"PR CI does not exercise release CodeQL regression {token!r}")


def validate_publishers(root: Path, configured: str) -> None:
    workflows = root / ".github/workflows"
    publishers: list[tuple[str, str]] = []
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        rel = path.relative_to(root).as_posix()
        if rel in {CODEQL, PREFLIGHT_WORKFLOW}:
            continue
        source = read(path)
        if contains_release_mutation(source):
            publishers.append((rel, source))

    if not publishers:
        if configured != "not-implemented":
            reject("managed_publisher must remain not-implemented while no publisher exists")
        return
    if len(publishers) != 1:
        reject(f"expected one managed GitHub Release publisher, found {[p[0] for p in publishers]!r}")
    rel, source = publishers[0]
    if configured != rel:
        reject(f"managed_publisher must identify {rel}")

    jobs = top_level_job_blocks(source)
    preflights = [(job, block, offset) for job, block, offset in jobs if job == "codeql-preflight"]
    if len(preflights) != 1:
        reject("managed publisher must define exactly one codeql-preflight job")
    _, preflight, preflight_offset = preflights[0]
    if PREFLIGHT_SCRIPT not in preflight or "actions: write" not in preflight:
        reject("publisher codeql-preflight must invoke trusted reuse-or-auto-scan gate with dispatch authority")
    if contains_release_mutation(preflight):
        reject("publisher codeql-preflight must be nonpublishing")

    mutation_jobs = [
        (job, block, offset)
        for job, block, offset in jobs
        if job != "codeql-preflight" and contains_release_mutation(block)
    ]
    if not mutation_jobs:
        reject("publisher mutation was not attributable to a job")
    for job, block, offset in mutation_jobs:
        if offset <= preflight_offset:
            reject(f"publication job {job} appears before codeql-preflight")
        if not needs_preflight(block):
            reject(f"publication job {job} is not gated by needs: codeql-preflight")


def verify(root: Path) -> None:
    root = root.resolve()
    configured = validate_policy(root)
    validate_distribution_contract(root)
    validate_scanner(root)
    validate_preflight(root)
    validate_ci_regressions(root)
    validate_publishers(root, configured)


def fixture_files(root: Path, publisher: str | None = None) -> None:
    (root / ".github/workflows").mkdir(parents=True)
    (root / ".github").mkdir(exist_ok=True)
    (root / ".github/scripts").mkdir(parents=True, exist_ok=True)
    (root / "tools/ci").mkdir(parents=True, exist_ok=True)
    managed = publisher or "not-implemented"
    (root / POLICY).write_text(
        textwrap.dedent(
            f'''\
            schema_version = 1
            channel = "github-releases"
            managed_publisher = "{managed}"
            prepublication_required = true
            preflight_entrypoint = "tools/ci/release_codeql_preflight.py"
            operator_bypass_scope = "owner-ui-api-governance"
            first_external_mutations = [
                "draft-to-public",
                "github-release-create",
                "github-release-upload",
                "crates-io-publish",
                "package-registry-publish",
            ]
            '''
        ),
        encoding="utf-8",
    )
    (root / ".github/distribution-policy.toml").write_text(
        textwrap.dedent(
            '''\
            decision = "github-release-cli"
            [channels.github_release]
            publishable = true
            package = "arkst-cli"
            binary = "arkst"
            publication_channel = "github-releases"
            [channels.crates_io]
            publishable = false
            cargo_install = false
            [channels.wasm]
            publishable = false
            distribution_status = "compile-only"
            '''
        ),
        encoding="utf-8",
    )
    (root / CODEQL).write_text(
        textwrap.dedent(
            '''\
            name: CodeQL
            run-name: CodeQL source ${{ inputs.candidate_sha || github.sha }}
            on:
              workflow_dispatch:
                inputs:
                  candidate_sha:
              schedule:
                - cron: "0 0 * * 1"
            concurrency:
              cancel-in-progress: false
            language: [actions, python, rust, java-kotlin]
            build-mode: none
            security-events: write
            compare/${EXPECTED_SHA}...${remote_main}
            merge_base_commit.sha
            base_commit.sha
            source-identity.json
            codeql-source-${{ env.EXPECTED_SHA }}-${{ matrix.language }}
            release-codeql-verdict.py
            retention-days: 90
            '''
        ),
        encoding="utf-8",
    )
    (root / PREFLIGHT_WORKFLOW).write_text(
        textwrap.dedent(
            f'''\
            name: Release CodeQL preflight
            on:
              workflow_dispatch:
                inputs:
                  candidate_sha:
            concurrency:
              cancel-in-progress: false
            jobs:
              preflight:
                permissions:
                  actions: write
                  contents: read
                run: python3 {PREFLIGHT_SCRIPT} --timeout-seconds 4200
            '''
        ),
        encoding="utf-8",
    )
    (root / CI).write_text(
        "release-codeql-verdict.py --self-test\n"
        "release_codeql_preflight.py --self-test\n"
        "verify_release_codeql_policy.py --self-test\n"
        "verify_release_codeql_policy.py\n",
        encoding="utf-8",
    )


def self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture_files(root)
        verify(root)

    publisher = ".github/workflows/release.yml"
    good = textwrap.dedent(
        f'''\
        name: release
        jobs:
          classify:
            run: echo read-only
          codeql-preflight:
            permissions:
              actions: write
              contents: read
            run: python3 {PREFLIGHT_SCRIPT}
          publish:
            needs: [classify, codeql-preflight]
            run: gh release create "$TAG"
        '''
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture_files(root, publisher)
        (root / publisher).write_text(good, encoding="utf-8")
        verify(root)

        # Regression for the old py/redos issue: a long rejected dependency
        # sequence and a valid multiline dependency are both handled linearly.
        long_needs = "  publish:\n    needs:\n" + "      - unrelated\n" * 10000 + "    run: echo fixture\n"
        assert not needs_preflight(long_needs)
        assert needs_preflight(long_needs.replace("    run:", "      - codeql-preflight\n    run:"))
        assert not needs_preflight("  publish:\n    needs: codeql-preflight\n    needs: unrelated\n")
        assert needs_preflight("  publish:\n    needs:\n      - classify\n      - codeql-preflight\n")
        assert not needs_preflight("  publish:\n    needs:\n      - classify\n      - unrelated\n")

        cases = {
            "missing preflight": re.sub(
                r"(?ms)^  codeql-preflight:.*?(?=^  publish:)", "", good
            ),
            "mutation not gated": good.replace(
                "needs: [classify, codeql-preflight]", "needs: classify"
            ),
            "publication before preflight": textwrap.dedent(
                f'''\
                name: release
                jobs:
                  publish:
                    needs: codeql-preflight
                    run: gh release create "$TAG"
                  codeql-preflight:
                    permissions:
                      actions: write
                    run: python3 {PREFLIGHT_SCRIPT}
                '''
            ),
            "preflight publication": good.replace(
                f"run: python3 {PREFLIGHT_SCRIPT}",
                f"run: |\n      python3 {PREFLIGHT_SCRIPT}\n      gh release create v0.0.0",
            ),
        }
        for label, source in cases.items():
            (root / publisher).write_text(source, encoding="utf-8")
            try:
                verify(root)
            except ReleaseCodeQLPolicyError:
                pass
            else:
                raise AssertionError(f"fail-open publication policy case: {label}")
    print("release CodeQL publication-order policy negative controls passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
        else:
            verify(args.root)
            print("release CodeQL prepublication policy verified")
    except (ReleaseCodeQLPolicyError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"release CodeQL policy error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
