#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("spring_crash_validator", HERE / "validate-spring-crash.py")
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


BASE_JAVA = r'''
case "recover" -> recover(
operator.recover(execution)
jobRepository.getJobInstances(JOB_NAME
jobRepository.getLastJobExecution(instance)
parameters.getString("import_name")
parameters.getString("reader_mode")
parameters.getString("definition_revision")
getString("source_digest")
source digest changed for existing logical Spring execution
TransactionSynchronizationManager.registerSynchronization
public void afterCommit()
ProcessHandle.current().pid()
StandardOpenOption.CREATE_NEW
new CountDownLatch(1).await()
"before-commit"
"after-commit"
'''

BASE_HARNESS = r'''
kill -KILL "$pid"
[[ "$status" -ne 137 ]]
marker mismatch
[[ "$pid" == "$CRASHED_PID" ]]
recover \
run \
source_customer SET balance_cents = balance_cents + 1
expect_source_mutation_rejection
[[ "$status" -eq 124 || "$status" -eq 137 ]]
timed out instead of proving source-mutation rejection
source digest changed for existing logical Spring execution
failed for an unproven reason; expected source-digest rejection
logical_instance_count_sql
durable_write_count_sql
cursor 50 before-commit 200
cursor 50 after-commit 300
paging 60 before-commit 200
paging 60 after-commit 300
postgres-postgres -- verify
'''

BASE_VALIDATE = "./ci/validate-spring-crash-recovery\n"
BASE_README = "JobOperator.recover(JobExecution)\nafterCommit\nFour-way measurement starts only in PR4\n"


def expect_fail(name: str, java: str = BASE_JAVA, harness: str = BASE_HARNESS,
                validate: str = BASE_VALIDATE, readme: str = BASE_README) -> None:
    try:
        validator.validate_texts(java, harness, validate, readme)
    except validator.ValidationError:
        print(f"ok - {name}")
        return
    raise AssertionError(f"validator accepted adversarial mutation: {name}")


cases = [
    ("self exit", dict(java=BASE_JAVA + "\nSystem.exit(1);\n")),
    ("self halt", dict(java=BASE_JAVA + "\nRuntime.getRuntime().halt(1);\n")),
    ("missing public recover", dict(java=BASE_JAVA.replace("operator.recover(execution)", "repository.update(execution)"))),
    ("missing history paging", dict(java=BASE_JAVA.replace("jobRepository.getJobInstances(JOB_NAME", "jobRepository.findJobInstances(JOB_NAME"))),
    ("afterChunk substitution", dict(java=BASE_JAVA.replace("public void afterCommit()", "public void afterChunk()"))),
    ("non-exclusive marker", dict(java=BASE_JAVA.replace("StandardOpenOption.CREATE_NEW", "StandardOpenOption.CREATE"))),
    ("parent uses TERM", dict(harness=BASE_HARNESS.replace('kill -KILL "$pid"', 'kill -TERM "$pid"'))),
    ("missing 137 proof", dict(harness=BASE_HARNESS.replace('[[ "$status" -ne 137 ]]', '[[ "$status" -ne 1 ]]'))),
    ("missing pid identity", dict(harness=BASE_HARNESS.replace('[[ "$pid" == "$CRASHED_PID" ]]', '[[ "$pid" == "$pid" ]]'))),
    ("missing mutation timeout guard", dict(harness=BASE_HARNESS.replace('[[ "$status" -eq 124 || "$status" -eq 137 ]]', '[[ "$status" -eq 999 ]]'))),
    ("missing semantic mutation proof", dict(harness=BASE_HARNESS.replace("failed for an unproven reason; expected source-digest rejection", "accept any nonzero"))),
    ("missing runtime hook", dict(validate="# no crash hook\n")),
]

for name, mutation in cases:
    expect_fail(name, **mutation)

validator.validate_texts(BASE_JAVA, BASE_HARNESS, BASE_VALIDATE, BASE_README)
print(f"{len(cases)}/{len(cases)} adversarial spring-crash policy tests passed")
