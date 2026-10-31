#!/usr/bin/env python3

import importlib.util
import json
import os
import subprocess
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

    def test_supply_chain_stage_always_requires_validated(self) -> None:
        # Unlike msrv, supply-chain has no policy-exemption concept: every
        # registered real workload is always expected to have actually run
        # the scan, regardless of its (irrelevant) msrv policy.
        self.assertEqual(agg.expected_outcome_for("supply-chain", {"declared": True}), "validated")
        self.assertEqual(agg.expected_outcome_for("supply-chain", {"declared": False}), "validated")
        self.assertEqual(agg.expected_outcome_for("supply-chain", None), "validated")


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


class SupplyChainStageCoverageTests(unittest.TestCase):
    """Explicit incomplete-coverage proof for the supply-chain stage (#32).

    Exercises the exact same production `compute_verdict` used by the other
    stages, with two expected real workloads A + B, covering every
    incomplete/duplicate/malformed/upstream-failure scenario the aggregate
    must fail closed on, plus the one scenario where it must pass.
    """

    def sc_exp(self, name: str) -> agg.ExpectedShard:
        return agg.ExpectedShard(name=name, expected_outcome="validated")

    def sc_ok(self, name: str) -> agg.ShardResult:
        return agg.ShardResult(workload=name, stage="supply-chain", status="success", outcome="validated", source=f"{name}.json")

    def test_a_only_fails(self) -> None:
        verdict = agg.compute_verdict([self.sc_exp("A"), self.sc_exp("B")], [self.sc_ok("A")])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("B" in r and "missing" in r for r in verdict.reasons))

    def test_b_cancelled_fails(self) -> None:
        verdict = agg.compute_verdict(
            [self.sc_exp("A"), self.sc_exp("B")], [self.sc_ok("A")], job_statuses={"B": "cancelled"}
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("B" in r and "cancelled" in r for r in verdict.reasons))

    def test_b_skipped_fails(self) -> None:
        verdict = agg.compute_verdict(
            [self.sc_exp("A"), self.sc_exp("B")], [self.sc_ok("A")], job_statuses={"B": "skipped"}
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("B" in r and "unexpectedly skipped" in r for r in verdict.reasons))

    def test_duplicate_a_fails(self) -> None:
        dup1 = self.sc_ok("A")
        dup2 = agg.ShardResult(workload="A", stage="supply-chain", status="success", outcome="validated", source="A-2.json")
        verdict = agg.compute_verdict([self.sc_exp("A"), self.sc_exp("B")], [dup1, dup2, self.sc_ok("B")])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("duplicate" in r and "A" in r for r in verdict.reasons))

    def test_unexpected_c_fails(self) -> None:
        verdict = agg.compute_verdict(
            [self.sc_exp("A"), self.sc_exp("B")], [self.sc_ok("A"), self.sc_ok("B"), self.sc_ok("C")]
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("unexpected extra result" in r and "C" in r for r in verdict.reasons))

    def test_malformed_result_fails(self) -> None:
        verdict = agg.compute_verdict(
            [self.sc_exp("A"), self.sc_exp("B")],
            [self.sc_ok("A"), self.sc_ok("B")],
            parse_errors=["B-2.json: invalid JSON"],
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("malformed result" in r for r in verdict.reasons))

    def test_discovery_failure_fails(self) -> None:
        verdict = agg.compute_verdict(
            [self.sc_exp("A"), self.sc_exp("B")], [self.sc_ok("A"), self.sc_ok("B")], discovery_ok=False
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("registry/discovery" in r for r in verdict.reasons))

    def test_fan_out_failure_fails(self) -> None:
        verdict = agg.compute_verdict(
            [self.sc_exp("A"), self.sc_exp("B")], [self.sc_ok("A"), self.sc_ok("B")], upstream_job_ok=False
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("fan-out job did not report success" in r for r in verdict.reasons))

    def test_a_and_b_both_validated_passes(self) -> None:
        verdict = agg.compute_verdict([self.sc_exp("A"), self.sc_exp("B")], [self.sc_ok("A"), self.sc_ok("B")])
        self.assertTrue(verdict.ok, verdict.reasons)


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



