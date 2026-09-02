#!/usr/bin/env python3

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("aggregate_verdict.py")
SPEC = importlib.util.spec_from_file_location("aggregate_verdict", MODULE_PATH)
assert SPEC and SPEC.loader
agg = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agg  # dataclasses needs the module registered before exec
SPEC.loader.exec_module(agg)


def exp(name: str, outcome: str = "validated") -> agg.ExpectedShard:
    return agg.ExpectedShard(name=name, expected_outcome=outcome)


def ok(workload: str, stage: str = "ci", outcome: str = "validated") -> agg.ShardResult:
    return agg.ShardResult(workload=workload, stage=stage, status="success", outcome=outcome, source=f"{workload}.json")


def failed(workload: str, stage: str = "ci") -> agg.ShardResult:
    return agg.ShardResult(workload=workload, stage=stage, status="failure", outcome="validated", source=f"{workload}.json")


class ExpectedOutcomeForTests(unittest.TestCase):
    def test_ci_stage_always_requires_validated(self) -> None:
        self.assertEqual(agg.expected_outcome_for("ci", {"declared": True}), "validated")
        self.assertEqual(agg.expected_outcome_for("ci", {"declared": False}), "validated")
        self.assertEqual(agg.expected_outcome_for("ci", None), "validated")

    def test_msrv_stage_follows_declared_policy(self) -> None:
        self.assertEqual(agg.expected_outcome_for("msrv", {"declared": True}), "validated")
        self.assertEqual(agg.expected_outcome_for("msrv", {"declared": False}), "not-applicable")
        self.assertEqual(agg.expected_outcome_for("msrv", None), "not-applicable")


class ComputeVerdictTests(unittest.TestCase):
    def test_full_matching_set_passes(self) -> None:
        verdict = agg.compute_verdict([exp("alpha"), exp("beta")], [ok("alpha"), ok("beta")])
        self.assertTrue(verdict.ok, verdict.reasons)

    def test_declared_msrv_validated_outcome_passes(self) -> None:
        verdict = agg.compute_verdict(
            [exp("csv-postgres", "validated")],
            [ok("csv-postgres", stage="msrv", outcome="validated")],
        )
        self.assertTrue(verdict.ok, verdict.reasons)

    def test_undeclared_msrv_not_applicable_outcome_passes(self) -> None:
        verdict = agg.compute_verdict(
            [exp("fixture-heterogeneous", "not-applicable")],
            [ok("fixture-heterogeneous", stage="msrv", outcome="not-applicable")],
        )
        self.assertTrue(verdict.ok, verdict.reasons)

    def test_declared_msrv_reporting_not_applicable_fails_closed(self) -> None:
        # The exact drift a strict reviewer flagged: a workload with
        # msrv.declared=true must never be satisfied by a not-applicable
        # disposition, even though its shard "succeeded".
        verdict = agg.compute_verdict(
            [exp("csv-postgres", "validated")],
            [ok("csv-postgres", stage="msrv", outcome="not-applicable")],
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("csv-postgres" in r and "requires 'validated'" in r for r in verdict.reasons))

    def test_undeclared_msrv_reporting_validated_fails_closed(self) -> None:
        verdict = agg.compute_verdict(
            [exp("fixture-heterogeneous", "not-applicable")],
            [ok("fixture-heterogeneous", stage="msrv", outcome="validated")],
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("requires 'not-applicable'" in r for r in verdict.reasons))

    def test_ci_stage_not_applicable_outcome_fails_closed(self) -> None:
        # not-applicable is meaningless for the ci stage regardless of any
        # workload's MSRV policy.
        verdict = agg.compute_verdict([exp("alpha", "validated")], [ok("alpha", stage="ci", outcome="not-applicable")])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("requires 'validated'" in r for r in verdict.reasons))

    def test_failed_shard_fails_closed_before_outcome_check(self) -> None:
        verdict = agg.compute_verdict([exp("alpha")], [failed("alpha")])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("alpha" in r and "failure" in r for r in verdict.reasons))

    def test_missing_result_fails_closed(self) -> None:
        verdict = agg.compute_verdict([exp("alpha"), exp("beta")], [ok("alpha")])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("beta" in r and "missing" in r for r in verdict.reasons))

    def test_missing_result_with_cancelled_job_status_names_cancellation(self) -> None:
        verdict = agg.compute_verdict(
            [exp("alpha"), exp("beta")], [ok("alpha")], job_statuses={"beta": "cancelled"}
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("cancelled" in r for r in verdict.reasons))

    def test_missing_result_with_skipped_job_status_names_skip(self) -> None:
        verdict = agg.compute_verdict(
            [exp("alpha"), exp("beta")], [ok("alpha")], job_statuses={"beta": "skipped"}
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("unexpectedly skipped" in r for r in verdict.reasons))

    def test_duplicate_result_fails_closed(self) -> None:
        dup = ok("alpha")
        dup2 = agg.ShardResult(workload="alpha", stage="ci", status="success", outcome="validated", source="alpha-2.json")
        verdict = agg.compute_verdict([exp("alpha")], [dup, dup2])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("duplicate" in r for r in verdict.reasons))

    def test_unexpected_extra_result_fails_closed(self) -> None:
        verdict = agg.compute_verdict([exp("alpha")], [ok("alpha"), ok("ghost")])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("unexpected extra result" in r and "ghost" in r for r in verdict.reasons))

    def test_renamed_shard_identity_is_both_missing_and_extra(self) -> None:
        # A shard renamed from "alpha" to "alpha-v2" must not silently satisfy
        # the "alpha" expectation.
        verdict = agg.compute_verdict([exp("alpha")], [ok("alpha-v2")])
        self.assertFalse(verdict.ok)
        reasons = " ".join(verdict.reasons)
        self.assertIn("missing result for workload 'alpha'", reasons)
        self.assertIn("unexpected extra result", reasons)
        self.assertIn("alpha-v2", reasons)

    def test_zero_workload_state_fails_closed(self) -> None:
        verdict = agg.compute_verdict([], [])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("zero-workload" in r for r in verdict.reasons))

    def test_zero_workload_state_fails_even_with_stray_results(self) -> None:
        verdict = agg.compute_verdict([], [ok("ghost")])
        self.assertFalse(verdict.ok)
        reasons = " ".join(verdict.reasons)
        self.assertIn("zero-workload", reasons)
        self.assertIn("unexpected extra result", reasons)

    def test_discovery_failure_fails_closed_even_with_perfect_results(self) -> None:
        verdict = agg.compute_verdict([exp("alpha")], [ok("alpha")], discovery_ok=False)
        self.assertFalse(verdict.ok)
        self.assertTrue(any("registry/discovery" in r for r in verdict.reasons))

    def test_upstream_job_not_ok_fails_closed_even_with_perfect_results(self) -> None:
        verdict = agg.compute_verdict([exp("alpha")], [ok("alpha")], upstream_job_ok=False)
        self.assertFalse(verdict.ok)
        self.assertTrue(any("fan-out job did not report success" in r for r in verdict.reasons))

    def test_malformed_result_file_fails_closed(self) -> None:
        verdict = agg.compute_verdict([exp("alpha")], [ok("alpha")], parse_errors=["alpha.json: invalid JSON"])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("malformed result" in r for r in verdict.reasons))

    def test_multiple_failures_all_reported_not_short_circuited(self) -> None:
        verdict = agg.compute_verdict(
            [exp("alpha"), exp("beta"), exp("gamma")], [failed("alpha"), ok("gamma"), ok("gamma")]
        )
        self.assertFalse(verdict.ok)
        reasons = " ".join(verdict.reasons)
        self.assertIn("alpha", reasons)
        self.assertIn("beta", reasons)
        self.assertIn("duplicate", reasons)
        self.assertIn("gamma", reasons)


class LoadResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write(self, name: str, payload: dict | str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")

    def test_loads_matching_stage_only(self) -> None:
        self.write("a.json", {"workload": "alpha", "stage": "ci", "status": "success", "outcome": "validated"})
        self.write("b.json", {"workload": "beta", "stage": "msrv", "status": "success", "outcome": "validated"})
        results, errors = agg.load_results(self.root, "ci")
        self.assertEqual(errors, [])
        self.assertEqual([r.workload for r in results], ["alpha"])

    def test_reports_invalid_json_as_parse_error(self) -> None:
        self.write("bad.json", "{not json")
        results, errors = agg.load_results(self.root, "ci")
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("bad.json", errors[0])

    def test_reports_missing_keys_as_parse_error(self) -> None:
        self.write("incomplete.json", {"workload": "alpha", "stage": "ci"})
        results, errors = agg.load_results(self.root, "ci")
        self.assertEqual(results, [])
        self.assertIn("missing key", errors[0])

    def test_reports_invalid_status_as_parse_error(self) -> None:
        self.write("bad-status.json", {"workload": "alpha", "stage": "ci", "status": "maybe", "outcome": "validated"})
        results, errors = agg.load_results(self.root, "ci")
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)

    def test_missing_directory_yields_no_results_no_errors(self) -> None:
        results, errors = agg.load_results(self.root / "does-not-exist", "ci")
        self.assertEqual(results, [])
        self.assertEqual(errors, [])

    def test_finds_results_in_nested_artifact_subdirectories(self) -> None:
        self.write(
            "result-ci-alpha/result.json",
            {"workload": "alpha", "stage": "ci", "status": "success", "outcome": "validated"},
        )
        results, errors = agg.load_results(self.root, "ci")
        self.assertEqual(errors, [])
        self.assertEqual([r.workload for r in results], ["alpha"])


class LoadExpectedTests(unittest.TestCase):
    def test_builds_expected_outcome_from_msrv_policy(self) -> None:
        discover_json = json.dumps(
            {
                "include": [
                    {"name": "csv-postgres", "msrv": {"declared": True, "version": "1.95"}},
                    {"name": "fixture-heterogeneous", "msrv": {"declared": False, "policy_reason": "n/a"}},
                ]
            }
        )
        expected = agg._load_expected(discover_json, "msrv")
        by_name = {e.name: e.expected_outcome for e in expected}
        self.assertEqual(by_name, {"csv-postgres": "validated", "fixture-heterogeneous": "not-applicable"})

    def test_ci_stage_ignores_msrv_policy(self) -> None:
        discover_json = json.dumps({"include": [{"name": "fixture-heterogeneous", "msrv": {"declared": False, "policy_reason": "n/a"}}]})
        expected = agg._load_expected(discover_json, "ci")
        self.assertEqual(expected[0].expected_outcome, "validated")


if __name__ == "__main__":
    unittest.main()
