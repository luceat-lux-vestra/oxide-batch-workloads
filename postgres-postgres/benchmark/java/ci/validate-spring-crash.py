#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JAVA_REL = Path("benchmark/java/spring-batch/src/main/java/io/oxidebatch/workloads/postgres/benchmark/springbatch/SpringBatchMain.java")
HARNESS_REL = Path("ci/validate-spring-crash-recovery")
VALIDATE_REL = Path("ci/validate")
README_REL = Path("benchmark/java/README.md")


class ValidationError(RuntimeError):
    pass


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise ValidationError(f"{label}: missing required evidence: {needle!r}")


def forbid_regex(text: str, pattern: str, label: str) -> None:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if match:
        raise ValidationError(f"{label}: forbidden pattern matched: {match.group(0)!r}")


def validate_texts(java: str, harness: str, validate: str, readme: str) -> None:
    for pattern in (
        r"\bSystem\.exit\s*\(",
        r"\bRuntime\.getRuntime\(\)\.halt\s*\(",
        r"\bProcessHandle\.current\(\)\.(?:destroy|destroyForcibly)\s*\(",
        r"\b(?:kill|abort)\s*\(",
    ):
        forbid_regex(java, pattern, "SpringBatchMain")

    require(java, 'case "recover" -> recover(', "SpringBatchMain")
    require(java, "operator.recover(execution)", "SpringBatchMain")
    require(java, "jobRepository.getJobInstances(JOB_NAME", "SpringBatchMain")
    require(java, "jobRepository.getLastJobExecution(instance)", "SpringBatchMain")
    require(java, 'parameters.getString("import_name")', "SpringBatchMain")
    require(java, 'parameters.getString("reader_mode")', "SpringBatchMain")
    require(java, 'parameters.getString("definition_revision")', "SpringBatchMain")
    require(java, 'getString("source_digest")', "SpringBatchMain")
    require(java, "source digest changed for existing logical Spring execution", "SpringBatchMain")

    require(java, "TransactionSynchronizationManager.registerSynchronization", "SpringBatchMain")
    require(java, "public void afterCommit()", "SpringBatchMain")
    forbid_regex(java, r"\bafterChunk\s*\(", "SpringBatchMain")

    require(java, "ProcessHandle.current().pid()", "SpringBatchMain")
    require(java, "StandardOpenOption.CREATE_NEW", "SpringBatchMain")
    require(java, "new CountDownLatch(1).await()", "SpringBatchMain")
    require(java, '"before-commit"', "SpringBatchMain")
    require(java, '"after-commit"', "SpringBatchMain")

    forbid_regex(
        java,
        r'(?s)"[^"]*\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+spring_batch\.',
        "SpringBatchMain",
    )

    require(harness, 'kill -KILL "$pid"', "crash harness")
    require(harness, '[[ "$status" -ne 137 ]]', "crash harness")
    require(harness, 'marker mismatch', "crash harness")
    require(harness, '[[ "$pid" == "$CRASHED_PID" ]]', "crash harness")
    require(harness, 'recover \\', "crash harness")
    require(harness, 'run \\', "crash harness")
    require(harness, "source_customer SET balance_cents = balance_cents + 1", "crash harness")
    require(harness, "expect_source_mutation_rejection", "crash harness")
    require(harness, '[[ "$status" -eq 124 || "$status" -eq 137 ]]', "crash harness")
    require(harness, "timed out instead of proving source-mutation rejection", "crash harness")
    require(harness, "source digest changed for existing logical Spring execution", "crash harness")
    require(harness, "failed for an unproven reason; expected source-digest rejection", "crash harness")
    require(harness, "logical_instance_count_sql", "crash harness")
    require(harness, "durable_write_count_sql", "crash harness")
    require(harness, "cursor 50 before-commit 200", "crash harness")
    require(harness, "cursor 50 after-commit 300", "crash harness")
    require(harness, "paging 60 before-commit 200", "crash harness")
    require(harness, "paging 60 after-commit 300", "crash harness")
    require(harness, "postgres-postgres -- verify", "crash harness")

    require(validate, "./ci/validate-spring-crash-recovery", "ci/validate")
    require(readme, "JobOperator.recover(JobExecution)", "README")
    require(readme, "afterCommit", "README")
    require(readme, "Four-way measurement starts only in PR4", "README")


def load(root: Path) -> tuple[str, str, str, str]:
    return (
        (root / JAVA_REL).read_text(encoding="utf-8"),
        (root / HARNESS_REL).read_text(encoding="utf-8"),
        (root / VALIDATE_REL).read_text(encoding="utf-8"),
        (root / README_REL).read_text(encoding="utf-8"),
    )


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    try:
        validate_texts(*load(root))
    except (OSError, ValidationError) as error:
        print(f"spring-crash-policy: FAIL: {error}", file=sys.stderr)
        return 1
    print("spring-crash-policy ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
