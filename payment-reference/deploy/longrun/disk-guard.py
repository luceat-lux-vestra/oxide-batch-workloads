#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

DEFAULT_MIN_FREE_PERCENT = 20
DEFAULT_MIN_FREE_BYTES = 20 * 1024 * 1024 * 1024
BLOCK_EXIT = 75
ERROR_EXIT = 70


def evaluate(total_bytes: int, free_bytes: int, min_free_percent: int, min_free_bytes: int) -> dict[str, object]:
    if total_bytes <= 0:
        raise ValueError("total_bytes must be positive")
    if free_bytes < 0 or free_bytes > total_bytes:
        raise ValueError("free_bytes must be within [0,total_bytes]")
    if not 0 <= min_free_percent <= 100:
        raise ValueError("min_free_percent must be within [0,100]")
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes must be nonnegative")

    percent_ok = free_bytes * 100 >= total_bytes * min_free_percent
    absolute_ok = free_bytes >= min_free_bytes
    decision = "ALLOW" if percent_ok and absolute_ok else "BLOCK"
    return {
        "schema": "oxide-batch-workloads.payment-reference-disk-guard",
        "schema_version": 1,
        "decision": decision,
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "minimum_free_percent": min_free_percent,
        "minimum_free_bytes": min_free_bytes,
        "percent_ok": percent_ok,
        "absolute_ok": absolute_ok,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fail-closed disk guard for payment-reference long-running evidence/data roots")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--minimum-free-percent", type=int, default=DEFAULT_MIN_FREE_PERCENT)
    parser.add_argument("--minimum-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        args.path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(args.path)
        result = evaluate(
            usage.total,
            usage.free,
            args.minimum_free_percent,
            args.minimum_free_bytes,
        )
    except (OSError, ValueError) as exc:
        print(f"disk guard error: {exc}", file=sys.stderr)
        return ERROR_EXIT

    print(json.dumps(result, sort_keys=True))
    print(result["decision"])
    return 0 if result["decision"] == "ALLOW" else BLOCK_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
