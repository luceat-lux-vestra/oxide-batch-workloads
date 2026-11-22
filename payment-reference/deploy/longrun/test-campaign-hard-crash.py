#!/usr/bin/env python3
"""Offline tests for #318 deterministic hard-crash supervision."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

import campaign_hard_crash as crash


class CampaignHardCrashTests(unittest.TestCase):
    def test_schedule_is_derived_from_frozen_day_plan(self) -> None:
        self.assertEqual(
            crash.scheduled_spec({"logical_day_sequence": 3}),
            {
                "scheduled_hard_crash": "reconciliation-during-write",
                "owner": "reconciliation",
                "boundary": "during-write",
                "marker_label": "chunk-during-write",
                "recovery_action": "recover-run-finalize",
            },
        )
        self.assertEqual(
            crash.scheduled_spec({"logical_day_sequence": 6})["marker_label"],
            "chunk-after-business-commit",
        )
        recovery = crash.scheduled_spec({"logical_day_sequence": 8})
        self.assertEqual(recovery["owner"], "recovery")
        self.assertEqual(
            recovery["boundary"],
            "after-external-convergence-before-local-commit",
        )
        self.assertEqual(
            crash.scheduled_spec({"logical_day_sequence": 10})["marker_label"],
            "finalize-after-commit",
        )
        self.assertIsNone(crash.scheduled_spec({"logical_day_sequence": 1}))

    def test_supervisor_kills_exact_child_only_after_exact_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            label = "after-external-convergence-before-local-commit"
            target = "candidate-1"
            code = (
                "import os,pathlib,sys,time;"
                "pathlib.Path(sys.argv[1]).write_text("
                "f'{os.getpid()}\\n{sys.argv[2]}\\n{sys.argv[3]}\\n',encoding='utf-8');"
                "time.sleep(60)"
            )
            proof = crash.kill_at_marker(
                command=[sys.executable, "-c", code, str(marker), label, target],
                marker_path=marker,
                expected_marker_label=label,
                expected_marker_target=target,
                marker_timeout_seconds=5,
                poll_interval_seconds=0.01,
            )
            self.assertTrue(proof["semantic_marker_observed"])
            self.assertEqual(proof["signal"], 9)
            self.assertTrue(proof["process_gone_after_kill"])
            self.assertEqual(proof["marker_target"], target)

    def test_marker_identity_mismatch_blocks_and_cleans_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            code = (
                "import os,pathlib,sys,time;"
                "pathlib.Path(sys.argv[1]).write_text("
                "f'{os.getpid()}\\nwrong-marker\\n',encoding='utf-8');"
                "time.sleep(60)"
            )
            with self.assertRaisesRegex(crash.HardCrashError, "label mismatch"):
                crash.kill_at_marker(
                    command=[sys.executable, "-c", code, str(marker)],
                    marker_path=marker,
                    expected_marker_label="expected-marker",
                    expected_marker_target=None,
                    marker_timeout_seconds=5,
                    poll_interval_seconds=0.01,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
