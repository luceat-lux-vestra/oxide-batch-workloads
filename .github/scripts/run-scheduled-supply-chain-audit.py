#!/usr/bin/env python3

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DISCOVER = ROOT / ".github" / "scripts" / "discover-supply-chain-workloads.py"
VALIDATE = ROOT / ".github" / "scripts" / "validate-supply-chain.py"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
POLICY_FAILURE = re.compile(r"\b(advisories|licenses|bans|sources)\s+FAILED\b")


def result(classification, details, workloads=None):
    return {
        "schema_version": 1,
        "classification": classification,
        "workloads": workloads or [],
        "details": details,
    }


def strip_ansi(value):
    return ANSI_ESCAPE.sub("", value)


def run_command(command):
    return subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def run_live_audit():
    discovered = run_command([sys.executable, str(DISCOVER)])
    if discovered.returncode != 0:
        return result("infrastructure-failure", "canonical workload discovery failed:\n" + discovered.stdout)
    try:
        matrix = json.loads(discovered.stdout)
        entries = matrix["include"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return result("infrastructure-failure", f"canonical workload discovery returned malformed output: {exc}\n{discovered.stdout}")
    if not entries:
        return result("infrastructure-failure", "canonical workload discovery returned zero real workloads")

    scanned = []
    policy_findings = []
    infrastructure_failures = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            infrastructure_failures.append(f"malformed workload entry from canonical discovery: {entry!r}")
            continue
        completed = run_command([sys.executable, str(VALIDATE), "--workload", name])
        scanned.append(name)
        if completed.returncode == 0:
            continue
        output = completed.stdout.strip()
        normalized_output = strip_ansi(output)
        failed_checks = sorted(set(POLICY_FAILURE.findall(normalized_output)))
        if failed_checks:
            policy_findings.append({"workload": name, "checks": failed_checks, "output": normalized_output})
        else:
            infrastructure_failures.append(
                f"workload {name!r} scan failed without a confirmed cargo-deny policy verdict "
                f"(exit {completed.returncode}):\n{normalized_output}"
            )

    if infrastructure_failures:
        details = "\n\n".join(infrastructure_failures)
        if policy_findings:
            details += "\n\nConfirmed policy findings were also observed, but infrastructure failure prevents a complete audit:\n"
            details += json.dumps(policy_findings, indent=2)
        return result("infrastructure-failure", details, scanned)
    if policy_findings:
        return result("policy-finding", json.dumps(policy_findings, indent=2), scanned)
    return result("clean", "all registered real workload graphs passed the canonical supply-chain policy", scanned)


def synthetic_result(mode):
    if mode == "policy-finding":
        return result("policy-finding", "synthetic safe test: confirmed policy finding", ["synthetic-workload"])
    if mode == "infrastructure-failure":
        return result("infrastructure-failure", "synthetic safe test: runner/tooling failure", ["synthetic-workload"])
    if mode == "clean":
        return result("clean", "synthetic safe test: clean recovery", ["synthetic-workload"])
    raise ValueError(f"unknown test mode: {mode}")


def exit_code_for(classification):
    return {"clean": 0, "policy-finding": 1, "infrastructure-failure": 2}[classification]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--test-mode", choices=("live", "policy-finding", "infrastructure-failure", "clean"), default="live")
    args = parser.parse_args()

    audit = run_live_audit() if args.test_mode == "live" else synthetic_result(args.test_mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, indent=2))
    return exit_code_for(audit["classification"])


if __name__ == "__main__":
    raise SystemExit(main())
