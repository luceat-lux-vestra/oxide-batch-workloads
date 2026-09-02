#!/usr/bin/env python3
"""Write one trusted, normalized shard result JSON file.

Identity (workload name, stage) always comes from the caller's own trusted
arguments (matrix data derived from the validated registry) -- never from
anything a workload's own `ci/validate` script prints. This is what makes a
workload's self-reported success/failure untrustable-by-design: the central
workflow decides status purely from the process exit code it observed.

`build_result` is the pure, unit-tested core; `main` is a thin argparse
wrapper around it.
"""

import argparse
import json
from pathlib import Path


def build_result(
    *,
    workload: str,
    stage: str,
    exit_code: int | None = None,
    not_applicable: bool = False,
    reason: str | None = None,
) -> dict:
    if not_applicable and exit_code is not None:
        raise ValueError("--exit-code and --not-applicable are mutually exclusive")
    if not not_applicable and exit_code is None:
        raise ValueError("one of --exit-code or --not-applicable is required")

    if not_applicable:
        # A not-applicable disposition only ever means "this workload
        # declares no MSRV policy" -- it is meaningless for the ci and
        # supply-chain stages, where every workload/fixture (ci) or every
        # real workload (supply-chain) must actually be validated.
        if stage != "msrv":
            raise ValueError("--not-applicable is only valid for --stage msrv")
        if not reason or not reason.strip():
            raise ValueError("--reason is required and must be non-empty with --not-applicable")
        return {
            "workload": workload,
            "stage": stage,
            "status": "success",
            "outcome": "not-applicable",
            "reason": reason,
        }

    return {
        "workload": workload,
        "stage": stage,
        "status": "success" if exit_code == 0 else "failure",
        "outcome": "validated",
        "exit_code": exit_code,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--stage", required=True, choices=["ci", "msrv", "supply-chain"])
    parser.add_argument("--out", required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--exit-code", type=int, help="observed exit code of the contract invocation")
    group.add_argument(
        "--not-applicable",
        action="store_true",
        help="emit an explicit not-applicable disposition instead of invoking the contract (msrv-not-declared policy)",
    )
    parser.add_argument("--reason", help="required with --not-applicable: the registry's policy_reason")
    args = parser.parse_args()

    try:
        result = build_result(
            workload=args.workload,
            stage=args.stage,
            exit_code=args.exit_code,
            not_applicable=args.not_applicable,
            reason=args.reason,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result), encoding="utf-8")
    print(f"wrote shard result: {result}")


if __name__ == "__main__":
    main()
