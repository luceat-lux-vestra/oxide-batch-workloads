#!/usr/bin/env python3
"""Regression tests for PR qualification scope classification."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).with_name("detect-pr-qualification-scope.py")
SPEC = importlib.util.spec_from_file_location("qualification_scope", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def expect(paths: list[str], **expected: bool) -> None:
    actual = MODULE.classify(paths)
    baseline = {
        "jberet_clean": False,
        "jberet_crash": False,
        "pr4_semantic": False,
        "governance": False,
    }
    baseline.update(expected)
    assert actual == baseline, (paths, baseline, actual)


def main() -> int:
    expect(["README.md"])
    expect(
        ["postgres-postgres/ci/validate-jberet-clean"],
        jberet_clean=True,
        pr4_semantic=True,
    )
    expect(
        ["postgres-postgres/src/main.rs"],
        jberet_crash=True,
        pr4_semantic=True,
    )
    expect(
        [".github/workflows/jberet-crash.yml"],
        jberet_crash=True,
        governance=True,
    )
    expect(
        [".github/workflows/benchmark-postgres-postgres-four-way.yml"],
        pr4_semantic=True,
        governance=True,
    )
    expect(["workloads.json"], governance=True)
    expect([".github/ISSUE_TEMPLATE/config.yml"], governance=True)
    expect([".github/workflows/ci.yml"], governance=True)
    print("qualification scope classifier tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
