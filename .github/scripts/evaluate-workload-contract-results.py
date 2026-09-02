#!/usr/bin/env python3

import argparse
import json
from collections import Counter
from pathlib import Path


class EvaluationError(ValueError):
    pass


def fail(message: str) -> None:
    raise EvaluationError(message)


def load_results(results_dir: Path, contract: str) -> list[dict[str, str]]:
    if not results_dir.exists():
        return []
    results: list[dict[str, str]] = []
    for file in sorted(results_dir.glob(f"{contract}-*.json")):
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(f"invalid result artifact {file.name}: {exc}")
        if not isinstance(record, dict):
            fail(f"invalid result artifact {file.name}: root must be object")
        workload = record.get("workload")
        status = record.get("status")
        artifact_contract = record.get("contract")
        if not isinstance(workload, str) or not workload:
            fail(f"invalid result artifact {file.name}: missing workload")
        if artifact_contract != contract:
            fail(f"invalid result artifact {file.name}: contract mismatch {artifact_contract!r}")
        if status not in {"success", "failure", "cancelled", "skipped"}:
            fail(f"invalid result artifact {file.name}: unsupported status {status!r}")
        results.append({"workload": workload, "status": status})
    return results


def evaluate(
    *,
    contract: str,
    expected_workloads: list[str],
    results: list[dict[str, str]],
    discovery_result: str,
    fanout_result: str,
) -> None:
    if discovery_result != "success":
        fail(f"registry/contract discovery did not succeed: {discovery_result}")

    if contract == "ci" and not expected_workloads:
        fail("expected workload set is empty for ci contract")

    if contract not in {"ci", "msrv"}:
        fail(f"unsupported contract type: {contract!r}")

    if contract == "msrv" and not expected_workloads:
        if fanout_result not in {"success", "skipped"}:
            fail(f"msrv fan-out had unexpected result with empty expected set: {fanout_result}")
        return

    if fanout_result == "skipped":
        fail("fan-out job was skipped unexpectedly")
    if fanout_result in {"failure", "cancelled"}:
        fail(f"fan-out job did not succeed: {fanout_result}")
    if fanout_result != "success":
        fail(f"fan-out job has unsupported result: {fanout_result}")

    actual_workloads = [record["workload"] for record in results]
    counts = Counter(actual_workloads)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        fail("duplicate shard result(s): " + ", ".join(duplicates))

    expected_set = set(expected_workloads)
    actual_set = set(actual_workloads)
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    if missing:
        fail("missing shard result(s): " + ", ".join(missing))
    if extra:
        fail("unexpected shard result(s): " + ", ".join(extra))

    by_name = {record["workload"]: record["status"] for record in results}
    not_success = sorted(name for name in expected_workloads if by_name.get(name) != "success")
    if not_success:
        rendered = ", ".join(f"{name}={by_name.get(name)}" for name in not_success)
        fail(f"non-success shard result(s): {rendered}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, choices=("ci", "msrv"))
    parser.add_argument("--expected", required=True, help="JSON array of expected workload names")
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--discovery-result", required=True)
    parser.add_argument("--fanout-result", required=True)
    args = parser.parse_args()

    try:
        expected_workloads = json.loads(args.expected)
        if not isinstance(expected_workloads, list) or any(not isinstance(item, str) for item in expected_workloads):
            fail("--expected must be a JSON array of strings")
        results = load_results(Path(args.results_dir), args.contract)
        evaluate(
            contract=args.contract,
            expected_workloads=expected_workloads,
            results=results,
            discovery_result=args.discovery_result,
            fanout_result=args.fanout_result,
        )
    except EvaluationError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc

    print(
        f"{args.contract} aggregate passed: expected={len(expected_workloads)} "
        f"results={len(results)} discovery={args.discovery_result} fanout={args.fanout_result}"
    )


if __name__ == "__main__":
    main()
