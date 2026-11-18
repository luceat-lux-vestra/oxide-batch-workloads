#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

SCHEMA = "oxide-batch-workloads.payment-reference-campaign-admission"
SCHEMA_VERSION = 1
EXIT_BLOCK = 75
EXIT_ERROR = 70


class AdmissionError(RuntimeError):
    pass


def load_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionError(f"unable to read contract {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdmissionError("qualification contract must be a JSON object")
    if value.get("schema_version") != 2:
        raise AdmissionError("qualification contract schema_version must be 2")
    admission = value.get("campaign_admission")
    if not isinstance(admission, dict):
        raise AdmissionError("qualification contract missing campaign_admission")
    return value


def read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    fields: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AdmissionError(f"unable to read {path}: {exc}") from exc
    for line in lines:
        name, _, rest = line.partition(":")
        if name not in wanted:
            continue
        parts = rest.strip().split()
        if len(parts) != 2 or parts[1] != "kB":
            raise AdmissionError(f"unexpected /proc/meminfo format for {name}")
        fields[name] = int(parts[0]) * 1024
    missing = sorted(wanted - set(fields))
    if missing:
        raise AdmissionError(f"/proc/meminfo missing fields: {missing}")
    return fields


def evaluate(
    contract: dict[str, Any],
    *,
    architecture: str,
    vcpu: int,
    memory_total_bytes: int,
    memory_available_bytes: int,
    swap_total_bytes: int,
    swap_free_bytes: int,
    disk_total_bytes: int,
    disk_free_bytes: int,
    data_path: str,
) -> dict[str, Any]:
    admission = contract["campaign_admission"]
    swap_used_bytes = max(0, swap_total_bytes - swap_free_bytes)
    reasons: list[str] = []

    if architecture not in contract["allowed_guest_architectures"]:
        reasons.append(
            f"architecture {architecture!r} not in {contract['allowed_guest_architectures']!r}"
        )
    if vcpu < contract["minimum_vcpu"]:
        reasons.append(f"vCPU {vcpu} < required {contract['minimum_vcpu']}")
    if memory_total_bytes < contract["minimum_memory_bytes"]:
        reasons.append(
            f"memory total {memory_total_bytes} < required {contract['minimum_memory_bytes']}"
        )
    if memory_available_bytes < admission["minimum_memory_available_bytes"]:
        reasons.append(
            f"MemAvailable {memory_available_bytes} < required "
            f"{admission['minimum_memory_available_bytes']}"
        )
    if swap_used_bytes > admission["maximum_swap_used_bytes"]:
        reasons.append(
            f"swap used {swap_used_bytes} > allowed {admission['maximum_swap_used_bytes']}"
        )
    if disk_total_bytes < contract["minimum_disk_total_bytes"]:
        reasons.append(
            f"data filesystem total {disk_total_bytes} < required "
            f"{contract['minimum_disk_total_bytes']}"
        )
    if disk_free_bytes < contract["minimum_disk_free_bytes"]:
        reasons.append(
            f"data filesystem free {disk_free_bytes} < required "
            f"{contract['minimum_disk_free_bytes']}"
        )

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "host_profile": contract["host_profile"],
        "data_path": data_path,
        "architecture": architecture,
        "vcpu": vcpu,
        "memory_total_bytes": memory_total_bytes,
        "memory_available_bytes": memory_available_bytes,
        "swap_total_bytes": swap_total_bytes,
        "swap_used_bytes": swap_used_bytes,
        "disk_total_bytes": disk_total_bytes,
        "disk_free_bytes": disk_free_bytes,
        "thresholds": {
            "minimum_memory_available_bytes": admission["minimum_memory_available_bytes"],
            "maximum_swap_used_bytes": admission["maximum_swap_used_bytes"],
            "minimum_disk_total_bytes": contract["minimum_disk_total_bytes"],
            "minimum_disk_free_bytes": contract["minimum_disk_free_bytes"],
        },
        "decision": "ALLOW" if not reasons else "BLOCK",
        "reasons": reasons,
    }


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Fail-closed pre-campaign admission gate for payment-reference #318"
    )
    parser.add_argument("--data-path", type=Path, required=True)
    parser.add_argument(
        "--contract",
        type=Path,
        default=here / "host-qualification-contract.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        contract = load_contract(args.contract.resolve())
        data_path = args.data_path.resolve()
        if not data_path.exists():
            raise AdmissionError(f"data path does not exist: {data_path}")
        mem = read_meminfo()
        disk = shutil.disk_usage(data_path)
        result = evaluate(
            contract,
            architecture=platform.machine().lower(),
            vcpu=os.cpu_count() or 0,
            memory_total_bytes=mem["MemTotal"],
            memory_available_bytes=mem["MemAvailable"],
            swap_total_bytes=mem["SwapTotal"],
            swap_free_bytes=mem["SwapFree"],
            disk_total_bytes=disk.total,
            disk_free_bytes=disk.free,
            data_path=str(data_path),
        )
    except (AdmissionError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "schema_version": SCHEMA_VERSION,
                    "decision": "ERROR",
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return EXIT_ERROR

    print(json.dumps(result, sort_keys=True))
    print(result["decision"])
    return 0 if result["decision"] == "ALLOW" else EXIT_BLOCK


if __name__ == "__main__":
    sys.exit(main())
