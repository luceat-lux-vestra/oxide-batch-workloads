#!/usr/bin/env python3
"""Pure deterministic campaign contract and day-planning core for #318.

This module owns no runtime mutation, evidence journal, network, subprocess, or
settlement behavior. It is the shared source of campaign contract validation,
logical-day identity, and small canonical hashing helpers used by CLI adapters.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from typing import Any

CONTRACT_PATH = Path(__file__).with_name("campaign-contract-v1.json")
CAMPAIGN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class EvidenceError(ValueError):
    """Compatibility error type for frozen #318 campaign-contract violations."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path}: expected JSON object")
    return value


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256(path: Path) -> str:
    return digest(path.read_bytes())


def contract() -> dict[str, Any]:
    value = load_json(CONTRACT_PATH)
    require(value.get("schema") == "oxide-batch-workloads.payment-reference-campaign-contract", "wrong contract schema")
    require(value.get("schema_version") == 1, "wrong contract version")
    require(value.get("contract") == "payment-reference-longrun-v1", "wrong contract identity")
    require(type(value.get("base_seed")) is int and value["base_seed"] == 20261118, "wrong base seed")
    require(sum(value["traffic"].values()) == value["primary_lifecycles_per_day"] == 100, "wrong daily traffic geometry")
    require(value["traffic"] == {"auth_capture_purchase": 40, "authorize_capture": 25, "authorize_cancel": 10, "auth_capture_refund": 15, "fault_overlay": 10}, "wrong canonical traffic")
    require(value["crash_schedule_modulo_10"] == {"3": "reconciliation-during-write", "6": "reconciliation-after-business-commit", "8": "recovery-after-external-convergence-before-local-commit", "0": "reconciliation-finalize-after-commit"}, "wrong canonical crash schedule")
    require(value["acceptance"] == {"minimum_elapsed_seconds": 86400, "minimum_completed_logical_days": 30, "minimum_primary_lifecycles": 3000}, "wrong campaign acceptance floors")
    require((value["quiet_seconds_each_day"], value["quiet_seconds_every_tenth_day"], value["quiet_seconds_after_final_day"]) == (60, 600, 1800), "wrong quiet windows")
    require(value["first_logical_business_date"] == "2026-11-18", "wrong logical business date anchor")
    return value


def plan_day(sequence: int) -> dict[str, Any]:
    require(type(sequence) is int and 1 <= sequence <= 100000, "day sequence must be 1..100000")
    c = contract()
    epoch = dt.date.fromisoformat(c["first_logical_business_date"])
    day_seed = int.from_bytes(
        hashlib.sha256(f'{c["contract"]}:{c["base_seed"]}:{sequence}'.encode("ascii")).digest()[:8],
        "big",
    )
    return {
        "schema": "oxide-batch-workloads.payment-reference-day-plan",
        "schema_version": 1,
        "contract": c["contract"],
        "base_seed": c["base_seed"],
        "logical_day_sequence": sequence,
        "logical_business_date": (epoch + dt.timedelta(days=sequence - 1)).isoformat(),
        "day_seed": day_seed,
        "primary_lifecycles": 100,
        "traffic": c["traffic"],
        "fault_overlay_rotation_offset": (sequence - 1) % 5,
        "scheduled_hard_crash": c["crash_schedule_modulo_10"].get(str(sequence % 10)),
        "minimum_quiet_seconds": 600 if sequence % 10 == 0 else 60,
    }
