#!/usr/bin/env python3
"""Independent compact proof for one retained payment-reference quiescence window."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import campaign_core as core
import campaign_quiescence as quiescence

SCHEMA = "oxide-batch-workloads.payment-reference-day-quiescence-verification"
SCHEMA_VERSION = 1
DECISION = "QUIESCENCE_WINDOW_VERIFIED_NOT_DAY_ACCEPTED"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise core.EvidenceError(message)


def file_sha256() -> str:
    return core.sha256(Path(__file__).resolve())


def json_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def artifact_from_records(
    *,
    plan: dict[str, Any],
    campaign_id: str,
    source_revision: str,
    expected_services: list[str],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    contract = quiescence.load_contract()
    contract_sha = core.sha256(quiescence.CONTRACT_PATH)
    subject = quiescence.verify_window(
        records,
        campaign_id=campaign_id,
        sequence=plan["logical_day_sequence"],
        source_revision=source_revision,
        contract=contract,
        contract_sha256=contract_sha,
        expected_services=expected_services,
    )
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "campaign_id": campaign_id,
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_revision,
        "day_plan_sha256": json_sha256(plan),
        "quiescence_contract_sha256": contract_sha,
        "verifier_semantics_sha256": file_sha256(),
        "window_records_sha256": json_sha256(records),
        "window_subject": subject,
    }


def collect(
    *,
    campaign_dir: Path,
    plan: dict[str, Any],
    campaign_id: str,
    source_revision: str,
    expected_services: list[str],
) -> dict[str, Any]:
    records = quiescence.read_window_records(
        campaign_dir,
        campaign_id=campaign_id,
        sequence=plan["logical_day_sequence"],
    )
    return artifact_from_records(
        plan=plan,
        campaign_id=campaign_id,
        source_revision=source_revision,
        expected_services=expected_services,
        records=records,
    )


def validate(
    artifact: Any,
    *,
    campaign_dir: Path,
    plan: dict[str, Any],
    source_revision: str,
    expected_services: list[str],
) -> dict[str, Any]:
    require(isinstance(artifact, dict), "quiescence proof must be object")
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "day_plan_sha256",
        "quiescence_contract_sha256", "verifier_semantics_sha256",
        "window_records_sha256", "window_subject",
    }
    require(set(artifact) == expected_fields, "quiescence proof fields mismatch")
    require(
        artifact["schema"] == SCHEMA
        and artifact["schema_version"] == SCHEMA_VERSION
        and artifact["decision"] == DECISION,
        "quiescence proof schema/decision mismatch",
    )
    require(
        artifact["campaign_id"] == campaign_dir.name
        and artifact["logical_day_sequence"] == plan["logical_day_sequence"]
        and artifact["source_revision"] == source_revision,
        "quiescence proof campaign/day/source mismatch",
    )
    require(
        artifact["day_plan_sha256"] == json_sha256(plan),
        "quiescence proof day-plan digest mismatch",
    )
    contract = quiescence.load_contract()
    contract_sha = core.sha256(quiescence.CONTRACT_PATH)
    require(
        artifact["quiescence_contract_sha256"] == contract_sha,
        "quiescence proof contract digest mismatch",
    )
    require(
        artifact["verifier_semantics_sha256"] == file_sha256(),
        "quiescence verifier semantics source mismatch",
    )
    records = quiescence.read_window_records(
        campaign_dir,
        campaign_id=campaign_dir.name,
        sequence=plan["logical_day_sequence"],
    )
    require(
        artifact["window_records_sha256"] == json_sha256(records),
        "quiescence retained window digest mismatch",
    )
    subject = quiescence.verify_window(
        records,
        campaign_id=campaign_dir.name,
        sequence=plan["logical_day_sequence"],
        source_revision=source_revision,
        contract=contract,
        contract_sha256=contract_sha,
        expected_services=expected_services,
    )
    require(
        artifact["window_subject"] == subject,
        "quiescence descriptor subject differs from retained window",
    )
    return {
        "window_id": subject["window_id"],
        "sample_count": subject["sample_count"],
        "first_ordinal": subject["first_ordinal"],
        "last_ordinal": subject["last_ordinal"],
        "observed_duration_nanos": subject["observed_duration_nanos"],
        "maximum_sample_gap_nanos": subject["maximum_sample_gap_nanos"],
        "window_records_sha256": artifact["window_records_sha256"],
        "quiescence_contract_sha256": contract_sha,
    }


def retain_exact(path: Path, artifact: dict[str, Any]) -> str:
    raw = core.canonical_bytes(artifact)
    if path.exists():
        require(path.is_file() and not path.is_symlink(), "quiescence proof path unsafe")
        require(path.read_bytes() == raw, "quiescence proof immutable conflict")
        return "replay"
    flags = 0
    fd = None
    import os
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return "created"


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 independent per-day quiescence proof")
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--day", type=int, required=True)
    args = parser.parse_args()
    try:
        campaign_dir = args.campaign_dir.resolve()
        record = core.load_json(campaign_dir / "campaign.json")
        identity = record.get("identity")
        require(isinstance(identity, dict), "campaign identity missing")
        campaign_id = identity.get("campaign_id")
        source_revision = identity.get("source_revision")
        images = identity.get("images")
        require(
            campaign_id == campaign_dir.name
            and isinstance(source_revision, str)
            and HEX40.fullmatch(source_revision) is not None
            and isinstance(images, dict)
            and isinstance(images.get("services"), dict),
            "campaign identity invalid for quiescence verification",
        )
        plan = core.plan_day(args.day)
        expected_services = sorted(images["services"])
        artifact = collect(
            campaign_dir=campaign_dir,
            plan=plan,
            campaign_id=campaign_id,
            source_revision=source_revision,
            expected_services=expected_services,
        )
        output = campaign_dir / "days" / f"day-{args.day:06}.quiescence.verification.json"
        retention = retain_exact(output, artifact)
        subject = validate(
            artifact,
            campaign_dir=campaign_dir,
            plan=plan,
            source_revision=source_revision,
            expected_services=expected_services,
        )
        print(json.dumps({
            "decision": DECISION,
            "verification_path": str(output),
            "retention": retention,
            "subject": subject,
        }, sort_keys=True))
        return 0
    except (core.EvidenceError, quiescence.QuiescenceError, OSError, ValueError, KeyError, TypeError) as exc:
        print(
            json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
