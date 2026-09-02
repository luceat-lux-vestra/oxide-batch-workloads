#!/usr/bin/env python3
"""Write one trusted, normalized shard result JSON file.

Identity (workload name, stage) always comes from the caller's own trusted
arguments (matrix data derived from the validated registry) -- never from
anything a workload's own `ci/validate` script prints. This is what makes a
workload's self-reported success/failure untrustable-by-design: the central
workflow decides status purely from the process exit code it observed.
"""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--stage", required=True, choices=["ci", "msrv"])
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

    if args.not_applicable:
        if not args.reason or not args.reason.strip():
            raise SystemExit("--reason is required and must be non-empty with --not-applicable")
        result = {
            "workload": args.workload,
            "stage": args.stage,
            "status": "success",
            "outcome": "not-applicable",
            "reason": args.reason,
        }
    else:
        result = {
            "workload": args.workload,
            "stage": args.stage,
            "status": "success" if args.exit_code == 0 else "failure",
            "outcome": "validated",
            "exit_code": args.exit_code,
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result), encoding="utf-8")
    print(f"wrote shard result: {result}")


if __name__ == "__main__":
    main()
