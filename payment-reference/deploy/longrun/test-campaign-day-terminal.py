#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import campaign_day_terminal as m


class TerminalDaySequencerTests(unittest.TestCase):
    def config(self, root: Path) -> m.TerminalConfig:
        return m.TerminalConfig(
            campaign_dir=root / "campaign",
            online_evidence_root=root / "online",
            reconciliation_workload_binary=root / "bin/reconciliation",
            provider_account_id="provider-account",
            gateway_url="http://127.0.0.1:28082",
            provider_url="http://127.0.0.1:28084",
            merchant_url="http://127.0.0.1:28083",
            killbill_url="http://127.0.0.1:28081",
            fault_proxy_base_url="http://127.0.0.1:28085",
            fault_control_timeout_seconds=7.5,
            provider_control_timeout_seconds=8.5,
            repo_root=root / "repo",
            data_path=root / "data",
            compose_file=root / "compose.yaml",
            compose_env_file=root / "compose.env",
            recovery_crash_evidence=root / "recovery-crash.json",
            reconciliation_crash_evidence=root / "reconciliation-crash.json",
            day=3,
        )

    def identity(self) -> dict:
        return {
            "campaign_id": "campaign-318",
            "source_revision": "a" * 40,
        }

    def test_build_steps_freezes_current_terminal_order_and_explicit_runtime_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            steps = m.build_steps(config, self.identity())

        self.assertEqual([step.name for step in steps], [
            "canonical-online",
            "canonical-online-proof",
            "recovery-proof",
            "reconciliation-proof",
            "settlement-roster",
            "settlement-close",
            "provider-cardinality",
            "financial-invariants",
            "quiescence-sampling",
            "quiescence-proof",
        ])
        commands = [" ".join(step.command) for step in steps]
        self.assertIn(config.fault_proxy_base_url, commands[0])
        self.assertIn(config.gateway_url, commands[4])
        self.assertIn(config.provider_url, commands[4])
        self.assertIn(config.merchant_url, commands[5])
        self.assertIn(config.killbill_url, commands[7])
        self.assertIn(str(config.recovery_crash_evidence), commands[2])
        self.assertIn(str(config.reconciliation_crash_evidence), commands[3])
        self.assertIn(str(config.compose_file), commands[8])
        env = m.subprocess_environment(config)
        self.assertEqual(env["GATEWAY_PORT"], "28082")
        self.assertEqual(env["PAYMENT_SANDBOX_DIRECT_PORT"], "28084")
        self.assertEqual(env["MERCHANT_PORT"], "28083")

    def test_validate_config_rejects_non_loopback_endpoint_before_day_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            for path in (
                config.campaign_dir,
                config.online_evidence_root,
                config.repo_root,
                config.data_path,
                config.reconciliation_workload_binary.parent,
            ):
                path.mkdir(parents=True, exist_ok=True)
            for path in (
                config.reconciliation_workload_binary,
                config.compose_file,
                config.compose_env_file,
                config.recovery_crash_evidence,
                config.reconciliation_crash_evidence,
            ):
                path.write_text("x", encoding="utf-8")
            invalid = m.TerminalConfig(
                **{**config.__dict__, "gateway_url": "http://example.com:28082"}
            )
            with self.assertRaisesRegex(m.TerminalDayError, "Gateway URL"):
                m.validate_config(invalid)

    def test_run_step_rejects_wrong_decision_even_on_zero_exit(self):
        step = m.Step("proof", "EXPECTED", ("python3", "proof.py"))

        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, '{"decision":"WRONG"}\n', "")

        with self.assertRaisesRegex(m.TerminalDayError, "decision mismatch"):
            m.run_step(step, env={}, runner=runner)

    def test_execute_starts_once_runs_all_steps_then_delegates_terminal_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            order = []

            for path in (
                config.campaign_dir,
                config.online_evidence_root,
                config.repo_root,
                config.data_path,
                config.reconciliation_workload_binary.parent,
            ):
                path.mkdir(parents=True, exist_ok=True)
            for path in (
                config.reconciliation_workload_binary,
                config.compose_file,
                config.compose_env_file,
                config.recovery_crash_evidence,
                config.reconciliation_crash_evidence,
            ):
                path.write_text("x", encoding="utf-8")

            expected_by_script = {
                "campaign_day_online_canonical.py": "ONLINE_TRAFFIC_COMPLETE_CANONICAL_ORDER_VERIFIED",
                "campaign_day_online_verification.py": "ONLINE_EXECUTION_VERIFIED_NOT_DAY_ACCEPTED",
                "campaign_day_recovery.py": "RECOVERY_VERIFIED_NOT_DAY_ACCEPTED",
                "campaign_day_reconciliation.py": "RECONCILIATION_VERIFIED_NOT_DAY_ACCEPTED",
                "campaign-settlement-roster.py": "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED",
                "campaign-settlement-verification.py": "SETTLEMENT_VERIFIED_NOT_DAY_ACCEPTED",
                "campaign_day_cardinality.py": "PROVIDER_DAY_CARDINALITY_VERIFIED_NOT_DAY_ACCEPTED",
                "campaign_day_financial_invariants.py": "FINANCIAL_INVARIANTS_VERIFIED_NOT_DAY_ACCEPTED",
                "campaign_quiescence.py": "QUIESCENCE_VERIFIED_NOT_DAY_ACCEPTED",
                "campaign_day_quiescence_verification.py": "QUIESCENCE_WINDOW_VERIFIED_NOT_DAY_ACCEPTED",
            }

            def runner(command, **kwargs):
                script = Path(command[1]).name
                order.append(script)
                return subprocess.CompletedProcess(
                    command, 0,
                    json.dumps({"decision": expected_by_script[script]}) + "\n",
                    "",
                )

            def finalize(campaign_dir, day):
                order.append("materialize-terminal")
                return {
                    "decision": "DAY_TERMINAL_SUCCESS_INDEPENDENTLY_VERIFIED",
                    "logical_day_sequence": day,
                    "frontier_sequence": day,
                    "campaign_acceptance_authorized": False,
                }

            with (
                mock.patch.object(m, "load_identity", return_value=self.identity()),
                mock.patch.object(
                    m.evidence,
                    "lifecycle_state",
                    return_value={"frontier_sequence": 2, "active_day": None, "terminal_failure": None},
                ),
                mock.patch.object(
                    m.evidence,
                    "inspect",
                    return_value={"decision": "READY", "frontier_sequence": 2,
                                  "next_day": {"logical_day_sequence": 3}},
                ),
                mock.patch.object(m.evidence, "append_event") as append_event,
                mock.patch.object(m.evidence, "materialize_terminal_day", side_effect=finalize),
            ):
                result = m.execute(config, runner=runner)

            append_event.assert_called_once_with(config.campaign_dir, 3, "day-started", {})
            self.assertEqual(order[-1], "materialize-terminal")
            self.assertEqual(len(order), 11)
            self.assertEqual(result["frontier_sequence"], 3)
            self.assertFalse(result["campaign_acceptance_authorized"])

    def test_execute_completed_day_is_noop_not_financial_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            for path in (
                config.campaign_dir,
                config.online_evidence_root,
                config.repo_root,
                config.data_path,
                config.reconciliation_workload_binary.parent,
            ):
                path.mkdir(parents=True, exist_ok=True)
            for path in (
                config.reconciliation_workload_binary,
                config.compose_file,
                config.compose_env_file,
                config.recovery_crash_evidence,
                config.reconciliation_crash_evidence,
            ):
                path.write_text("x", encoding="utf-8")

            runner = mock.Mock()
            with (
                mock.patch.object(m, "load_identity", return_value=self.identity()),
                mock.patch.object(
                    m.evidence,
                    "lifecycle_state",
                    return_value={"frontier_sequence": 3, "active_day": None, "terminal_failure": None},
                ),
                mock.patch.object(
                    m.evidence,
                    "inspect",
                    return_value={"decision": "READY", "frontier_sequence": 3,
                                  "next_day": {"logical_day_sequence": 4}},
                ),
                mock.patch.object(m.evidence, "append_event") as append_event,
                mock.patch.object(m.evidence, "materialize_terminal_day") as finalize,
            ):
                result = m.execute(config, runner=runner)

            self.assertEqual(result["decision"], "DAY_ALREADY_TERMINAL")
            runner.assert_not_called()
            append_event.assert_not_called()
            finalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
