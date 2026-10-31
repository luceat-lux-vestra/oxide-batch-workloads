#!/usr/bin/env python3
"""Classify PR file changes into maintained qualification workflow scopes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable


def _under(path: str, prefix: str) -> bool:
    return path.startswith(prefix)


def _workflow_file(path: str) -> bool:
    prefix = ".github/workflows/"
    if not path.startswith(prefix) or not path.endswith(".yml"):
        return False
    return "/" not in path[len(prefix) :]


def classify(paths: Iterable[str]) -> dict[str, bool]:
    normalized = {path.strip() for path in paths if path.strip()}

    def any_path(predicate) -> bool:
        return any(predicate(path) for path in normalized)

    jberet_clean = any_path(
        lambda path: _under(path, "postgres-postgres/benchmark/java/")
        or path
        in {
            "postgres-postgres/ci/validate-jberet-clean",
            "postgres-postgres/ci/validate-supply-chain",
            ".github/workflows/jberet-clean.yml",
        }
    )

    jberet_crash = any_path(
        lambda path: path == ".github/workflows/jberet-crash.yml"
        or _under(path, "postgres-postgres/benchmark/java/jberet/src/test/")
        or path
        in {
            "postgres-postgres/benchmark/java/ci/validate-jberet-crash.py",
            "postgres-postgres/benchmark/java/ci/test-validate-jberet-crash.py",
            "postgres-postgres/ci/validate-jberet-crash-recovery",
        }
        or _under(path, "postgres-postgres/benchmark/java/jberet/src/main/")
        or path
        in {
            "postgres-postgres/benchmark/java/jberet/pom.xml",
            "postgres-postgres/Cargo.toml",
            "postgres-postgres/Cargo.lock",
            "postgres-postgres/docker-compose.yml",
        }
        or _under(path, "postgres-postgres/src/")
        or _under(path, "postgres-postgres/migrations/")
    )

    pr4_semantic = any_path(
        lambda path: _under(path, "postgres-postgres/")
        or path
        in {
            ".github/workflows/pr4-four-way-semantic.yml",
            ".github/workflows/benchmark-postgres-postgres-four-way.yml",
        }
    )

    governance_exact = {
        "deny.toml",
        "workloads.json",
        ".github/labels.json",
        ".github/dependabot.yml",
        ".github/repository-settings-policy.json",
        ".github/scripts/audit_issue.py",
        ".github/scripts/discover-supply-chain-workloads.py",
        ".github/scripts/validate-supply-chain.py",
        ".github/scripts/run-hardening-drift-audit.py",
        ".github/scripts/report-hardening-drift-audit.py",
        ".github/scripts/run-scheduled-supply-chain-audit.py",
        ".github/scripts/report-supply-chain-audit.py",
        ".github/scripts/reconcile-labels.py",
        ".github/scripts/validate-label-taxonomy.py",
        ".github/scripts/validate-workflow-security.py",
        ".github/scripts/test-hardening-drift-audit.py",
        ".github/scripts/test-cache-producer-guards.py",
        ".github/scripts/test-scheduled-supply-chain-audit.py",
        ".github/scripts/test-repository-settings-policy.py",
        ".github/scripts/test-reconcile-labels.py",
        ".github/scripts/test-label-taxonomy.py",
    }
    governance = any_path(
        lambda path: path in governance_exact
        or _under(path, ".github/ISSUE_TEMPLATE/")
        or _workflow_file(path)
    )

    return {
        "jberet_clean": jberet_clean,
        "jberet_crash": jberet_crash,
        "pr4_semantic": pr4_semantic,
        "governance": governance,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files-from", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    result = classify(args.files_from.read_text(encoding="utf-8").splitlines())
    print(json.dumps(result, sort_keys=True))

    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, enabled in result.items():
                output.write(f"{key}={'true' if enabled else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
