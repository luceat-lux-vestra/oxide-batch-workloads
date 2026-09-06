#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JAVA_REL = Path("benchmark/java/raw-jdbc/src/main/java/io/oxidebatch/workloads/postgres/benchmark/rawjdbc/RawJdbcMain.java")
HARNESS_REL = Path("ci/validate-raw-jdbc-crash-recovery")


class ValidationError(RuntimeError):
    pass


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise ValidationError(f"{label}: missing required evidence: {needle!r}")


def forbid_regex(text: str, pattern: str, label: str) -> None:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if match:
        raise ValidationError(f"{label}: forbidden pattern matched: {match.group(0)!r}")


def validate_texts(java: str, harness: str) -> None:
    for pattern in (
        r"\bSystem\.exit\s*\(",
        r"\bRuntime\.getRuntime\(\)\.halt\s*\(",
        r"\bProcessHandle\.current\(\)\.(?:destroy|destroyForcibly)\s*\(",
        r"\b(?:kill|abort)\s*\(",
    ):
        forbid_regex(java, pattern, "RawJdbcMain")

    for needle in (
        "ProcessHandle.current().pid()",
        "StandardOpenOption.CREATE_NEW",
        "new CountDownLatch(1).await()",
        'pauseIfRequested(config, checkpoint.committedChunks(), "before-commit")',
        'pauseIfRequested(config, checkpoint.committedChunks(), "after-commit")',
        'Set.of("before-commit", "after-commit")',
        "--pause-at-chunk, --pause-phase, and --pause-marker must be supplied together",
        "--fail-after-chunk cannot be combined with external-kill pause controls",
    ):
        require(java, needle, "RawJdbcMain")

    for needle in (
        'local expected="pid=${expected_pid} phase=${phase} chunk=${chunk}"',
        '[[ "$actual" == "$expected" ]]',
        'kill -0 "$expected_pid"',
        'kill -KILL "$pid"',
        '[[ "$status" -ne 137 ]]',
        '[[ "$pid" == "$CRASHED_PID" ]]',
        'timeout --signal=KILL 60s',
        '[[ "$status" -ne 124 && "$status" -ne 137 ]]',
        "source digest changed for existing raw Java execution identity",
        "cursor 50 before-commit 200 2",
        "cursor 50 after-commit 300 3",
        "paging 60 before-commit 200 2",
        "paging 60 after-commit 300 3",
        "postgres-postgres -- verify",
    ):
        require(harness, needle, "raw JDBC crash harness")


def main() -> int:
    java = (ROOT / JAVA_REL).read_text(encoding="utf-8")
    harness = (ROOT / HARNESS_REL).read_text(encoding="utf-8")
    validate_texts(java, harness)
    print("raw-jdbc crash policy ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as exc:
        print(f"raw-jdbc crash policy violation: {exc}", file=sys.stderr)
        raise SystemExit(1)
