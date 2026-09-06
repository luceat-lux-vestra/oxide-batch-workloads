#!/usr/bin/env python3
import importlib.util
import math
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("four_way.py")
SPEC = importlib.util.spec_from_file_location("four_way", MODULE_PATH)
assert SPEC and SPEC.loader
four = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(four)


class FourWayHarnessTests(unittest.TestCase):
    def test_four_position_rotation(self):
        self.assertEqual(four.candidate_order(0), ("raw_rust", "oxide", "raw_java", "spring"))
        self.assertEqual(four.candidate_order(1), ("oxide", "raw_java", "spring", "raw_rust"))
        self.assertEqual(four.candidate_order(3), ("spring", "raw_rust", "oxide", "raw_java"))
        counts = four.position_counts(8)
        self.assertTrue(all(values == [2, 2, 2, 2] for values in counts.values()))

    def test_measured_rounds_fail_closed_unless_multiple_of_four(self):
        for value in (1, 2, 3, 5, 7, 9):
            with self.assertRaises(ValueError):
                four.assert_balanced_measured_rounds(value)
        four.assert_balanced_measured_rounds(4)
        four.assert_balanced_measured_rounds(8)

    def test_distribution_has_required_statistics(self):
        summary = four.distribution([1, 2, 3, 4, 5])
        self.assertEqual(summary["min"], 1.0)
        self.assertEqual(summary["median"], 3.0)
        self.assertEqual(summary["max"], 5.0)
        self.assertTrue(math.isclose(summary["p95"], 4.8))

    def test_interpretation_pairs_are_exact(self):
        self.assertEqual(
            four.PAIRINGS,
            (("raw_rust", "oxide"), ("raw_java", "spring"), ("raw_rust", "raw_java"), ("oxide", "spring")),
        )

    def test_java_launcher_metrics_require_exact_single_pid_and_active_interval(self):
        parsed = four.parse_java_metrics(
            "noise\nOXIDEBATCH_BENCH_PID=123\nOXIDEBATCH_BENCH_ACTIVE_WORK_NS=500000000\n",
            0.6,
        )
        self.assertEqual(parsed["candidate_pid"], 123)
        self.assertTrue(math.isclose(parsed["active_work_seconds"], 0.5))
        with self.assertRaises(RuntimeError):
            four.parse_java_metrics("OXIDEBATCH_BENCH_PID=123\n", 1.0)

    def test_process_pid_parser_is_fail_closed(self):
        self.assertEqual(four.parse_process_pid("OXIDEBATCH_PROCESS_PID=42\n"), 42)
        with self.assertRaises(RuntimeError):
            four.parse_process_pid("missing\n")
        with self.assertRaises(RuntimeError):
            four.parse_process_pid("OXIDEBATCH_PROCESS_PID=1\nOXIDEBATCH_PROCESS_PID=2\n")

    def test_verifier_parser_rejects_invalid_digest_or_mismatch(self):
        import json
        valid = {
            "source_digest": "a" * 64,
            "source_rows": 10,
            "destination_rows": 10,
            "row_counts_match": True,
            "expected_digest_sha256": "b" * 64,
            "actual_digest_sha256": "b" * 64,
            "digests_match": True,
            "total_mismatches": 0,
        }
        self.assertEqual(four.parse_verify_report(json.dumps(valid), 10)["source_rows"], 10)
        bad = dict(valid, source_digest="not-a-digest")
        with self.assertRaises(RuntimeError):
            four.parse_verify_report(json.dumps(bad), 10)
        bad = dict(valid, destination_rows=9, row_counts_match=False)
        with self.assertRaises(RuntimeError):
            four.parse_verify_report(json.dumps(bad), 10)

    def test_sample_database_names_are_safe_and_unique(self):
        names = {
            four.sample_database_name(mode, kind, ordinal, candidate)
            for mode in four.READER_MODES
            for kind in ("warmup", "measured", "recovery")
            for ordinal in (1, 2)
            for candidate in four.CANDIDATES
        }
        self.assertEqual(len(names), 48)
        self.assertTrue(all(four.DATABASE_NAME.fullmatch(name) for name in names))

    def test_crash_identity_and_durable_snapshot_are_pre_recovery_invariants(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('crash_env["OXIDEBATCH_SPAWN_PID_FILE"]', source)
        self.assertIn('if not spawn_text.isdigit() or int(spawn_text) != killed_pid:', source)
        snapshot = source.index("durable_metadata = durability_state(database, candidate, name, mode)")
        recover = source.index("recover = recover_command(candidate, args=args, url=url, name=name, mode=mode)")
        self.assertLess(snapshot, recover)

    def test_recovery_targets_precommit_prefix_near_half(self):
        self.assertEqual(four.recovery_kill_chunk(1_000_000, 1_000), 501)
        self.assertEqual((501 - 1) * 1000, 500_000)

    def test_jdbc_url_removes_url_credentials(self):
        url = four.jdbc_url(
            "postgresql://oxide_batch_workload:secret@localhost:5434/base",
            "fw_c_m_01_rr",
        )
        self.assertEqual(url, "jdbc:postgresql://localhost:5434/fw_c_m_01_rr")

    def test_writer_contract(self):
        metrics = four.writer_metrics(1000, 1000)
        self.assertEqual(metrics["writer_statements"], 4)
        self.assertEqual(metrics["max_bound_parameters_per_full_statement"], 1995)

    def test_manual_workflow_canonical_defaults_and_security(self):
        workflow = MODULE_PATH.parents[2] / ".github" / "workflows" / "benchmark-postgres-postgres-four-way.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("push:", text)
        for value in ('default: "1000000"', 'default: "20260904"', 'default: "1000"', 'default: "500"', 'default: "750"', 'default: "2"', 'default: "8"'):
            self.assertIn(value, text)
        self.assertIn("runs-on: ubuntu-24.04", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("if: always()", text)
        self.assertIn("retention-days: 30", text)
        self.assertIn("BENCH_MEASURED_RUNS % 4 == 0", text)

    def test_semantic_gate_runs_on_pr_and_main_push(self):
        workflow = MODULE_PATH.parents[2] / ".github" / "workflows" / "pr4-four-way-semantic.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("pull_request:", text)
        self.assertIn("push:", text)
        self.assertIn("branches: [main]", text)
        self.assertIn("./ci/validate-raw-jdbc-crash-recovery", text)
        self.assertIn("--measured-runs 4", text)
        self.assertIn("persist-credentials: false", text)
        migrate = text.index("cargo run --locked -q -p postgres-postgres -- migrate")
        crash = text.index("./ci/validate-raw-jdbc-crash-recovery")
        self.assertLess(migrate, crash)

    def test_pr4_preserves_pr3_readme_evidence_contract(self):
        readme = MODULE_PATH.parent / "java" / "README.md"
        text = readme.read_text(encoding="utf-8")
        self.assertIn("Four-way measurement starts only in PR4", text)


if __name__ == "__main__":
    unittest.main()
