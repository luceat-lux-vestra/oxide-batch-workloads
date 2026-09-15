#!/usr/bin/env python3
import copy
import importlib.util
import json
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("verify-track-f-retained-evidence.py")
SPEC = importlib.util.spec_from_file_location("track_f_retained_verifier", MODULE_PATH)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
VALIDATION_DIR = Path(__file__).resolve().parent


def artifact(name: str) -> dict:
    return json.loads((VALIDATION_DIR / name).read_text(encoding="utf-8"))


class TrackFVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.interop = artifact("process-interop-run.json")
        self.closure = artifact("process-interop-closure-run.json")
        self.identity = artifact("process-interop-identity-run.json")

    @staticmethod
    def violations_for(fn, value):
        violations = []
        fn(value, violations)
        return violations

    def test_track_f_artifacts_are_internally_consistent(self):
        self.assertEqual(self.violations_for(verifier.verify_process_interop, self.interop), [])
        self.assertEqual(self.violations_for(verifier.verify_process_closure, self.closure), [])
        self.assertEqual(self.violations_for(verifier.verify_process_identity, self.identity), [])

    def test_graceful_sigterm_claim_is_rejected(self):
        value = copy.deepcopy(self.interop)
        value["capability_gaps"]["graceful_sigterm"] = "SUPPORTED"
        self.assertTrue(any("graceful SIGTERM" in v for v in self.violations_for(verifier.verify_process_interop, value)))

    def test_stale_step_gap_cannot_be_papered_over(self):
        value = copy.deepcopy(self.closure)
        value["public_recovery"]["active_step_executions_after_recovery"] = 0
        value["public_recovery"]["step_lifecycle_closure"] = "SUPPORTED"
        violations = self.violations_for(verifier.verify_process_closure, value)
        self.assertTrue(any("stale-StepExecution GAP" in v for v in violations), violations)

    def test_same_pid_continuation_is_rejected(self):
        value = copy.deepcopy(self.closure)
        value["continuation"]["workload_pid"] = value["sigterm"]["workload_pid"]
        self.assertTrue(any("genuinely new" in v for v in self.violations_for(verifier.verify_process_closure, value)))

    def test_duplicate_retry_creating_second_execution_is_rejected(self):
        value = copy.deepcopy(self.interop)
        value["active_duplicate_lost_response_retry"]["job_executions_after_retry"] = 2
        self.assertTrue(any("duplicate/lost-response" in v for v in self.violations_for(verifier.verify_process_interop, value)))

    def test_recovery_job_instance_identity_drift_is_rejected(self):
        value = copy.deepcopy(self.identity)
        value["recovery_continuation"]["job_instance_id"] = "999"
        self.assertTrue(any("preserve JobInstance" in v for v in self.violations_for(verifier.verify_process_identity, value)))

    def test_producer_checkout_drift_is_rejected(self):
        value = copy.deepcopy(self.identity)
        value["producer_checkout"] = "0" * 40
        self.assertTrue(any("producer_checkout" in v for v in self.violations_for(verifier.verify_process_identity, value)))


if __name__ == "__main__":
    unittest.main()