class AggregateWorkflowIndependenceTests(unittest.TestCase):
    def test_required_aggregates_do_not_depend_on_jobs_api(self) -> None:
        # Shard-job API details are diagnostic only. Required aggregate verdicts
        # must not fail when the GitHub Jobs API intermittently responds 502.
        source = (MODULE_PATH.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for job, following, upstream in (
            ("workloads-ci", "workloads-msrv", "ci-shard"),
            ("workloads-msrv", "supply-chain", "msrv-shard"),
        ):
            section = source.split(f"  {job}:\n", 1)[1]
            if following is not None:
                section = section.split(f"  {following}:\n", 1)[0]
            with self.subTest(job=job):
                self.assertIn("Compute aggregate verdict", section)
                self.assertIn("aggregate_verdict.py", section)
                self.assertIn(f"needs.{upstream}.result == 'success'", section)
                self.assertIn('--upstream-job-ok "$UPSTREAM_JOB_OK"', section)
                self.assertIn("DISCOVERY_OK:", section)
                self.assertIn("Download shard results", section)
                self.assertNotIn("listJobsForWorkflowRunAttempt", section)
                self.assertNotIn("Fetch shard job conclusions", section)
                self.assertNotIn("steps.jobs.outputs.result", section)

    def test_supply_chain_is_single_registry_driven_authority(self) -> None:
        source = (MODULE_PATH.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("  supply-chain-shard:\n", source)
        section = source.split("  supply-chain:\n", 1)[1].split(
            "  # Path-scoped qualification authorities execute inside", 1
        )[0]
        self.assertIn("needs: discover", section)
        self.assertIn("needs.discover.outputs.supply-chain-workloads", section)
        self.assertIn("run-supply-chain-authority.py", section)
        self.assertNotIn("strategy:", section)
        self.assertNotIn("matrix:", section)
        self.assertNotIn("upload-artifact@", section)
        self.assertNotIn("download-artifact@", section)
        self.assertNotIn("aggregate_verdict.py", section)

    def test_pr_discovery_is_local_and_fast_is_advisory(self) -> None:
        source = (MODULE_PATH.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
        discovery = source.split("  discover:\n", 1)[1].split("  ci-shard:\n", 1)[0]
        self.assertIn("python3 .github/scripts/test-workload-registry.py", discovery)
        self.assertIn("python3 .github/scripts/validate-workload-registry.py", discovery)
        self.assertNotIn("fast-evidence", discovery)
        self.assertNotIn("trusted_api_json()", discovery)
        self.assertNotIn("actions/workflows/fast-branch.yml/runs", discovery)
        self.assertNotIn("actions/runs/$run_id/jobs", discovery)
        self.assertNotIn("for _ in $(seq 1 60)", discovery)
        self.assertNotIn("gh api", discovery)
        # Retain independent early feedback, but never use its push run for PR authority.
        fast = (MODULE_PATH.parents[1] / "workflows" / "fast-branch.yml").read_text(encoding="utf-8")
        self.assertIn("  push:", fast)
        self.assertNotIn("  pull_request:", fast)
        self.assertIn("  dependency-review:\n", source)
        self.assertIn("name: dependency-review", source)
        self.assertIn("actions/dependency-review-action@", source)
        self.assertIn("  merge-gate:\n", source)

    def test_final_gate_declares_all_semantic_authorities(self) -> None:
        source = (MODULE_PATH.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
        gate = source.split("  merge-gate:\n", 1)[1]
        self.assertIn("    if: always()", gate)
        self.assertIn("    name: Merge Gate", gate)
        for name in (
            "discover", "ci-shard", "msrv-shard", "workloads-ci", "workloads-msrv", "supply-chain", "dependency-review",
            "jberet-clean-qualification", "jberet-crash-qualification",
            "pr4-semantic-qualification", "repository-governance-policy",
        ):
            with self.subTest(name=name):
                self.assertIn(f"      - {name}\n", gate)

    def test_final_gate_rejects_adversarial_job_results(self) -> None:
        # Execute the *actual* inline GitHub Actions gate Python, rather than
        # duplicating verdict logic in a test-only imitation.
        source = (MODULE_PATH.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
        gate = source.split("  merge-gate:\n", 1)[1]
        program = gate.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        program = "\n".join(line.removeprefix("          ") for line in program.splitlines()) + "\n"
        scoped = {
            "jberet-clean-qualification": "jberet_clean",
            "jberet-crash-qualification": "jberet_crash",
            "pr4-semantic-qualification": "pr4_semantic",
            "repository-governance-policy": "governance",
        }
        names = {
            "discover", "ci-shard", "msrv-shard", "workloads-ci", "workloads-msrv", "supply-chain", "dependency-review",
            *scoped,
        }

        def run(
            changes: dict[str, str] | None = None,
            *,
            scopes: dict[str, str] | None = None,
            removed: str | None = None,
            extra: str | None = None,
            event: str = "pull_request",
            draft: str = "false",
        ) -> subprocess.CompletedProcess[str]:
            scope_values = {output: "false" for output in scoped.values()}
            scope_values.update(scopes or {})
            values = {name: {"result": "success"} for name in names}
            values["discover"]["outputs"] = scope_values
            for name, output in scoped.items():
                enabled = scope_values.get(output)
                values[name] = {"result": "success" if enabled == "true" else "skipped"}
            if event == "workflow_dispatch":
                values["dependency-review"] = {"result": "skipped"}
            for name, result in (changes or {}).items():
                values[name] = {"result": result}
            if removed is not None:
                del values[removed]
            if extra is not None:
                values[extra] = {"result": "success"}
            return subprocess.run(
                [sys.executable, "-c", program],
                env={**os.environ, "NEEDS_JSON": json.dumps(values),
                     "GITHUB_EVENT_NAME": event, "PR_DRAFT": draft},
                capture_output=True, text=True, check=False,
            )

        for label, kwargs in (
            ("valid PR", {}),
            ("valid scoped PR", {"scopes": {"jberet_clean": "true", "governance": "true"}}),
            ("valid manual", {"event": "workflow_dispatch"}),
        ):
            with self.subTest(label=label):
                actual = run(**kwargs)
                self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr)

        for label, kwargs in (
            ("missing shard", {"removed": "ci-shard"}),
            ("missing dependency", {"removed": "dependency-review"}),
            ("missing scoped authority", {"removed": "jberet-clean-qualification"}),
            ("unexpected extra", {"extra": "untrusted-observer"}),
            ("failed aggregate", {"changes": {"workloads-msrv": "failure"}}),
            ("failed supply-chain authority", {"changes": {"supply-chain": "failure"}}),
            ("skipped discovery", {"changes": {"discover": "skipped"}}),
            ("invalid scope classification", {"scopes": {"jberet_clean": "unknown"}}),
            ("applicable scoped failure", {
                "scopes": {"jberet_clean": "true"},
                "changes": {"jberet-clean-qualification": "failure"},
            }),
            ("non-applicable scoped success", {
                "changes": {"jberet-clean-qualification": "success"},
            }),
            ("skipped PR review", {"changes": {"dependency-review": "skipped"}}),
            ("manual claimed diff review", {"event": "workflow_dispatch",
                                             "changes": {"dependency-review": "success"}}),
            ("draft", {"draft": "true"}),
            ("unrecognized event", {"event": "push"}),
        ):
            with self.subTest(label=label):
                actual = run(**kwargs)
                self.assertNotEqual(actual.returncode, 0, label + ": unexpected pass")
                self.assertIn("::error::", actual.stdout)



if __name__ == "__main__":
    unittest.main()
