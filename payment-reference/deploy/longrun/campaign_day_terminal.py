#!/usr/bin/env python3
"""#318 minimal terminal-day sequencing controller.

The controller sequences existing qualified producers but is not a financial
acceptance authority. Only campaign-evidence.py may materialize terminal PASS,
after independently validating the canonical eight-proof contract.
"""
from __future__ import annotations

import argparse
import fcntl
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.parse
from typing import Any, Callable

import campaign_core as core

HERE = Path(__file__).resolve().parent
EVIDENCE_SCRIPT = HERE / "campaign-evidence.py"


class TerminalDayError(core.EvidenceError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TerminalDayError(message)


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evidence = _module("payment_reference_campaign_evidence_terminal", EVIDENCE_SCRIPT)


@dataclass(frozen=True)
class TerminalConfig:
    campaign_dir: Path
    online_evidence_root: Path
    reconciliation_workload_binary: Path
    provider_account_id: str
    gateway_url: str
    provider_url: str
    merchant_url: str
    killbill_url: str
    fault_proxy_base_url: str
    fault_control_timeout_seconds: float
    provider_control_timeout_seconds: float
    repo_root: Path
    data_path: Path
    compose_file: Path
    compose_env_file: Path | None
    recovery_crash_evidence: Path | None
    reconciliation_crash_evidence: Path | None
    day: int


@dataclass(frozen=True)
class Step:
    name: str
    expected_decision: str
    command: tuple[str, ...]


def load_identity(campaign_dir: Path) -> dict[str, Any]:
    record = core.load_json(campaign_dir / "campaign.json")
    identity = record.get("identity")
    require(isinstance(identity, dict), "campaign identity missing")
    evidence.verify_identity(identity)
    require(identity["campaign_id"] == campaign_dir.name, "campaign directory identity mismatch")
    return identity


def validate_config(config: TerminalConfig) -> TerminalConfig:
    require(type(config.day) is int and config.day > 0, "day must be positive")
    require(config.campaign_dir.is_dir() and not config.campaign_dir.is_symlink(),
            "campaign directory missing/unsafe")
    require(config.online_evidence_root.is_dir() and not config.online_evidence_root.is_symlink(),
            "online evidence root missing/unsafe")
    require(config.repo_root.is_dir() and not config.repo_root.is_symlink(),
            "repository root missing/unsafe")
    require(config.data_path.is_dir() and not config.data_path.is_symlink(),
            "campaign data path missing/unsafe")
    for path, label in (
        (config.reconciliation_workload_binary, "reconciliation workload binary"),
        (config.compose_file, "compose file"),
    ):
        require(path.is_file() and not path.is_symlink(), f"{label} missing/unsafe")
    for path, label in (
        (config.compose_env_file, "compose env file"),
        (config.recovery_crash_evidence, "recovery crash evidence"),
        (config.reconciliation_crash_evidence, "reconciliation crash evidence"),
    ):
        if path is not None:
            require(path.is_file() and not path.is_symlink(), f"{label} missing/unsafe")
    for value, label in (
        (config.provider_account_id, "provider account id"),
        (config.gateway_url, "gateway URL"),
        (config.provider_url, "provider URL"),
        (config.merchant_url, "merchant URL"),
        (config.killbill_url, "Kill Bill URL"),
        (config.fault_proxy_base_url, "fault proxy URL"),
    ):
        require(isinstance(value, str) and value.strip(), f"{label} must be explicit")
    for value, label in (
        (config.fault_control_timeout_seconds, "fault control timeout"),
        (config.provider_control_timeout_seconds, "provider control timeout"),
    ):
        require(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0,
            f"{label} must be positive",
        )
    for url, label in (
        (config.gateway_url, "Gateway URL"),
        (config.provider_url, "provider URL"),
        (config.merchant_url, "Merchant URL"),
        (config.killbill_url, "Kill Bill URL"),
        (config.fault_proxy_base_url, "fault proxy URL"),
    ):
        explicit_loopback_port(url, label)
    return config


def artifact_paths(campaign_dir: Path, day: int) -> dict[str, Path]:
    label = f"day-{day:06}"
    statements = campaign_dir / "statements"
    return {
        "roster": statements / f"{label}.roster.json",
        "roster_verification": statements / f"{label}.roster.verification.json",
        "statement": statements / f"{label}.statement.json",
        "settlement_verification": statements / f"{label}.settlement.verification.json",
    }


def build_steps(config: TerminalConfig, identity: dict[str, Any]) -> list[Step]:
    paths = artifact_paths(config.campaign_dir, config.day)
    common_day = ("--campaign-dir", str(config.campaign_dir), "--day", str(config.day))

    recovery = [
        sys.executable, str(HERE / "campaign_day_recovery.py"),
        *common_day,
    ]
    if config.recovery_crash_evidence is not None:
        recovery.extend(["--crash-evidence", str(config.recovery_crash_evidence)])

    reconciliation = [
        sys.executable, str(HERE / "campaign_day_reconciliation.py"),
        *common_day,
        "--workload-binary", str(config.reconciliation_workload_binary),
    ]
    if config.reconciliation_crash_evidence is not None:
        reconciliation.extend(["--crash-evidence", str(config.reconciliation_crash_evidence)])

    quiescence = [
        sys.executable, str(HERE / "campaign_quiescence.py"),
        "--campaign-dir", str(config.campaign_dir),
        "--repo-root", str(config.repo_root),
        "--data-path", str(config.data_path),
        "--compose-file", str(config.compose_file),
        "--day", str(config.day),
    ]
    if config.compose_env_file is not None:
        quiescence.extend(["--compose-env-file", str(config.compose_env_file)])

    return [
        Step(
            "canonical-online",
            "ONLINE_TRAFFIC_COMPLETE_CANONICAL_ORDER_VERIFIED",
            tuple([
                sys.executable, str(HERE / "campaign_day_online_canonical.py"),
                "--campaign-id", identity["campaign_id"],
                "--day", str(config.day),
                "--execute-canonical-day",
                "--expected-source-sha", identity["source_revision"],
                "--provenance-dir", str(config.campaign_dir / "provenance"),
                "--evidence-root", str(config.online_evidence_root),
                "--fault-proxy-base-url", config.fault_proxy_base_url,
                "--fault-control-timeout-seconds", str(config.fault_control_timeout_seconds),
                "--provider-control-timeout-seconds", str(config.provider_control_timeout_seconds),
            ]),
        ),
        Step(
            "canonical-online-proof",
            "ONLINE_EXECUTION_VERIFIED_NOT_DAY_ACCEPTED",
            (
                sys.executable, str(HERE / "campaign_day_online_verification.py"),
                "--campaign-dir", str(config.campaign_dir),
                "--online-evidence-root", str(config.online_evidence_root),
                "--day", str(config.day),
            ),
        ),
        Step("recovery-proof", "RECOVERY_VERIFIED_NOT_DAY_ACCEPTED", tuple(recovery)),
        Step("reconciliation-proof", "RECONCILIATION_VERIFIED_NOT_DAY_ACCEPTED", tuple(reconciliation)),
        Step(
            "settlement-roster",
            "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED",
            (
                sys.executable, str(HERE / "campaign-settlement-roster.py"),
                *common_day,
                "--provider-account-id", config.provider_account_id,
                "--gateway-url", config.gateway_url,
                "--provider-url", config.provider_url,
                "--output", str(paths["roster"]),
                "--verification-output", str(paths["roster_verification"]),
            ),
        ),
        Step(
            "settlement-close",
            "SETTLEMENT_VERIFIED_NOT_DAY_ACCEPTED",
            (
                sys.executable, str(HERE / "campaign-settlement-verification.py"),
                *common_day,
                "--roster", str(paths["roster"]),
                "--roster-verification", str(paths["roster_verification"]),
                "--statement-output", str(paths["statement"]),
                "--verification-output", str(paths["settlement_verification"]),
                "--provider-url", config.provider_url,
                "--merchant-url", config.merchant_url,
            ),
        ),
        Step(
            "provider-cardinality",
            "PROVIDER_DAY_CARDINALITY_VERIFIED_NOT_DAY_ACCEPTED",
            (
                sys.executable, str(HERE / "campaign_day_cardinality.py"),
                *common_day,
            ),
        ),
        Step(
            "financial-invariants",
            "FINANCIAL_INVARIANTS_VERIFIED_NOT_DAY_ACCEPTED",
            (
                sys.executable, str(HERE / "campaign_day_financial_invariants.py"),
                *common_day,
                "--merchant-url", config.merchant_url,
                "--gateway-url", config.gateway_url,
                "--killbill-url", config.killbill_url,
            ),
        ),
        Step("quiescence-sampling", "QUIESCENCE_VERIFIED_NOT_DAY_ACCEPTED", tuple(quiescence)),
        Step(
            "quiescence-proof",
            "QUIESCENCE_WINDOW_VERIFIED_NOT_DAY_ACCEPTED",
            (
                sys.executable, str(HERE / "campaign_day_quiescence_verification.py"),
                *common_day,
            ),
        ),
    ]


def explicit_loopback_port(url: str, label: str) -> int:
    parsed = urllib.parse.urlsplit(url)
    require(
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment,
        f"{label} must be an explicit 127.0.0.1 HTTP endpoint with port",
    )
    return parsed.port


def subprocess_environment(config: TerminalConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["PAYMENT_REFERENCE_COMPOSE_FILE"] = str(config.compose_file)
    if config.compose_env_file is None:
        env.pop("PAYMENT_REFERENCE_COMPOSE_ENV_FILE", None)
    else:
        env["PAYMENT_REFERENCE_COMPOSE_ENV_FILE"] = str(config.compose_env_file)

    # Existing qualified producers validate their explicit loopback URL against
    # these configured host-port variables. Derive them from the controller's
    # explicit endpoints instead of falling back to producer-local defaults.
    env["GATEWAY_PORT"] = str(explicit_loopback_port(config.gateway_url, "Gateway URL"))
    env["PAYMENT_SANDBOX_DIRECT_PORT"] = str(
        explicit_loopback_port(config.provider_url, "provider URL")
    )
    env["MERCHANT_PORT"] = str(explicit_loopback_port(config.merchant_url, "Merchant URL"))
    explicit_loopback_port(config.killbill_url, "Kill Bill URL")
    explicit_loopback_port(config.fault_proxy_base_url, "fault proxy URL")
    return env


def run_step(
    step: Step,
    *,
    env: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    result = runner(
        list(step.command),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    require(result.returncode == 0, f"{step.name}: producer blocked")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    require(len(lines) == 1, f"{step.name}: producer output must be one JSON record")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise TerminalDayError(f"{step.name}: producer output is not JSON") from exc
    require(type(payload) is dict, f"{step.name}: producer output must be an object")
    require(payload.get("decision") == step.expected_decision,
            f"{step.name}: producer decision mismatch")
    return payload


def ensure_day_started(campaign_dir: Path, day: int) -> str:
    state = evidence.lifecycle_state(campaign_dir)
    require(state["terminal_failure"] is None, "campaign contains terminal day failure")
    if state["active_day"] is not None:
        require(
            state["active_day"] == day and state["frontier_sequence"] == day - 1,
            "another logical day is already active",
        )
        return "resumed"

    status = evidence.inspect(campaign_dir)
    require(status.get("decision") == "READY", "campaign frontier is not ready")
    frontier = status["frontier_sequence"]
    if frontier == day:
        return "already-completed"
    require(frontier == day - 1, "requested day is not the next campaign day")
    next_day = status.get("next_day")
    require(
        isinstance(next_day, dict)
        and next_day.get("logical_day_sequence") == day,
        "campaign next-day plan differs from requested day",
    )
    evidence.append_event(campaign_dir, day, "day-started", {})
    return "started"


def _execute_locked(
    config: TerminalConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    identity = load_identity(config.campaign_dir)
    start_mode = ensure_day_started(config.campaign_dir, config.day)
    if start_mode == "already-completed":
        return {
            "decision": "DAY_ALREADY_TERMINAL",
            "logical_day_sequence": config.day,
            "frontier_sequence": config.day,
            "campaign_acceptance_authorized": False,
        }

    env = subprocess_environment(config)
    completed_steps = []
    for step in build_steps(config, identity):
        run_step(step, env=env, runner=runner)
        completed_steps.append(step.name)

    terminal = evidence.materialize_terminal_day(config.campaign_dir, config.day)
    require(
        terminal.get("decision") == "DAY_TERMINAL_SUCCESS_INDEPENDENTLY_VERIFIED",
        "independent terminal materialization did not succeed",
    )
    return {
        **terminal,
        "start_mode": start_mode,
        "completed_steps": completed_steps,
    }


def execute(
    config: TerminalConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    config = validate_config(config)
    lock_path = config.campaign_dir / ".terminal-day.lock"
    require(not lock_path.is_symlink(), "terminal controller lock path must not be a symlink")
    with lock_path.open("a+b") as controller_lock:
        fcntl.flock(controller_lock, fcntl.LOCK_EX)
        return _execute_locked(config, runner=runner)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="#318 minimal terminal-day sequencer; campaign acceptance remains separate"
    )
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--online-evidence-root", type=Path, required=True)
    parser.add_argument("--reconciliation-workload-binary", type=Path, required=True)
    parser.add_argument("--provider-account-id", required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--provider-url", required=True)
    parser.add_argument("--merchant-url", required=True)
    parser.add_argument("--killbill-url", required=True)
    parser.add_argument("--fault-proxy-base-url", required=True)
    parser.add_argument("--fault-control-timeout-seconds", type=float, required=True)
    parser.add_argument("--provider-control-timeout-seconds", type=float, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--data-path", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--compose-env-file", type=Path)
    parser.add_argument("--recovery-crash-evidence", type=Path)
    parser.add_argument("--reconciliation-crash-evidence", type=Path)
    parser.add_argument("--day", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = execute(TerminalConfig(
            campaign_dir=args.campaign_dir.absolute(),
            online_evidence_root=args.online_evidence_root.absolute(),
            reconciliation_workload_binary=args.reconciliation_workload_binary.absolute(),
            provider_account_id=args.provider_account_id,
            gateway_url=args.gateway_url,
            provider_url=args.provider_url,
            merchant_url=args.merchant_url,
            killbill_url=args.killbill_url,
            fault_proxy_base_url=args.fault_proxy_base_url,
            fault_control_timeout_seconds=args.fault_control_timeout_seconds,
            provider_control_timeout_seconds=args.provider_control_timeout_seconds,
            repo_root=args.repo_root.absolute(),
            data_path=args.data_path.absolute(),
            compose_file=args.compose_file.absolute(),
            compose_env_file=args.compose_env_file.absolute() if args.compose_env_file else None,
            recovery_crash_evidence=args.recovery_crash_evidence.absolute() if args.recovery_crash_evidence else None,
            reconciliation_crash_evidence=args.reconciliation_crash_evidence.absolute() if args.reconciliation_crash_evidence else None,
            day=args.day,
        ))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        TerminalDayError,
        core.EvidenceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
