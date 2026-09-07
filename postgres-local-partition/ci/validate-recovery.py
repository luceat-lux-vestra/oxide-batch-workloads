#!/usr/bin/env python3
"""Campaign #93 PR2: external SIGKILL/restart and cancellation qualification."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "target" / "debug" / "postgres-local-partition"
RECOVERY = ROOT / "target" / "debug" / "postgres-local-partition-recoveryctl"
DATABASE_URL = os.environ["DATABASE_URL"]
ROWS = 1024
PARTITIONS = 128
ROWS_PER_PARTITION = ROWS // PARTITIONS
SEED = 20260908
WORKER_POINTS = (1, 8, 64)
WAIT_SECONDS = 30.0
CANCEL_PROCESS_TIMEOUT_SECONDS = 20.0
CONTROL_ENV = (
    "OXIDEBATCH_WORKLOAD_CRASH_PARTITION",
    "OXIDEBATCH_WORKLOAD_CRASH_BOUNDARY",
    "OXIDEBATCH_WORKLOAD_CRASH_MARKER",
    "OXIDEBATCH_WORKLOAD_READY_MARKER",
    "OXIDEBATCH_WORKLOAD_DRAIN_MARKER",
    "OXIDEBATCH_WORKLOAD_WORKER_HOLD_MS",
    "OXIDEBATCH_WORKLOAD_STOP_FILE",
)


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in CONTROL_ENV:
        env.pop(name, None)
    return env


def command(binary: Path, *args: str) -> list[str]:
    return [str(binary), "--database-url", DATABASE_URL, *args]


def run(binary: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command(binary, *args),
        env=clean_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command(binary, *args))}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def json_stdout(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(f"stdout was not one JSON object: {completed.stdout!r}") from error
    if not isinstance(value, dict):
        raise AssertionError("expected JSON object output")
    return value


def seed() -> None:
    run(MAIN, "seed", "--rows", str(ROWS), "--seed", str(SEED))


def inspect(run_name: str) -> dict[str, Any]:
    return json_stdout(run(RECOVERY, "inspect", "--run-name", run_name))


def recover(run_name: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(
        RECOVERY,
        "recover",
        "--run-name",
        run_name,
        "--rows",
        str(ROWS),
        "--partitions",
        str(PARTITIONS),
        check=check,
    )


def verify(run_name: str) -> dict[str, Any]:
    return json_stdout(
        run(
            MAIN,
            "verify",
            "--run-name",
            run_name,
            "--rows",
            str(ROWS),
            "--partitions",
            str(PARTITIONS),
        )
    )


def normalized_status(value: object) -> str:
    return str(value).upper()


def partition_map(observation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    partitions = observation.get("partitions")
    if not isinstance(partitions, list):
        raise AssertionError("inspection omitted partition list")
    result: dict[str, dict[str, Any]] = {}
    for partition in partitions:
        if not isinstance(partition, dict) or not isinstance(partition.get("key"), str):
            raise AssertionError("malformed partition observation")
        key = partition["key"]
        if key in result:
            raise AssertionError(f"duplicate partition observation: {key}")
        result[key] = partition
    return result


def wait_json_marker(path: Path, timeout: float = WAIT_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
            except (OSError, json.JSONDecodeError) as error:
                last_error = error
        time.sleep(0.02)
    raise AssertionError(f"marker {path} was not readable before timeout: {last_error}")


def assert_pid_gone(pid: int) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"old process {pid} still exists after SIGKILL/wait")


def spawn_run(run_name: str, workers: int, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command(
            MAIN,
            "run",
            "--run-name",
            run_name,
            "--rows",
            str(ROWS),
            "--partitions",
            str(PARTITIONS),
            "--workers",
            str(workers),
        ),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def assert_final_recovery(
    run_name: str,
    before: dict[str, Any],
    target_key: str,
    old_pid: int,
    continuation_pid: int,
) -> dict[str, Any]:
    if continuation_pid == old_pid:
        raise AssertionError("continuation reused the killed process PID")
    final_verify = verify(run_name)
    framework = final_verify.get("framework", {})
    business = final_verify.get("business", {})
    if not framework.get("terminal_state_consistent"):
        raise AssertionError("final framework state is not terminal/consistent")
    if business.get("destination_rows") != ROWS:
        raise AssertionError("final destination row count differs from source")
    if business.get("expected_destination_digest") != business.get("actual_destination_digest"):
        raise AssertionError("final destination digest differs from independently expected digest")
    if not business.get("range_ownership_complete"):
        raise AssertionError("final range ownership is incomplete")

    final = inspect(run_name)
    if normalized_status(final.get("latest_execution_status")) != "COMPLETED":
        raise AssertionError("continuation did not finish COMPLETED")
    if final.get("nonterminal_execution_count") != 0:
        raise AssertionError("stale/nonterminal execution remains after recovery")
    if len(final.get("executions", [])) != len(before.get("executions", [])) + 1:
        raise AssertionError("recovery did not create exactly one continuation execution")

    before_partitions = partition_map(before)
    final_partitions = partition_map(final)
    if set(before_partitions) != set(final_partitions) or len(final_partitions) != PARTITIONS:
        raise AssertionError("durable partition key set changed across restart")
    if any(normalized_status(partition.get("status")) != "COMPLETED" for partition in final_partitions.values()):
        raise AssertionError("not every durable partition is COMPLETED after restart")

    completed_before = {
        key: partition.get("worker_step_execution_id")
        for key, partition in before_partitions.items()
        if normalized_status(partition.get("status")) == "COMPLETED"
    }
    if not completed_before:
        raise AssertionError("crash happened before any durable completed partition existed")
    if any(worker_id is None for worker_id in completed_before.values()):
        raise AssertionError("completed pre-crash partition lacked worker execution identity")
    for key, old_worker_id in completed_before.items():
        if final_partitions[key].get("worker_step_execution_id") != old_worker_id:
            raise AssertionError(f"completed partition {key} was re-executed after restart")

    target_before = before_partitions[target_key]
    target_after = final_partitions[target_key]
    old_target_worker = target_before.get("worker_step_execution_id")
    new_target_worker = target_after.get("worker_step_execution_id")
    if old_target_worker is None or new_target_worker is None or old_target_worker == new_target_worker:
        raise AssertionError("unfinished target partition was not executed by a new worker after restart")

    return {
        "completed_partitions_reused": len(completed_before),
        "target_old_worker_step_execution_id": old_target_worker,
        "target_new_worker_step_execution_id": new_target_worker,
        "final_destination_digest_sha256": business.get("actual_destination_digest"),
        "continuation_pid": continuation_pid,
    }


def crash_case(workers: int, boundary: str, case_id: str) -> dict[str, Any]:
    seed()
    run_name = f"{case_id}-w{workers}-{boundary}"
    target_key = f"partition-{workers:04d}"
    with tempfile.TemporaryDirectory(prefix="campaign93-crash-") as directory:
        marker_path = Path(directory) / "crash.json"
        env = clean_env()
        env.update(
            {
                "OXIDEBATCH_WORKLOAD_CRASH_PARTITION": target_key,
                "OXIDEBATCH_WORKLOAD_CRASH_BOUNDARY": boundary,
                "OXIDEBATCH_WORKLOAD_CRASH_MARKER": str(marker_path),
            }
        )
        crashed = spawn_run(run_name, workers, env)
        marker = wait_json_marker(marker_path)
        if marker.get("pid") != crashed.pid:
            raise AssertionError("crash marker PID does not match the spawned Rust process")
        if marker.get("partition_key") != target_key or marker.get("boundary") != boundary:
            raise AssertionError("crash marker semantic boundary does not match the requested target")
        os.kill(crashed.pid, signal.SIGKILL)
        crashed_stdout, crashed_stderr = crashed.communicate(timeout=10)
        if crashed.returncode != -signal.SIGKILL:
            raise AssertionError(
                f"expected external SIGKILL, got {crashed.returncode}; stdout={crashed_stdout!r}; stderr={crashed_stderr!r}"
            )
        assert_pid_gone(crashed.pid)

        before = inspect(run_name)
        if normalized_status(before.get("latest_execution_status")) not in {
            "STARTING",
            "STARTED",
            "STOPPING",
            "UNKNOWN",
        }:
            raise AssertionError("killed execution is not durably nonterminal before recovery")
        before_partitions = partition_map(before)
        target = before_partitions[target_key]
        if normalized_status(target.get("status")) == "COMPLETED":
            raise AssertionError("target partition published completion before SIGKILL")
        expected_business_rows = 0 if boundary == "before-write" else ROWS_PER_PARTITION
        if target.get("business_rows") != expected_business_rows:
            raise AssertionError(
                f"target business rows at {boundary} were {target.get('business_rows')}, expected {expected_business_rows}"
            )

        recover(run_name)
        continuation = spawn_run(run_name, workers, clean_env())
        continuation_pid = continuation.pid
        continuation_stdout, continuation_stderr = continuation.communicate(timeout=WAIT_SECONDS)
        if continuation.returncode != 0:
            raise AssertionError(
                f"continuation failed ({continuation.returncode}); stdout={continuation_stdout!r}; stderr={continuation_stderr!r}"
            )
        final = assert_final_recovery(
            run_name, before, target_key, crashed.pid, continuation_pid
        )
        return {
            "workers": workers,
            "boundary": boundary,
            "killed_pid": crashed.pid,
            "pre_crash_target_business_rows": expected_business_rows,
            "replayed_target_partition": boundary == "after-business-write",
            **final,
        }


def source_mutation_case(case_id: str) -> dict[str, Any]:
    workers = 8
    boundary = "before-write"
    seed()
    run_name = f"{case_id}-source-mutation"
    target_key = f"partition-{workers:04d}"
    with tempfile.TemporaryDirectory(prefix="campaign93-mutation-") as directory:
        marker_path = Path(directory) / "crash.json"
        env = clean_env()
        env.update(
            {
                "OXIDEBATCH_WORKLOAD_CRASH_PARTITION": target_key,
                "OXIDEBATCH_WORKLOAD_CRASH_BOUNDARY": boundary,
                "OXIDEBATCH_WORKLOAD_CRASH_MARKER": str(marker_path),
            }
        )
        crashed = spawn_run(run_name, workers, env)
        marker = wait_json_marker(marker_path)
        if marker.get("pid") != crashed.pid:
            raise AssertionError("source-mutation crash marker PID mismatch")
        os.kill(crashed.pid, signal.SIGKILL)
        crashed.communicate(timeout=10)
        if crashed.returncode != -signal.SIGKILL:
            raise AssertionError("source-mutation case did not die by external SIGKILL")
        assert_pid_gone(crashed.pid)
        before = inspect(run_name)

        run(RECOVERY, "mutate-source", "--source-id", "1", "--delta", "1")
        refused = recover(run_name, check=False)
        if refused.returncode == 0:
            raise AssertionError("public recovery accepted a mutated source identity")
        if "recovery refused" not in refused.stderr:
            raise AssertionError(
                f"source-mutation recovery failed for an unexpected reason: {refused.stderr!r}"
            )
        changed = inspect(run_name)
        if changed.get("live_source_digest") == before.get("live_source_digest"):
            raise AssertionError("source mutation did not change the live source digest")

        run(RECOVERY, "mutate-source", "--source-id", "1", "--delta", "-1")
        restored = inspect(run_name)
        if restored.get("live_source_digest") != before.get("live_source_digest"):
            raise AssertionError("source restoration did not restore the original digest")
        recover(run_name)
        continuation = spawn_run(run_name, workers, clean_env())
        continuation_pid = continuation.pid
        out, err = continuation.communicate(timeout=WAIT_SECONDS)
        if continuation.returncode != 0:
            raise AssertionError(f"post-restoration continuation failed: stdout={out!r}; stderr={err!r}")
        final = assert_final_recovery(
            run_name, before, target_key, crashed.pid, continuation_pid
        )
        return {
            "workers": workers,
            "killed_pid": crashed.pid,
            "mutated_source_recovery_refused": True,
            "original_source_digest_sha256": before.get("live_source_digest"),
            **final,
        }


def cancellation_case(case_id: str) -> dict[str, Any]:
    workers = 64
    seed()
    run_name = f"{case_id}-cancel-w{workers}"
    with tempfile.TemporaryDirectory(prefix="campaign93-cancel-") as directory:
        ready_path = Path(directory) / "ready.json"
        stop_path = Path(directory) / "stop.request"
        env = clean_env()
        env.update(
            {
                "OXIDEBATCH_WORKLOAD_READY_MARKER": str(ready_path),
                "OXIDEBATCH_WORKLOAD_WORKER_HOLD_MS": "5000",
                "OXIDEBATCH_WORKLOAD_STOP_FILE": str(stop_path),
            }
        )
        process = spawn_run(run_name, workers, env)
        ready = wait_json_marker(ready_path)
        if ready.get("pid") != process.pid:
            raise AssertionError("cancellation ready marker PID mismatch")
        if ready.get("active_workers") != workers or ready.get("peak_workers") != workers:
            raise AssertionError(f"cancellation did not reach full {workers}-worker occupancy: {ready}")

        requested_at = time.monotonic()
        stop_path.write_text("stop\n", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=CANCEL_PROCESS_TIMEOUT_SECONDS)
        latency_ms = round((time.monotonic() - requested_at) * 1000.0, 3)
        if process.returncode == 0:
            raise AssertionError("cancelled run unexpectedly satisfied completed-run verification")
        try:
            run_observation = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise AssertionError(
                f"cancelled run did not emit join/final-state observation: stdout={stdout!r}; stderr={stderr!r}"
            ) from error
        if run_observation.get("peak_active_workers") != workers:
            raise AssertionError("cancelled run did not retain the proven peak worker occupancy")
        if run_observation.get("active_workers_after_join") != 0:
            raise AssertionError("launcher returned while owned workers were still active")
        if run_observation.get("launch_completed") is not False:
            raise AssertionError("cancelled launch was misclassified as completed")

        durable = inspect(run_name)
        if normalized_status(durable.get("latest_execution_status")) != "STOPPED":
            raise AssertionError(f"cancelled job did not durably end STOPPED: {durable.get('latest_execution_status')}")
        if normalized_status(durable.get("latest_parent_status")) != "STOPPED":
            raise AssertionError(f"cancelled partition parent did not durably end STOPPED: {durable.get('latest_parent_status')}")
        if durable.get("nonterminal_execution_count") != 0:
            raise AssertionError("cancelled run left a stale/nonterminal execution")
        business_rows = sum(
            int(partition.get("business_rows", 0)) for partition in partition_map(durable).values()
        )
        if business_rows != 0:
            raise AssertionError("cancellation hold allowed business writes before the external stop")

        return {
            "workers": workers,
            "pid": process.pid,
            "external_stop_to_process_terminal_ms": latency_ms,
            "peak_active_workers": run_observation.get("peak_active_workers"),
            "active_workers_after_join": run_observation.get("active_workers_after_join"),
            "durable_job_status": durable.get("latest_execution_status"),
            "durable_parent_status": durable.get("latest_parent_status"),
            "business_rows_before_terminal": business_rows,
            "timeout_is_harness_liveness_guard_not_product_sla": CANCEL_PROCESS_TIMEOUT_SECONDS,
        }


def main() -> int:
    if not MAIN.is_file() or not RECOVERY.is_file():
        raise AssertionError("expected debug binaries are missing; run cargo build --locked --all-targets first")

    identity = f"ci-{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '0')}-{os.getpid()}"
    crash_results = [
        crash_case(workers, boundary, identity)
        for workers in WORKER_POINTS
        for boundary in ("before-write", "after-business-write")
    ]
    mutation = source_mutation_case(identity)
    cancellation = cancellation_case(identity)

    print(
        json.dumps(
            {
                "campaign": 93,
                "validation_subject": "oxide-batch =0.6.0",
                "classification": "recovery/cancellation qualification; no performance claim",
                "rows": ROWS,
                "partitions": PARTITIONS,
                "rows_per_partition": ROWS_PER_PARTITION,
                "crash_cases": crash_results,
                "source_mutation": mutation,
                "cancellation": cancellation,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # qualification harness: surface one actionable terminal error
        print(f"recovery qualification failed: {error}", file=sys.stderr)
        raise
