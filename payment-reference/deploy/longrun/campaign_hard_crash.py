#!/usr/bin/env python3
"""Deterministic external-SIGKILL supervisor for #318 scheduled hard crashes.

The workload owns the semantic pause marker.  This module owns only process
supervision: start the explicit command, wait for the exact marker identity,
send SIGKILL to that exact child, reap it, and return the primitive proof.
Recovery/reconciliation verifiers remain the acceptance authorities.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

import campaign_core as core
import campaign_day_reconciliation as reconciliation

SCHEMA = "oxide-batch-workloads.payment-reference-hard-crash-supervision"
SCHEMA_VERSION = 1


class HardCrashError(core.EvidenceError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HardCrashError(message)


def scheduled_spec(plan: dict[str, Any]) -> dict[str, Any] | None:
    sequence = plan.get("logical_day_sequence")
    require(type(sequence) is int and sequence > 0, "hard-crash day sequence invalid")
    day = core.plan_day(sequence)
    scheduled = day["scheduled_hard_crash"]
    if scheduled is None:
        return None

    if scheduled in reconciliation.RECONCILIATION_CRASHES:
        boundary, recovery_action = reconciliation.RECONCILIATION_CRASHES[scheduled]
        marker_label = (
            f"finalize-{boundary}"
            if recovery_action == "finalize-replay"
            else f"chunk-{boundary}"
        )
        return {
            "scheduled_hard_crash": scheduled,
            "owner": "reconciliation",
            "boundary": boundary,
            "marker_label": marker_label,
            "recovery_action": recovery_action,
        }

    prefix = "recovery-"
    require(scheduled.startswith(prefix), "unknown scheduled hard-crash authority")
    boundary = scheduled[len(prefix):]
    require(boundary, "recovery crash boundary missing")
    return {
        "scheduled_hard_crash": scheduled,
        "owner": "recovery",
        "boundary": boundary,
        "marker_label": boundary,
        "recovery_action": "recover",
    }


def _marker(
    path: Path,
    *,
    expected_target: str | None,
) -> tuple[int, str, str | None] | None:
    """Return a complete marker, or None while publication is still incomplete."""
    raw = path.read_text(encoding="utf-8")
    expected_lines = 3 if expected_target is not None else 2

    # Marker writers currently publish with ordinary file writes. File creation
    # can therefore become visible before the full newline-terminated payload.
    # Treat only structurally incomplete publication as transient; once the
    # expected shape is fully present, all identity validation remains strict.
    if not raw.endswith("\n"):
        return None
    lines = raw.splitlines()
    if len(lines) < expected_lines:
        return None

    require(len(lines) == expected_lines, "hard-crash marker line count mismatch")
    require(lines[0].isdigit(), "hard-crash marker PID invalid")
    pid = int(lines[0])
    require(pid > 1, "hard-crash marker PID invalid")
    require(lines[1], "hard-crash marker label missing")
    target = lines[2] if expected_target is not None else None
    require(target is None or target != "", "hard-crash marker target blank")
    return pid, lines[1], target


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def kill_at_marker(
    *,
    command: Sequence[str],
    marker_path: Path,
    expected_marker_label: str,
    expected_marker_target: str | None,
    marker_timeout_seconds: float,
    poll_interval_seconds: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    require(
        isinstance(command, Sequence)
        and not isinstance(command, (str, bytes))
        and len(command) > 0
        and all(isinstance(part, str) and part for part in command),
        "hard-crash command must be a non-empty argv",
    )
    require(isinstance(expected_marker_label, str) and expected_marker_label, "expected marker label missing")
    require(
        expected_marker_target is None
        or (isinstance(expected_marker_target, str) and expected_marker_target),
        "expected marker target invalid",
    )
    require(
        isinstance(marker_timeout_seconds, (int, float))
        and not isinstance(marker_timeout_seconds, bool)
        and marker_timeout_seconds > 0,
        "marker timeout must be positive",
    )
    require(
        isinstance(poll_interval_seconds, (int, float))
        and not isinstance(poll_interval_seconds, bool)
        and poll_interval_seconds > 0
        and poll_interval_seconds <= marker_timeout_seconds,
        "marker poll interval invalid",
    )
    require(not marker_path.exists(), "hard-crash marker path already exists")
    require(marker_path.parent.is_dir() and not marker_path.parent.is_symlink(), "hard-crash marker parent unsafe")

    process = subprocess.Popen(
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
    )
    deadline = time.monotonic() + marker_timeout_seconds
    marker: tuple[int, str, str | None] | None = None
    try:
        while marker is None:
            code = process.poll()
            if code is not None:
                raise HardCrashError(
                    f"hard-crash target exited before semantic marker: {code}"
                )
            if marker_path.is_file() and not marker_path.is_symlink():
                marker = _marker(
                    marker_path,
                    expected_target=expected_marker_target,
                )
                if marker is not None:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HardCrashError("hard-crash semantic marker timeout")
            time.sleep(min(poll_interval_seconds, remaining))

        marker_pid, marker_label, marker_target = marker
        require(marker_pid == process.pid, "hard-crash marker PID differs from supervised child")
        require(marker_label == expected_marker_label, "hard-crash marker label mismatch")
        require(marker_target == expected_marker_target, "hard-crash marker target mismatch")

        os.kill(process.pid, signal.SIGKILL)
        code = process.wait()
        require(code == -signal.SIGKILL, "hard-crash child did not terminate by SIGKILL")
        gone = _gone(process.pid)
        require(gone, "hard-crash child still exists after SIGKILL/reap")
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "semantic_marker_observed": True,
            "marker_label": marker_label,
            "marker_target": marker_target,
            "signal": signal.SIGKILL,
            "killed_pid": process.pid,
            "process_gone_after_kill": gone,
        }
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 deterministic hard-crash supervisor")
    sub = parser.add_subparsers(dest="command", required=True)
    planned = sub.add_parser("plan")
    planned.add_argument("--campaign-id", required=True)
    planned.add_argument("--day", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            require(core.CAMPAIGN_ID.fullmatch(args.campaign_id) is not None, "campaign id unsafe")
            plan = {
                "campaign_id": args.campaign_id,
                "logical_day_sequence": args.day,
            }
            print(json.dumps({
                "decision": "PLAN_ONLY",
                "scheduled_crash": scheduled_spec(plan),
                "no_process_mutation": True,
            }, sort_keys=True))
            return 0
        raise HardCrashError("unsupported command")
    except (HardCrashError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
