#!/usr/bin/env python3
from __future__ import annotations

import types
import unittest

import allocator_churn


def process(pid: int, value: int) -> dict[str, object]:
    return {
        "pid": pid,
        "role": "same-process-churn",
        "rss_bytes": value,
        "pss_bytes": value - 10,
        "smaps_rollup_rss_bytes": value,
        "private_clean_bytes": 0,
        "private_dirty_bytes": value - 20,
        "shared_clean_bytes": 20,
        "shared_dirty_bytes": 0,
        "swap_bytes": 0,
        "minor_faults": value,
        "major_faults": 0,
        "cpu_time_ticks": value,
        "voluntary_context_switches": value,
        "nonvoluntary_context_switches": 0,
        "thread_count": 4,
        "fd_count": 12,
        "unavailable_reason": None,
        "smaps_rollup_unavailable_reason": None,
        "fd_count_unavailable_reason": None,
    }


def sample(phase: str, pid: int, value: int) -> dict[str, object]:
    return {"phase": phase, "process": process(pid, value)}


def runner_report(*, cycles: int = 4, native: bool = False) -> dict[str, object]:
    pid = 4242
    reports = []
    for sequence in range(1, cycles + 1):
        base = 1_000 + sequence * 100
        reports.append(
            {
                "sequence": sequence,
                "reader": "cursor" if sequence % 2 else "paging",
                "import_name": f"endurance-test-{sequence}",
                "projection_rows": allocator_churn.ROWS,
                "cleanup_deleted_rows": allocator_churn.ROWS,
                "pre_run": sample("pre-run", pid, base),
                "post_run": sample("post-run", pid, base + 300),
                "post_cleanup": sample("post-cleanup", pid, base + 100),
                "post_idle": sample("post-idle", pid, base + 50),
                "native": {
                    "post_run": f"/tmp/{sequence}/post-run/snapshot.json" if native else None,
                    "post_cleanup": f"/tmp/{sequence}/post-cleanup/snapshot.json" if native else None,
                    "post_idle": f"/tmp/{sequence}/post-idle/snapshot.json" if native else None,
                },
            }
        )
    return {
        "schema": allocator_churn.RUNNER_SCHEMA,
        "allocator": "jemalloc" if native else "system",
        "process_id": pid,
        "campaign_id": "allocchurn-test",
        "source_rows": allocator_churn.ROWS,
        "cycles": cycles,
        "cursor_cycles": cycles // 2,
        "paging_cycles": cycles // 2,
        "idle_ms": allocator_churn.DEFAULT_IDLE_MS,
        "chunk_size": allocator_churn.CHUNK_SIZE,
        "cursor_fetch_size": allocator_churn.FETCH_SIZE,
        "paging_page_size": allocator_churn.PAGE_SIZE,
        "native_telemetry_enabled": native,
        "cycle_reports": reports,
        "limitations": [],
    }


class AllocatorChurnProducerTests(unittest.TestCase):
    def test_default_geometry_is_balanced_and_bounded(self) -> None:
        args = types.SimpleNamespace(
            cycles=allocator_churn.DEFAULT_CYCLES,
            idle_ms=allocator_churn.DEFAULT_IDLE_MS,
        )
        allocator_churn.validate_geometry(args)
        self.assertEqual(args.cycles, 20)
        self.assertLessEqual(args.cycles, allocator_churn.MAX_CYCLES)

    def test_odd_cycle_count_is_rejected(self) -> None:
        args = types.SimpleNamespace(cycles=3, idle_ms=1_000)
        with self.assertRaisesRegex(ValueError, "even"):
            allocator_churn.validate_geometry(args)

    def test_valid_same_process_report_is_accepted(self) -> None:
        report = runner_report()
        validated = allocator_churn.validate_runner_report(
            report,
            allocator="system",
            cycles=4,
            idle_ms=allocator_churn.DEFAULT_IDLE_MS,
            source_rows=allocator_churn.ROWS,
            native_enabled=False,
        )
        self.assertEqual(validated["pid"], 4242)
        self.assertEqual(len(validated["phase_processes"]["post_idle"]), 4)

    def test_pid_change_fails_same_process_contract(self) -> None:
        report = runner_report()
        report["cycle_reports"][2]["post_idle"]["process"]["pid"] = 9999
        with self.assertRaisesRegex(RuntimeError, "pid changed"):
            allocator_churn.validate_runner_report(
                report,
                allocator="system",
                cycles=4,
                idle_ms=allocator_churn.DEFAULT_IDLE_MS,
                source_rows=allocator_churn.ROWS,
                native_enabled=False,
            )

    def test_projection_cardinality_mismatch_is_rejected(self) -> None:
        report = runner_report()
        report["cycle_reports"][1]["projection_rows"] -= 1
        with self.assertRaisesRegex(RuntimeError, "projection row count mismatch"):
            allocator_churn.validate_runner_report(
                report,
                allocator="system",
                cycles=4,
                idle_ms=allocator_churn.DEFAULT_IDLE_MS,
                source_rows=allocator_churn.ROWS,
                native_enabled=False,
            )

    def test_wired_native_report_requires_three_snapshots_per_cycle(self) -> None:
        report = runner_report(native=True)
        allocator_churn.validate_runner_report(
            report,
            allocator="jemalloc",
            cycles=4,
            idle_ms=allocator_churn.DEFAULT_IDLE_MS,
            source_rows=allocator_churn.ROWS,
            native_enabled=True,
        )
        report["cycle_reports"][0]["native"]["post_idle"] = None
        with self.assertRaisesRegex(RuntimeError, "native post_idle path missing"):
            allocator_churn.validate_runner_report(
                report,
                allocator="jemalloc",
                cycles=4,
                idle_ms=allocator_churn.DEFAULT_IDLE_MS,
                source_rows=allocator_churn.ROWS,
                native_enabled=True,
            )

    def test_post_idle_summary_retains_first_last_and_delta(self) -> None:
        report = runner_report()
        validated = allocator_churn.validate_runner_report(
            report,
            allocator="system",
            cycles=4,
            idle_ms=allocator_churn.DEFAULT_IDLE_MS,
            source_rows=allocator_churn.ROWS,
            native_enabled=False,
        )
        summary = allocator_churn.summarize_phases(validated["phase_processes"])
        rss = summary["post_idle"]["rss_bytes"]
        self.assertEqual(rss["observed_count"], 4)
        self.assertEqual(rss["first"], 1_150)
        self.assertEqual(rss["last"], 1_450)
        self.assertEqual(rss["delta_first_to_last"], 300)


if __name__ == "__main__":
    unittest.main()
