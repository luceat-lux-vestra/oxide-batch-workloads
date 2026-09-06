#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("raw_jdbc_crash_validator", HERE / "validate-raw-jdbc-crash.py")
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)

BASE_JAVA = r'''
ProcessHandle.current().pid()
StandardOpenOption.CREATE_NEW
new CountDownLatch(1).await()
pauseIfRequested(config, checkpoint.committedChunks(), "before-commit")
pauseIfRequested(config, checkpoint.committedChunks(), "after-commit")
Set.of("before-commit", "after-commit")
--pause-at-chunk, --pause-phase, and --pause-marker must be supplied together
--fail-after-chunk cannot be combined with external-kill pause controls
'''
BASE_HARNESS = r'''
local expected="pid=${expected_pid} phase=${phase} chunk=${chunk}"
[[ "$actual" == "$expected" ]]
kill -0 "$expected_pid"
kill -KILL "$pid"
[[ "$status" -ne 137 ]]
[[ "$pid" == "$CRASHED_PID" ]]
timeout --signal=KILL 60s
[[ "$status" -ne 124 && "$status" -ne 137 ]]
source digest changed for existing raw Java execution identity
cursor 50 before-commit 200 2
cursor 50 after-commit 300 3
paging 60 before-commit 200 2
paging 60 after-commit 300 3
postgres-postgres -- verify
'''


def expect_fail(name: str, java: str = BASE_JAVA, harness: str = BASE_HARNESS) -> None:
    try:
        validator.validate_texts(java, harness)
    except validator.ValidationError:
        print(f"ok - {name}")
        return
    raise AssertionError(f"validator accepted adversarial mutation: {name}")


cases = [
    ("self exit", dict(java=BASE_JAVA + "\nSystem.exit(1);\n")),
    ("self halt", dict(java=BASE_JAVA + "\nRuntime.getRuntime().halt(1);\n")),
    ("nonexclusive marker", dict(java=BASE_JAVA.replace("StandardOpenOption.CREATE_NEW", "StandardOpenOption.CREATE"))),
    ("missing passive wait", dict(java=BASE_JAVA.replace("new CountDownLatch(1).await()", "Thread.sleep(1)"))),
    ("missing before commit", dict(java=BASE_JAVA.replace('pauseIfRequested(config, checkpoint.committedChunks(), "before-commit")', "missing"))),
    ("missing after commit", dict(java=BASE_JAVA.replace('pauseIfRequested(config, checkpoint.committedChunks(), "after-commit")', "missing"))),
    ("marker ignores pid", dict(harness=BASE_HARNESS.replace('local expected="pid=${expected_pid} phase=${phase} chunk=${chunk}"', 'local expected="ready"'))),
    ("missing live process check", dict(harness=BASE_HARNESS.replace('kill -0 "$expected_pid"', 'true'))),
    ("parent uses TERM", dict(harness=BASE_HARNESS.replace('kill -KILL "$pid"', 'kill -TERM "$pid"'))),
    ("missing 137 proof", dict(harness=BASE_HARNESS.replace('[[ "$status" -ne 137 ]]', '[[ "$status" -ne 1 ]]'))),
    ("missing pid identity", dict(harness=BASE_HARNESS.replace('[[ "$pid" == "$CRASHED_PID" ]]', '[[ "$pid" == "$pid" ]]'))),
    ("mutation timeout unbounded", dict(harness=BASE_HARNESS.replace('timeout --signal=KILL 60s', 'java'))),
    ("mutation timeout accepted", dict(harness=BASE_HARNESS.replace('[[ "$status" -ne 124 && "$status" -ne 137 ]]', 'true'))),
    ("missing paging after", dict(harness=BASE_HARNESS.replace("paging 60 after-commit 300 3", "missing"))),
]

for name, mutation in cases:
    expect_fail(name, **mutation)

validator.validate_texts(BASE_JAVA, BASE_HARNESS)
print(f"{len(cases)}/{len(cases)} adversarial raw-jdbc crash policy tests passed")
