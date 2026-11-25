#!/usr/bin/env python3
"""#318 frozen campaign identity, deterministic day plan and fail-closed evidence scanner.

No online payment, settlement, recovery or batch workload is run by this module.
A completed day can only be claimed by a future independently verified runner.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any

import campaign_core as core
import campaign_day_cardinality as day_cardinality
import campaign_day_financial_invariants as day_financial
import campaign_day_reconciliation as day_reconciliation
import campaign_day_recovery as day_recovery
import campaign_day_online_verification as day_online
import campaign_day_quiescence_verification as day_quiescence

CONTRACT_PATH = core.CONTRACT_PATH
SCHEMA = "oxide-batch-workloads.payment-reference-campaign"
EVENT_SCHEMA = "oxide-batch-workloads.payment-reference-campaign-event"
DAY_SCHEMA = "oxide-batch-workloads.payment-reference-campaign-day"
SCHEMA_VERSION = 1
CAMPAIGN_PHASES = {"campaign-initialized", "controller-started", "controller-stopped"}
DAY_PHASES = {
    "day-started", "traffic", "fault", "batch", "crash", "recovery",
    "reconciliation", "statement", "close", "quiescence",
    "day-terminal-success", "day-terminal-failure",
}
ARTIFACTS = ("host.json", "runtime.json", "container-images.json", "toolchains.json")
BINARIES = ("postgres-postgres", "payment-reconciliation-postgres", "payment-recovery-postgres")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
CAMPAIGN_ID = core.CAMPAIGN_ID
DAY_VERIFICATION_SCHEMA = "oxide-batch-workloads.payment-reference-day-independent-verification"
DAY_VERIFICATION_SCHEMA_VERSION = 1
FINANCIAL_CHECK = "financial_invariants"
ONLINE_CHECK = "canonical_online"
QUIESCENCE_CHECK = "quiescence"
RECONCILIATION_CHECK = "reconciliation"
RECOVERY_CHECK = "recovery"
SETTLEMENT_CHECKS = {"settlement_roster", "settlement_close"}
CARDINALITY_CHECK = "side_effect_cardinality"
SETTLEMENT_ROSTER_SCHEMA = "oxide-batch-workloads.payment-reference-settlement-roster-verification"
SETTLEMENT_VERIFICATION_SCHEMA = "oxide-batch-workloads.payment-reference-settlement-verification"
DIGEST64 = re.compile(r"sha256:[0-9a-f]{64}\Z")
STATEMENT_ID = re.compile(r"statement-[0-9a-f]{64}\Z")
CURRENCY = re.compile(r"[A-Z]{3}\Z")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


EvidenceError = core.EvidenceError
require = core.require
load_json = core.load_json
canonical_bytes = core.canonical_bytes
digest = core.digest
sha256 = core.sha256
contract = core.contract
plan_day = core.plan_day

def verify_identity(identity: dict[str, Any]) -> None:
    require(set(identity) == {"campaign_id", "source_revision", "oxide_batch", "batch_binaries", "images", "provenance_sha256"}, "campaign identity fields mismatch")
    require(isinstance(identity["campaign_id"], str) and CAMPAIGN_ID.fullmatch(identity["campaign_id"]) is not None, "unsafe campaign id")
    require(isinstance(identity["source_revision"], str) and HEX40.fullmatch(identity["source_revision"]) is not None, "source SHA must be exact")
    oxide = identity["oxide_batch"]
    require(isinstance(oxide, dict) and set(oxide) == {"version", "registry", "lockfile_sha256", "artifact_sha256"}, "OxideBatch identity fields mismatch")
    require(oxide["version"] == "0.7.0" and oxide["registry"] == "crates.io", "OxideBatch must be published 0.7.0")
    for name in ("lockfile_sha256", "artifact_sha256"):
        require(isinstance(oxide[name], str) and HEX64.fullmatch(oxide[name]) is not None, f"invalid {name}")
    binaries = identity["batch_binaries"]
    require(isinstance(binaries, dict) and set(binaries) == set(BINARIES), "batch binary set mismatch")
    for name, b in binaries.items():
        require(isinstance(b, dict) and set(b) == {"sha256", "size_bytes"}, f"{name}: binary identity mismatch")
        require(isinstance(b["sha256"], str) and HEX64.fullmatch(b["sha256"]) is not None, f"{name}: bad SHA")
        require(type(b["size_bytes"]) is int and b["size_bytes"] > 0, f"{name}: bad binary size")
    images = identity["images"]
    require(isinstance(images, dict) and set(images) == {"manifest_sha256", "compose_sha256", "services"}, "image identity fields mismatch")
    for key in ("manifest_sha256", "compose_sha256"):
        require(isinstance(images[key], str) and HEX64.fullmatch(images[key]) is not None, f"bad {key}")
    services = images["services"]
    expected = {"merchant", "gateway", "killbill", "merchant-db", "gateway-db", "killbill-db", "payment-sandbox", "payment-sandbox-fault-proxy"}
    require(isinstance(services, dict) and set(services) == expected, "wrong image service set")
    for name, ref in services.items():
        require(isinstance(ref, str) and re.fullmatch(r"ghcr\.io/[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}", ref) is not None, f"{name}: image reference not immutable")
    hashes = identity["provenance_sha256"]
    require(isinstance(hashes, dict) and set(hashes) == set(ARTIFACTS), "wrong provenance file set")
    require(all(isinstance(x, str) and HEX64.fullmatch(x) for x in hashes.values()), "bad provenance hash")


def validate_provenance(identity: dict[str, Any], documents: dict[str, Any]) -> None:
    for name, document in documents.items():
        require(isinstance(document, dict) and document.get("repository_sha") == identity["source_revision"], f"{name}: repository SHA does not match")
    images = documents["container-images.json"].get("images")
    require(isinstance(images, list) and len(images) == 8, "provenance must contain 8 service images")
    refs = {}
    for value in images:
        require(isinstance(value, dict) and isinstance(value.get("service"), str) and isinstance(value.get("image_ref"), str), "malformed image provenance")
        require(value["service"] not in refs, "duplicate service in image provenance")
        refs[value["service"]] = value["image_ref"]
    require(refs == identity["images"]["services"], "images differ from immutable provenance")
    runtime = documents["runtime.json"]
    require(sorted(runtime.get("compose_services", [])) == sorted(refs), "runtime compose service identity mismatch")


def create_only(path: Path, raw: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    sync_dir(path.parent)


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize(root: Path, identity: dict[str, Any], provenance_dir: Path) -> Path:
    """Create-only skeleton: no campaign execution or acceptance implied."""
    verify_identity(identity)
    c = contract()
    source_bytes = {}
    for name in ARTIFACTS:
        src = provenance_dir / name
        raw = src.read_bytes()
        require(digest(raw) == identity["provenance_sha256"][name], f"provenance digest mismatch: {name}")
        source_bytes[name] = raw
    validate_provenance(identity, {name: json.loads(raw) for name, raw in source_bytes.items()})
    campaign_path = root / identity["campaign_id"]
    campaign_record = {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
        "contract": c["contract"], "contract_sha256": sha256(CONTRACT_PATH),
        "identity": identity, "acceptance": c["acceptance"],
        "traffic": c["traffic"], "crash_schedule_modulo_10": c["crash_schedule_modulo_10"],
    }
    campaign_path.mkdir(parents=True, exist_ok=False, mode=0o700)
    for relative in ("provenance", "days", "statements", "failures"):
        (campaign_path / relative).mkdir(mode=0o700)
    for name, raw in source_bytes.items():
        create_only(campaign_path / "provenance" / name, raw)
    create_only(campaign_path / "campaign.json", canonical_bytes(campaign_record))
    create_only(campaign_path / "samples.jsonl", b"")
    create_only(campaign_path / "events.jsonl", b"")
    append_event(campaign_path, 0, "campaign-initialized", {"contract_sha256": sha256(CONTRACT_PATH)})
    sync_dir(campaign_path)
    return campaign_path


def validate_event(event: Any, ordinal: int, campaign_id: str) -> None:
    require(isinstance(event, dict) and set(event) == {"schema", "schema_version", "ordinal", "campaign_id", "logical_day_sequence", "phase", "detail"}, f"event {ordinal}: wrong fields")
    require(event["schema"] == EVENT_SCHEMA and event["schema_version"] == 1, f"event {ordinal}: wrong schema")
    require(type(event["ordinal"]) is int and event["ordinal"] == ordinal and event["campaign_id"] == campaign_id, f"event {ordinal}: ordinal/campaign mismatch")
    require(type(event["logical_day_sequence"]) is int and event["logical_day_sequence"] >= 0, f"event {ordinal}: bad day index")
    require(event["phase"] in CAMPAIGN_PHASES | DAY_PHASES, f"event {ordinal}: unknown phase")
    require(isinstance(event["detail"], dict), f"event {ordinal}: detail must be JSON object")


def iter_events(path: Path, campaign_id: str):
    """Stream bounded-size JSONL records without loading a long-run journal."""
    with path.open("rb") as source:
        ordinal = 0
        while raw := source.readline(65537):
            ordinal += 1
            require(len(raw) <= 65536 and raw.endswith(b"\n"), f"event {ordinal}: oversized or torn JSONL line")
            event = json.loads(raw)
            validate_event(event, ordinal, campaign_id)
            yield event


def read_events(path: Path, campaign_id: str) -> list[dict[str, Any]]:
    """Small-fixture compatibility helper; production scan uses iter_events."""
    return list(iter_events(path, campaign_id))


def last_event_ordinal(file, campaign_id: str) -> int:
    """Read only the bounded terminal record under the append writer lock."""
    file.seek(0, os.SEEK_END)
    size = file.tell()
    if size == 0:
        return 0
    window = min(size, 65536)
    file.seek(size - window)
    tail = file.read(window)
    require(tail.endswith(b"\n"), "truncated JSONL terminal line")
    cut = tail.rfind(b"\n", 0, len(tail) - 1)
    require(cut >= 0 or size <= window, "oversized JSONL terminal line")
    last = tail[cut + 1:-1]
    require(len(last) <= 65535, "oversized JSONL terminal line")
    event = json.loads(last)
    require(isinstance(event, dict) and type(event.get("ordinal")) is int and event["ordinal"] > 0, "invalid last event ordinal")
    validate_event(event, event["ordinal"], campaign_id)
    return event["ordinal"]


def append_event(campaign_path: Path, sequence: int, phase: str, detail: dict[str, Any]) -> None:
    """Append and fsync one bounded event; full scan blocks inconsistent history."""
    require(phase in CAMPAIGN_PHASES | DAY_PHASES, "unrecognized event phase")
    require(type(sequence) is int and sequence >= 0, "invalid event day index")
    require(isinstance(detail, dict), "event detail must be an object")
    campaign_id = campaign_path.name
    path = campaign_path / "events.jsonl"
    with path.open("r+b") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        next_ordinal = last_event_ordinal(file, campaign_id) + 1
        event = {"schema": EVENT_SCHEMA, "schema_version": 1, "ordinal": next_ordinal,
                 "campaign_id": campaign_id, "logical_day_sequence": sequence,
                 "phase": phase, "detail": detail}
        raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        require(len(raw) <= 65536, "event record exceeds 64KiB")
        file.seek(0, os.SEEK_END)
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())


def _day_files(campaign_path: Path) -> tuple[dict[int, Path], dict[int, Path]]:
    days = {}
    verifications = {}
    for path in (campaign_path / "days").iterdir():
        require(path.is_file() and not path.is_symlink(), f"unsafe day evidence path: {path}")
        if re.fullmatch(r"[0-9]{6}\.json", path.name):
            seq = int(path.stem)
            require(seq > 0, "day zero terminal file invalid")
            days[seq] = path
        elif re.fullmatch(r"[0-9]{6}\.verification\.json", path.name):
            seq = int(path.name[:6])
            require(seq > 0, "day zero verification file invalid")
            verifications[seq] = path
        elif re.fullmatch(r"day-[0-9]{6}\.(?:recovery|reconciliation|financial-invariants|online|quiescence)\.verification\.json", path.name):
            seq = int(path.name[4:10])
            require(seq > 0, "day zero auxiliary verification file invalid")
            # Auxiliary proofs are validated only through their exact bound
            # descriptors in the day-independent verification artifact.
            continue
        else:
            raise EvidenceError(f"unexpected day evidence path: {path}")
    return days, verifications


def compact_json_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _safe_relative_artifact(
    campaign_path: Path,
    relative_path: Any,
    expected_name: str,
    check: str,
) -> Path:
    require(isinstance(relative_path, str) and relative_path, f"{check}: artifact path missing")
    require("\\" not in relative_path, f"{check}: artifact path must use POSIX separators")
    posix = PurePosixPath(relative_path)
    require(
        not posix.is_absolute()
        and str(posix) == relative_path
        and all(part not in ("", ".", "..") for part in posix.parts),
        f"{check}: unsafe artifact relative path",
    )
    require(posix.name == expected_name, f"{check}: artifact filename does not match logical day")

    cursor = campaign_path
    for part in posix.parts:
        cursor = cursor / part
        require(not cursor.is_symlink(), f"{check}: artifact path contains symlink")
    require(cursor.is_file(), f"{check}: artifact file missing")

    root = campaign_path.resolve(strict=True)
    resolved = cursor.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise EvidenceError(f"{check}: artifact escapes campaign directory") from exc
    return cursor


def _roster_subject(artifact: dict[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "day_plan_sha256",
        "plan_contract_sha256", "fault_catalog_sha256", "provider_account_id",
        "settlement_date", "currency", "observed_slot_count",
        "eligible_payment_count", "roster_sha256", "observations",
    }
    require(set(artifact) == expected_fields, "settlement_roster: retained artifact fields mismatch")
    for name in ("day_plan_sha256", "plan_contract_sha256", "fault_catalog_sha256"):
        value = artifact.get(name)
        require(isinstance(value, str) and HEX64.fullmatch(value), f"settlement_roster: invalid {name}")

    roster_sha = artifact["roster_sha256"]
    account = artifact["provider_account_id"]
    settlement_date = artifact["settlement_date"]
    currency = artifact["currency"]
    eligible = artifact["eligible_payment_count"]
    observations = artifact["observations"]
    require(isinstance(roster_sha, str) and DIGEST64.fullmatch(roster_sha), "settlement_roster: invalid roster digest")
    require(isinstance(account, str) and account.strip(), "settlement_roster: provider account missing")
    require(isinstance(settlement_date, str) and ISO_DATE.fullmatch(settlement_date), "settlement_roster: settlement date invalid")
    require(isinstance(currency, str) and CURRENCY.fullmatch(currency), "settlement_roster: currency invalid")
    require(
        artifact["observed_slot_count"] == 100
        and isinstance(observations, list)
        and len(observations) == 100,
        "settlement_roster: retained artifact must cover exactly 100 slots",
    )
    require(type(eligible) is int and 0 < eligible <= 100, "settlement_roster: eligible payment count invalid")
    return {
        "roster_sha256": roster_sha,
        "provider_account_id": account,
        "settlement_date": settlement_date,
        "currency": currency,
        "eligible_payment_count": eligible,
    }


def _settlement_subject(artifact: dict[str, Any]) -> dict[str, Any]:
    require(
        set(artifact) == {
            "schema", "schema_version", "decision", "campaign_id",
            "logical_day_sequence", "source_revision", "roster_sha256",
            "roster_verification_sha256", "statement", "merchant_close",
        },
        "settlement_close: retained artifact fields mismatch",
    )
    roster_sha = artifact["roster_sha256"]
    roster_verification_sha = artifact["roster_verification_sha256"]
    summary = artifact["statement"]
    proof = artifact["merchant_close"]
    require(isinstance(roster_sha, str) and DIGEST64.fullmatch(roster_sha), "settlement_close: invalid roster digest")
    require(
        isinstance(roster_verification_sha, str)
        and DIGEST64.fullmatch(roster_verification_sha),
        "settlement_close: invalid roster-verification digest",
    )
    require(
        isinstance(summary, dict)
        and set(summary) == {
            "statement_contract_version", "statement_id", "statement_digest",
            "statement_artifact_sha256", "source_roster_digest",
            "provider_account_id", "settlement_date", "currency", "line_count",
            "captured_total_minor", "refunded_total_minor",
            "net_settlement_total_minor",
        },
        "settlement_close: statement summary fields mismatch",
    )
    require(
        summary["statement_contract_version"] == "provider-settlement-statement-v1",
        "settlement_close: statement contract version mismatch",
    )
    statement_id = summary["statement_id"]
    statement_digest = summary["statement_digest"]
    statement_artifact_sha = summary["statement_artifact_sha256"]
    account = summary["provider_account_id"]
    settlement_date = summary["settlement_date"]
    currency = summary["currency"]
    line_count = summary["line_count"]
    captured = summary["captured_total_minor"]
    refunded = summary["refunded_total_minor"]
    net = summary["net_settlement_total_minor"]
    require(isinstance(statement_id, str) and STATEMENT_ID.fullmatch(statement_id), "settlement_close: statement id invalid")
    require(isinstance(statement_digest, str) and DIGEST64.fullmatch(statement_digest), "settlement_close: statement digest invalid")
    require(isinstance(statement_artifact_sha, str) and DIGEST64.fullmatch(statement_artifact_sha), "settlement_close: statement artifact digest invalid")
    require(summary["source_roster_digest"] == roster_sha, "settlement_close: statement/roster digest mismatch")
    require(isinstance(account, str) and account.strip(), "settlement_close: provider account missing")
    require(isinstance(settlement_date, str) and ISO_DATE.fullmatch(settlement_date), "settlement_close: settlement date invalid")
    require(isinstance(currency, str) and CURRENCY.fullmatch(currency), "settlement_close: currency invalid")
    require(type(line_count) is int and line_count > 0, "settlement_close: line count invalid")
    require(
        type(captured) is int
        and type(refunded) is int
        and type(net) is int
        and captured >= 0
        and 0 <= refunded <= captured
        and net == captured - refunded,
        "settlement_close: statement totals invalid",
    )

    expected_row = {
        "statement_id": statement_id,
        "statement_digest": statement_digest,
        "provider_account_id": account,
        "settlement_date": settlement_date,
        "currency": currency,
        "line_count": line_count,
        "captured_total_minor": captured,
        "refunded_total_minor": refunded,
        "net_settlement_total_minor": net,
        "settlement_state": "CLOSED",
    }
    require(
        isinstance(proof, dict)
        and set(proof) == {
            "settlement_state", "persisted_row", "business_event_count",
            "exact_replay_http_status", "digest_conflict_http_status",
            "totals_conflict_http_status",
            "authoritative_state_unchanged_after_conflicts",
        },
        "settlement_close: merchant close proof fields mismatch",
    )
    require(
        proof["settlement_state"] == "CLOSED"
        and proof["persisted_row"] == expected_row
        and proof["business_event_count"] == 1
        and proof["exact_replay_http_status"] == 200
        and proof["digest_conflict_http_status"] == 409
        and proof["totals_conflict_http_status"] == 409
        and proof["authoritative_state_unchanged_after_conflicts"] is True,
        "settlement_close: merchant close proof mismatch",
    )
    return {
        "roster_sha256": roster_sha,
        "statement_id": statement_id,
        "statement_digest": statement_digest,
        "provider_account_id": account,
        "settlement_date": settlement_date,
        "currency": currency,
        "line_count": line_count,
        "captured_total_minor": captured,
        "refunded_total_minor": refunded,
        "net_settlement_total_minor": net,
    }


def _validate_bound_settlement_check(
    *,
    check: str,
    result: Any,
    campaign_path: Path,
    identity: dict[str, Any],
    sequence: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_schema = {
        "settlement_roster": SETTLEMENT_ROSTER_SCHEMA,
        "settlement_close": SETTLEMENT_VERIFICATION_SCHEMA,
    }[check]
    expected_name = {
        "settlement_roster": f"day-{sequence:06}.roster.verification.json",
        "settlement_close": f"day-{sequence:06}.settlement.verification.json",
    }[check]
    expected_decision = {
        "settlement_roster": "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED",
        "settlement_close": "SETTLEMENT_VERIFIED_NOT_DAY_ACCEPTED",
    }[check]
    expected_fields = {
        "verdict",
        "artifact_schema",
        "artifact_schema_version",
        "campaign_id",
        "logical_day_sequence",
        "source_revision",
        "relative_path",
        "artifact_sha256",
        "subject",
    }
    require(isinstance(result, dict) and set(result) == expected_fields, f"{check}: bound artifact descriptor fields mismatch")
    require(result["verdict"] == "PASS", f"{check}: verdict is not PASS")
    require(
        result["artifact_schema"] == expected_schema
        and type(result["artifact_schema_version"]) is int
        and result["artifact_schema_version"] == 1,
        f"{check}: artifact schema/version mismatch",
    )
    require(
        result["campaign_id"] == identity["campaign_id"]
        and type(result["logical_day_sequence"]) is int
        and result["logical_day_sequence"] == sequence
        and result["source_revision"] == identity["source_revision"],
        f"{check}: descriptor campaign/day/source mismatch",
    )
    require(
        isinstance(result["artifact_sha256"], str)
        and HEX64.fullmatch(result["artifact_sha256"]) is not None,
        f"{check}: artifact SHA invalid",
    )

    artifact_path = _safe_relative_artifact(
        campaign_path,
        result["relative_path"],
        expected_name,
        check,
    )
    require(sha256(artifact_path) == result["artifact_sha256"], f"{check}: artifact file SHA mismatch")
    artifact = load_json(artifact_path)
    require(
        artifact.get("schema") == expected_schema
        and type(artifact.get("schema_version")) is int
        and artifact.get("schema_version") == 1
        and artifact.get("decision") == expected_decision,
        f"{check}: retained artifact schema/decision mismatch",
    )
    require(
        artifact.get("campaign_id") == identity["campaign_id"]
        and type(artifact.get("logical_day_sequence")) is int
        and artifact.get("logical_day_sequence") == sequence
        and artifact.get("source_revision") == identity["source_revision"],
        f"{check}: retained artifact campaign/day/source mismatch",
    )
    subject = _roster_subject(artifact) if check == "settlement_roster" else _settlement_subject(artifact)
    require(result["subject"] == subject, f"{check}: descriptor subject does not match retained artifact")
    return artifact, subject


def validate_verification_record(
    record: Any,
    campaign_path: Path,
    identity: dict[str, Any],
    sequence: int,
) -> None:
    require(
        set(record) == {
            "schema", "schema_version", "campaign_id",
            "logical_day_sequence", "source_revision", "verdict", "checks",
        },
        "independent verification fields mismatch",
    )
    require(
        record["schema"] == DAY_VERIFICATION_SCHEMA
        and type(record["schema_version"]) is int
        and record["schema_version"] == DAY_VERIFICATION_SCHEMA_VERSION,
        "independent verification schema mismatch",
    )
    require(
        record["campaign_id"] == identity["campaign_id"]
        and type(record["logical_day_sequence"]) is int
        and record["logical_day_sequence"] == sequence
        and record["source_revision"] == identity["source_revision"]
        and record["verdict"] == "PASS",
        "independent verification identity/verdict mismatch",
    )
    checks = record["checks"]
    expected = SETTLEMENT_CHECKS | {
        CARDINALITY_CHECK, FINANCIAL_CHECK, RECONCILIATION_CHECK, RECOVERY_CHECK,
        ONLINE_CHECK, QUIESCENCE_CHECK,
    }
    require(isinstance(checks, dict) and set(checks) == expected, "missing verification coverage")

    roster_artifact, roster_subject = _validate_bound_settlement_check(
        check="settlement_roster",
        result=checks["settlement_roster"],
        campaign_path=campaign_path,
        identity=identity,
        sequence=sequence,
    )
    settlement_artifact, settlement_subject = _validate_bound_settlement_check(
        check="settlement_close",
        result=checks["settlement_close"],
        campaign_path=campaign_path,
        identity=identity,
        sequence=sequence,
    )

    require(
        settlement_artifact["roster_verification_sha256"]
        == compact_json_sha256(roster_artifact),
        "settlement_close: does not bind exact settlement_roster verification artifact",
    )
    for field in ("roster_sha256", "provider_account_id", "settlement_date", "currency"):
        require(
            settlement_subject[field] == roster_subject[field],
            f"settlement_close: {field} differs from settlement_roster",
        )
    require(
        settlement_subject["line_count"] == roster_subject["eligible_payment_count"],
        "settlement_close: statement line count differs from eligible roster membership",
    )

    check = checks[CARDINALITY_CHECK]
    name = f"day-{sequence:06}.provider-cardinality.verification.json"
    expected_fields = {
        "verdict", "artifact_schema", "artifact_schema_version", "campaign_id",
        "logical_day_sequence", "source_revision", "relative_path",
        "artifact_sha256", "subject",
    }
    require(type(check) is dict and set(check) == expected_fields,
            "side_effect_cardinality: bound descriptor fields mismatch")
    require(
        check["verdict"] == "PASS"
        and check["artifact_schema"] == day_cardinality.SCHEMA
        and type(check["artifact_schema_version"]) is int
        and check["artifact_schema_version"] == 1
        and check["campaign_id"] == identity["campaign_id"]
        and type(check["logical_day_sequence"]) is int
        and check["logical_day_sequence"] == sequence
        and check["source_revision"] == identity["source_revision"],
        "side_effect_cardinality: descriptor identity/schema/verdict mismatch",
    )
    require(isinstance(check["artifact_sha256"], str)
            and HEX64.fullmatch(check["artifact_sha256"]) is not None,
            "side_effect_cardinality: invalid artifact SHA")
    artifact_path = _safe_relative_artifact(
        campaign_path, check["relative_path"], name, CARDINALITY_CHECK,
    )
    require(sha256(artifact_path) == check["artifact_sha256"],
            "side_effect_cardinality: retained file SHA mismatch")
    artifact = load_json(artifact_path)
    subject = day_cardinality.validate(
        artifact,
        plan=day_cardinality.plan_for(identity["campaign_id"], sequence),
        source_revision=identity["source_revision"],
        roster_verification=roster_artifact,
    )
    require(check["subject"] == subject,
            "side_effect_cardinality: descriptor subject mismatches independently re-derived proof")

    reconciliation = checks[RECONCILIATION_CHECK]
    reconciliation_name = f"day-{sequence:06}.reconciliation.verification.json"
    require(type(reconciliation) is dict and set(reconciliation) == expected_fields,
            "reconciliation: bound descriptor fields mismatch")
    require(
        reconciliation["verdict"] == "PASS"
        and reconciliation["artifact_schema"] == day_reconciliation.SCHEMA
        and type(reconciliation["artifact_schema_version"]) is int
        and reconciliation["artifact_schema_version"] == day_reconciliation.SCHEMA_VERSION
        and reconciliation["campaign_id"] == identity["campaign_id"]
        and type(reconciliation["logical_day_sequence"]) is int
        and reconciliation["logical_day_sequence"] == sequence
        and reconciliation["source_revision"] == identity["source_revision"],
        "reconciliation: descriptor identity/schema/verdict mismatch",
    )
    require(
        isinstance(reconciliation["artifact_sha256"], str)
        and HEX64.fullmatch(reconciliation["artifact_sha256"]) is not None,
        "reconciliation: artifact SHA invalid",
    )
    reconciliation_path = _safe_relative_artifact(
        campaign_path,
        reconciliation["relative_path"],
        reconciliation_name,
        RECONCILIATION_CHECK,
    )
    require(sha256(reconciliation_path) == reconciliation["artifact_sha256"],
            "reconciliation: retained file SHA mismatch")
    reconciliation_artifact = load_json(reconciliation_path)
    reconciliation_subject = day_reconciliation.validate(
        reconciliation_artifact,
        plan=day_cardinality.plan_for(identity["campaign_id"], sequence),
        source_revision=identity["source_revision"],
        workload_binary_sha256=identity["batch_binaries"]["payment-reconciliation-postgres"]["sha256"],
    )
    require(reconciliation["subject"] == reconciliation_subject,
            "reconciliation: descriptor subject mismatches independently re-derived proof")

    cardinality_by_provider = {
        row["provider_payment_id"]: row
        for row in artifact["observations"]
    }
    expected_reconciliation_provider_ids = {
        row["provider_payment_id"]
        for row in artifact["observations"]
        if row["verification"]["payment_status"] in {"authorized", "captured", "refunded"}
    }
    reconciliation_provider_rows = reconciliation_artifact["producer_report"]["snapshot"]["provider"]
    observed_reconciliation_provider_ids = {
        row["provider_payment_id"] for row in reconciliation_provider_rows
    }
    require(
        observed_reconciliation_provider_ids == expected_reconciliation_provider_ids,
        "reconciliation: provider snapshot membership differs from stable EOD day-cardinality membership",
    )
    status_map = {"AUTHORIZED": "authorized", "CAPTURED": "captured", "REFUNDED": "refunded"}
    for provider_row in reconciliation_provider_rows:
        provider_id = provider_row["provider_payment_id"]
        require(provider_id in cardinality_by_provider,
                "reconciliation: provider snapshot identity missing from day-cardinality proof")
        day_row = cardinality_by_provider[provider_id]
        day_proof = day_row["verification"]
        require(
            status_map.get(provider_row["normalized_status"]) == day_proof["payment_status"],
            "reconciliation: provider status differs from day-cardinality proof",
        )
        for field in ("authorized_amount_minor", "captured_amount_minor", "refunded_amount_minor"):
            require(
                provider_row[field] == day_proof[field],
                f"reconciliation: provider {field} differs from day-cardinality proof",
            )
        require(
            provider_row["provider_version"] == day_row["timeline"]["payment"]["version"],
            "reconciliation: provider version differs from day-cardinality proof",
        )

    recovery = checks[RECOVERY_CHECK]
    recovery_name = f"day-{sequence:06}.recovery.verification.json"
    require(type(recovery) is dict and set(recovery) == expected_fields,
            "recovery: bound descriptor fields mismatch")
    require(
        recovery["verdict"] == "PASS"
        and recovery["artifact_schema"] == day_recovery.SCHEMA
        and type(recovery["artifact_schema_version"]) is int
        and recovery["artifact_schema_version"] == day_recovery.SCHEMA_VERSION
        and recovery["campaign_id"] == identity["campaign_id"]
        and type(recovery["logical_day_sequence"]) is int
        and recovery["logical_day_sequence"] == sequence
        and recovery["source_revision"] == identity["source_revision"],
        "recovery: descriptor identity/schema/verdict mismatch",
    )
    require(
        isinstance(recovery["artifact_sha256"], str)
        and HEX64.fullmatch(recovery["artifact_sha256"]) is not None,
        "recovery: artifact SHA invalid",
    )
    recovery_path = _safe_relative_artifact(
        campaign_path,
        recovery["relative_path"],
        recovery_name,
        RECOVERY_CHECK,
    )
    require(sha256(recovery_path) == recovery["artifact_sha256"],
            "recovery: retained file SHA mismatch")
    recovery_artifact = load_json(recovery_path)
    recovery_subject = day_recovery.validate(
        recovery_artifact,
        plan=day_cardinality.plan_for(identity["campaign_id"], sequence),
        source_revision=identity["source_revision"],
        workload_binary_sha256=identity["batch_binaries"]["payment-recovery-postgres"]["sha256"],
    )
    require(recovery["subject"] == recovery_subject,
            "recovery: descriptor subject mismatches independently re-derived proof")

    cardinality_by_payment = {
        row["merchant_payment_id"]: row
        for row in artifact["observations"]
    }
    for entry in recovery_artifact["candidates"]:
        candidate = entry["candidate"]
        provider_proof = entry["provider_verification"]
        payment_id = candidate["merchant_payment_id"]
        require(payment_id in cardinality_by_payment,
                "recovery: candidate missing from day provider-cardinality proof")
        day_provider = cardinality_by_payment[payment_id]
        day_proof = day_provider["verification"]
        require(
            day_provider["provider_payment_id"] == provider_proof["provider_payment_id"]
            and day_proof["provider_payment_id"] == provider_proof["provider_payment_id"],
            "recovery: provider identity differs from day provider-cardinality proof",
        )
        for field in ("payment_status", "effective_mutation_count", "timeline_sha256"):
            require(
                day_proof[field] == provider_proof[field],
                f"recovery: provider {field} differs from day provider-cardinality proof",
            )

    financial = checks[FINANCIAL_CHECK]
    financial_name = f"day-{sequence:06}.financial-invariants.verification.json"
    require(type(financial) is dict and set(financial) == expected_fields,
            "financial_invariants: bound descriptor fields mismatch")
    require(
        financial["verdict"] == "PASS"
        and financial["artifact_schema"] == day_financial.SCHEMA
        and type(financial["artifact_schema_version"]) is int
        and financial["artifact_schema_version"] == day_financial.SCHEMA_VERSION
        and financial["campaign_id"] == identity["campaign_id"]
        and type(financial["logical_day_sequence"]) is int
        and financial["logical_day_sequence"] == sequence
        and financial["source_revision"] == identity["source_revision"],
        "financial_invariants: descriptor identity/schema/verdict mismatch",
    )
    require(
        isinstance(financial["artifact_sha256"], str)
        and HEX64.fullmatch(financial["artifact_sha256"]) is not None,
        "financial_invariants: artifact SHA invalid",
    )
    financial_path = _safe_relative_artifact(
        campaign_path,
        financial["relative_path"],
        financial_name,
        FINANCIAL_CHECK,
    )
    require(sha256(financial_path) == financial["artifact_sha256"],
            "financial_invariants: retained file SHA mismatch")
    financial_artifact = load_json(financial_path)
    financial_subject = day_financial.validate(
        financial_artifact,
        plan=day_cardinality.plan_for(identity["campaign_id"], sequence),
        source_revision=identity["source_revision"],
    )
    require(financial["subject"] == financial_subject,
            "financial_invariants: descriptor subject mismatches independently re-derived proof")

    financial_by_payment = {
        row["merchant_payment_id"]: row
        for row in financial_artifact["observations"]
    }
    require(set(financial_by_payment) == set(cardinality_by_payment),
            "financial_invariants: Merchant payment membership differs from day provider-cardinality proof")
    for payment_id, financial_row in financial_by_payment.items():
        day_provider = cardinality_by_payment[payment_id]
        raw_provider = financial_row["provider"]
        day_timeline = day_provider["timeline"]
        require(day_timeline is not None,
                "financial_invariants: accepted financial slot lacks provider timeline")
        day_payment = day_timeline["payment"]
        require(
            raw_provider["id"] == day_provider["provider_payment_id"] == day_payment["id"],
            "financial_invariants: provider identity differs from day provider-cardinality proof",
        )
        for field in ("status", "authorized_amount", "captured_amount", "refunded_amount", "version"):
            require(
                raw_provider[field] == day_payment[field],
                f"financial_invariants: provider {field} differs from day provider-cardinality proof",
            )

    require(
        financial_subject["settlement_eligible_count"] == roster_subject["eligible_payment_count"]
        and financial_subject["captured_total_minor"] == settlement_subject["captured_total_minor"]
        and financial_subject["refunded_total_minor"] == settlement_subject["refunded_total_minor"]
        and financial_subject["net_settlement_total_minor"] == settlement_subject["net_settlement_total_minor"],
        "financial_invariants: settlement totals differ from independent settlement proof",
    )

    financial_reconciliation_pairs = {
        (row["merchant_payment_id"], row["provider"]["id"])
        for row in financial_artifact["observations"]
        if row["provider"]["status"] in {"authorized", "captured", "refunded"}
    }
    reconciliation_pairs = {
        (row["merchant_payment_id"], row["provider_payment_id"])
        for row in reconciliation_artifact["producer_report"]["snapshot"]["internal"]
    }
    require(
        financial_reconciliation_pairs == reconciliation_pairs,
        "financial_invariants: Merchant/provider identity pairs differ from reconciliation snapshot",
    )

    recovery_by_payment = {
        entry["candidate"]["merchant_payment_id"]: entry
        for entry in recovery_artifact["candidates"]
    }
    for payment_id, entry in recovery_by_payment.items():
        require(payment_id in financial_by_payment,
                "financial_invariants: recovery candidate missing from financial proof")
        financial_row = financial_by_payment[payment_id]
        proof = financial_row["proof"]
        result = entry["result"]
        require(
            proof.get("canonical_payment_state") == result["final_canonical_payment_state"]
            and proof.get("canonical_operation_state") == result["final_canonical_operation_state"]
            and proof.get("provider_status") == result["final_provider_status"],
            "financial_invariants: recovered canonical/provider state differs from financial proof",
        )


    online = checks[ONLINE_CHECK]
    online_name = f"day-{sequence:06}.online.verification.json"
    require(type(online) is dict and set(online) == expected_fields,
            "canonical_online: bound descriptor fields mismatch")
    require(
        online["verdict"] == "PASS"
        and online["artifact_schema"] == day_online.SCHEMA
        and online["artifact_schema_version"] == day_online.SCHEMA_VERSION
        and online["campaign_id"] == identity["campaign_id"]
        and online["logical_day_sequence"] == sequence
        and online["source_revision"] == identity["source_revision"],
        "canonical_online: descriptor identity/schema/verdict mismatch",
    )
    require(
        isinstance(online["artifact_sha256"], str)
        and HEX64.fullmatch(online["artifact_sha256"]) is not None,
        "canonical_online: artifact SHA invalid",
    )
    online_path = _safe_relative_artifact(
        campaign_path, online["relative_path"], online_name, ONLINE_CHECK,
    )
    require(
        sha256(online_path) == online["artifact_sha256"],
        "canonical_online: retained file SHA mismatch",
    )
    online_artifact = load_json(online_path)
    online_subject = day_online.validate(
        online_artifact,
        plan=day_online.plan_for(identity["campaign_id"], sequence),
        source_revision=identity["source_revision"],
        provenance_hashes=identity["provenance_sha256"],
    )
    require(
        online["subject"] == online_subject,
        "canonical_online: descriptor subject mismatches independently re-derived proof",
    )
    require(
        online_subject["primary_lifecycle_count"] == plan_day(sequence)["primary_lifecycles"],
        "canonical_online: primary lifecycle count differs from campaign day plan",
    )

    quiescence = checks[QUIESCENCE_CHECK]
    quiescence_name = f"day-{sequence:06}.quiescence.verification.json"
    require(type(quiescence) is dict and set(quiescence) == expected_fields,
            "quiescence: bound descriptor fields mismatch")
    require(
        quiescence["verdict"] == "PASS"
        and quiescence["artifact_schema"] == day_quiescence.SCHEMA
        and quiescence["artifact_schema_version"] == day_quiescence.SCHEMA_VERSION
        and quiescence["campaign_id"] == identity["campaign_id"]
        and quiescence["logical_day_sequence"] == sequence
        and quiescence["source_revision"] == identity["source_revision"],
        "quiescence: descriptor identity/schema/verdict mismatch",
    )
    require(
        isinstance(quiescence["artifact_sha256"], str)
        and HEX64.fullmatch(quiescence["artifact_sha256"]) is not None,
        "quiescence: artifact SHA invalid",
    )
    quiescence_path = _safe_relative_artifact(
        campaign_path, quiescence["relative_path"], quiescence_name, QUIESCENCE_CHECK,
    )
    require(
        sha256(quiescence_path) == quiescence["artifact_sha256"],
        "quiescence: retained file SHA mismatch",
    )
    quiescence_artifact = load_json(quiescence_path)
    quiescence_subject = day_quiescence.validate(
        quiescence_artifact,
        campaign_dir=campaign_path,
        plan=plan_day(sequence),
        source_revision=identity["source_revision"],
        expected_services=sorted(identity["images"]["services"]),
    )
    require(
        quiescence["subject"] == quiescence_subject,
        "quiescence: descriptor subject mismatches independently re-derived proof",
    )


    return None


def validate_verification(
    path: Path,
    campaign_path: Path,
    identity: dict[str, Any],
    sequence: int,
) -> str:
    validate_verification_record(load_json(path), campaign_path, identity, sequence)
    return sha256(path)


def _bound_descriptor(
    *,
    check: str,
    artifact_path: Path,
    campaign_path: Path,
    artifact_schema: str,
    artifact_schema_version: int,
    campaign_id: str,
    sequence: int,
    source_revision: str,
    subject: dict[str, Any],
) -> dict[str, Any]:
    require(
        artifact_path.is_file() and not artifact_path.is_symlink(),
        f"{check}: required retained artifact missing/unsafe",
    )
    return {
        "verdict": "PASS",
        "artifact_schema": artifact_schema,
        "artifact_schema_version": artifact_schema_version,
        "campaign_id": campaign_id,
        "logical_day_sequence": sequence,
        "source_revision": source_revision,
        "relative_path": artifact_path.relative_to(campaign_path).as_posix(),
        "artifact_sha256": sha256(artifact_path),
        "subject": subject,
    }


def build_day_verification(campaign_path: Path, sequence: int) -> dict[str, Any]:
    """Assemble one candidate from retained proofs, then validate all cross-bindings."""
    require(type(sequence) is int and sequence > 0, "day sequence must be positive")
    campaign_path = campaign_path.resolve()
    record = load_json(campaign_path / "campaign.json")
    identity = record.get("identity")
    require(isinstance(identity, dict), "campaign identity missing")
    verify_identity(identity)
    require(identity["campaign_id"] == campaign_path.name, "campaign directory identity mismatch")

    campaign_id = identity["campaign_id"]
    source_revision = identity["source_revision"]
    plan = day_cardinality.plan_for(campaign_id, sequence)
    statement_dir = campaign_path / "statements"
    day_dir = campaign_path / "days"

    paths = {
        "settlement_roster": statement_dir / f"day-{sequence:06}.roster.verification.json",
        "settlement_close": statement_dir / f"day-{sequence:06}.settlement.verification.json",
        CARDINALITY_CHECK: statement_dir / f"day-{sequence:06}.provider-cardinality.verification.json",
        RECOVERY_CHECK: day_dir / f"day-{sequence:06}.recovery.verification.json",
        RECONCILIATION_CHECK: day_dir / f"day-{sequence:06}.reconciliation.verification.json",
        FINANCIAL_CHECK: day_dir / f"day-{sequence:06}.financial-invariants.verification.json",
        ONLINE_CHECK: day_dir / f"day-{sequence:06}.online.verification.json",
        QUIESCENCE_CHECK: day_dir / f"day-{sequence:06}.quiescence.verification.json",
    }
    for check, path in paths.items():
        require(path.is_file() and not path.is_symlink(), f"{check}: retained proof missing/unsafe")

    roster = load_json(paths["settlement_roster"])
    settlement = load_json(paths["settlement_close"])
    cardinality = load_json(paths[CARDINALITY_CHECK])
    recovery = load_json(paths[RECOVERY_CHECK])
    reconciliation = load_json(paths[RECONCILIATION_CHECK])
    financial = load_json(paths[FINANCIAL_CHECK])
    online = load_json(paths[ONLINE_CHECK])
    quiescence = load_json(paths[QUIESCENCE_CHECK])

    subjects = {
        "settlement_roster": _roster_subject(roster),
        "settlement_close": _settlement_subject(settlement),
        CARDINALITY_CHECK: day_cardinality.validate(
            cardinality,
            plan=plan,
            source_revision=source_revision,
            roster_verification=roster,
        ),
        RECOVERY_CHECK: day_recovery.validate(
            recovery,
            plan=plan,
            source_revision=source_revision,
            workload_binary_sha256=identity["batch_binaries"]["payment-recovery-postgres"]["sha256"],
        ),
        RECONCILIATION_CHECK: day_reconciliation.validate(
            reconciliation,
            plan=plan,
            source_revision=source_revision,
            workload_binary_sha256=identity["batch_binaries"]["payment-reconciliation-postgres"]["sha256"],
        ),
        FINANCIAL_CHECK: day_financial.validate(
            financial,
            plan=plan,
            source_revision=source_revision,
        ),
        ONLINE_CHECK: day_online.validate(
            online,
            plan=day_online.plan_for(campaign_id, sequence),
            source_revision=source_revision,
            provenance_hashes=identity["provenance_sha256"],
        ),
        QUIESCENCE_CHECK: day_quiescence.validate(
            quiescence,
            campaign_dir=campaign_path,
            plan=plan_day(sequence),
            source_revision=source_revision,
            expected_services=sorted(identity["images"]["services"]),
        ),
    }

    schemas = {
        "settlement_roster": (SETTLEMENT_ROSTER_SCHEMA, 1),
        "settlement_close": (SETTLEMENT_VERIFICATION_SCHEMA, 1),
        CARDINALITY_CHECK: (day_cardinality.SCHEMA, 1),
        RECOVERY_CHECK: (day_recovery.SCHEMA, day_recovery.SCHEMA_VERSION),
        RECONCILIATION_CHECK: (day_reconciliation.SCHEMA, day_reconciliation.SCHEMA_VERSION),
        FINANCIAL_CHECK: (day_financial.SCHEMA, day_financial.SCHEMA_VERSION),
        ONLINE_CHECK: (day_online.SCHEMA, day_online.SCHEMA_VERSION),
        QUIESCENCE_CHECK: (day_quiescence.SCHEMA, day_quiescence.SCHEMA_VERSION),
    }
    checks = {
        check: _bound_descriptor(
            check=check,
            artifact_path=paths[check],
            campaign_path=campaign_path,
            artifact_schema=schemas[check][0],
            artifact_schema_version=schemas[check][1],
            campaign_id=campaign_id,
            sequence=sequence,
            source_revision=source_revision,
            subject=subjects[check],
        )
        for check in paths
    }
    candidate = {
        "schema": DAY_VERIFICATION_SCHEMA,
        "schema_version": DAY_VERIFICATION_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "logical_day_sequence": sequence,
        "source_revision": source_revision,
        "verdict": "PASS",
        "checks": checks,
    }
    validate_verification_record(candidate, campaign_path, identity, sequence)
    return candidate


def retain_exact_json(path: Path, value: dict[str, Any]) -> str:
    raw = canonical_bytes(value)
    if path.exists():
        require(path.is_file() and not path.is_symlink(), "retained terminal path unsafe")
        require(path.read_bytes() == raw, "retained terminal artifact immutable conflict")
        return "replay"
    create_only(path, raw)
    return "created"


def lifecycle_state(campaign_path: Path) -> dict[str, Any]:
    """Validate event ordering without interpreting proof files, for crash-safe resume."""
    campaign_path = campaign_path.resolve()
    record = load_json(campaign_path / "campaign.json")
    identity = record.get("identity")
    require(isinstance(identity, dict), "campaign identity missing")
    verify_identity(identity)
    require(identity["campaign_id"] == campaign_path.name, "campaign directory identity mismatch")

    events = iter_events(campaign_path / "events.jsonl", identity["campaign_id"])
    first = next(events, None)
    require(
        first is not None
        and first["phase"] == "campaign-initialized"
        and first["logical_day_sequence"] == 0,
        "missing first initialization event",
    )

    frontier = 0
    active = None
    terminal_failure = None
    for event in events:
        require(terminal_failure is None, "events exist after terminal day failure")
        sequence = event["logical_day_sequence"]
        phase = event["phase"]
        if phase in CAMPAIGN_PHASES - {"campaign-initialized"}:
            require(sequence == 0, "controller event must have sequence zero")
            continue
        require(sequence > 0, "day lifecycle event on sequence zero")
        if phase == "day-started":
            require(active is None and sequence == frontier + 1,
                    "day-start event gap/duplicate/out-of-order")
            active = sequence
            continue
        require(active == sequence, "day event without matching start")
        if phase in DAY_PHASES - {"day-started", "day-terminal-success", "day-terminal-failure"}:
            continue
        if phase == "day-terminal-failure":
            terminal_failure = sequence
            active = None
            continue
        require(phase == "day-terminal-success", "unknown day lifecycle event")
        frontier = sequence
        active = None

    return {
        "frontier_sequence": frontier,
        "active_day": active,
        "terminal_failure": terminal_failure,
    }


def materialize_terminal_day(campaign_path: Path, sequence: int) -> dict[str, Any]:
    """Materialize terminal PASS only after the canonical eight-proof verifier passes."""
    require(type(sequence) is int and sequence > 0, "day sequence must be positive")
    campaign_path = campaign_path.resolve()
    state = lifecycle_state(campaign_path)
    require(state["terminal_failure"] is None, "campaign already contains terminal day failure")
    require(
        state["active_day"] == sequence and state["frontier_sequence"] == sequence - 1,
        "terminal materialization requires the matching active next day",
    )

    campaign = load_json(campaign_path / "campaign.json")
    identity = campaign["identity"]
    verification_path = campaign_path / "days" / f"{sequence:06}.verification.json"
    day_path = campaign_path / "days" / f"{sequence:06}.json"

    require(
        not any((campaign_path / "failures").iterdir()),
        "failure artifacts retained before terminal materialization",
    )
    if not verification_path.exists() and not day_path.exists():
        preflight = inspect(campaign_path)
        require(
            preflight.get("decision") == "BLOCK"
            and preflight.get("reason") == f"nonterminal day {sequence}"
            and preflight.get("frontier_sequence") == sequence - 1,
            "active day failed independent pre-terminal frontier validation",
        )
    else:
        require(
            verification_path.exists(),
            "terminal day artifact exists without independent verification artifact",
        )

    verification = build_day_verification(campaign_path, sequence)
    verification_retention = retain_exact_json(verification_path, verification)
    verification_sha = validate_verification(
        verification_path, campaign_path, identity, sequence
    )

    day = {
        "schema": DAY_SCHEMA,
        "schema_version": 1,
        "campaign_id": identity["campaign_id"],
        "logical_day_sequence": sequence,
        "plan": plan_day(sequence),
        "verdict": "PASS",
        "independent_verification_sha256": verification_sha,
    }
    day_retention = retain_exact_json(day_path, day)
    state = lifecycle_state(campaign_path)
    require(
        state["terminal_failure"] is None
        and state["active_day"] == sequence
        and state["frontier_sequence"] == sequence - 1,
        "day lifecycle changed during terminal materialization",
    )
    require(
        not any((campaign_path / "failures").iterdir()),
        "failure artifacts retained before terminal success event",
    )
    append_event(
        campaign_path,
        sequence,
        "day-terminal-success",
        {"day_sha256": sha256(day_path)},
    )

    status = inspect(campaign_path)
    require(
        status.get("decision") == "READY"
        and status.get("frontier_sequence") == sequence,
        "terminal materialization did not advance the independently verified frontier",
    )
    return {
        "decision": "DAY_TERMINAL_SUCCESS_INDEPENDENTLY_VERIFIED",
        "logical_day_sequence": sequence,
        "verification_retention": verification_retention,
        "day_retention": day_retention,
        "verification_sha256": verification_sha,
        "day_sha256": sha256(day_path),
        "frontier_sequence": sequence,
        "campaign_acceptance_authorized": False,
    }


def inspect(campaign_path: Path) -> dict[str, Any]:
    """Read-only frontier scan. READY is not campaign acceptance or financial proof."""
    record = load_json(campaign_path / "campaign.json")
    require(record.get("schema") == SCHEMA and record.get("schema_version") == SCHEMA_VERSION, "campaign schema drift")
    require(record.get("contract_sha256") == sha256(CONTRACT_PATH), "campaign contract changed")
    identity = record.get("identity")
    require(isinstance(identity, dict), "campaign identity missing")
    verify_identity(identity)
    require(identity["campaign_id"] == campaign_path.name, "campaign directory identity mismatch")
    for name in ARTIFACTS:
        require(sha256(campaign_path / "provenance" / name) == identity["provenance_sha256"][name], f"provenance changed: {name}")
    validate_provenance(identity, {name: load_json(campaign_path / "provenance" / name) for name in ARTIFACTS})
    events = iter_events(campaign_path / "events.jsonl", identity["campaign_id"])
    first = next(events, None)
    require(first is not None and first["phase"] == "campaign-initialized" and first["logical_day_sequence"] == 0, "missing first initialization event")
    days, verifications = _day_files(campaign_path)
    failures = tuple((campaign_path / "failures").iterdir())
    frontier = 0
    active = None
    completed = set()
    for event in events:
        s = event["logical_day_sequence"]
        phase = event["phase"]
        if phase in CAMPAIGN_PHASES - {"campaign-initialized"}:
            require(s == 0, "controller event must have sequence zero")
            continue
        require(s > 0, "day lifecycle event on sequence zero")
        if phase == "day-started":
            require(active is None and s == frontier + 1, "day-start event gap/duplicate/out-of-order")
            active = s
            continue
        require(active == s, "day event without matching start")
        if phase in DAY_PHASES - {"day-started", "day-terminal-success", "day-terminal-failure"}:
            continue
        if phase == "day-terminal-failure":
            return {"decision": "BLOCK", "reason": f"terminal failure on day {s}", "frontier_sequence": frontier}
        require(phase == "day-terminal-success", "unknown day lifecycle event")
        require(s in days and s not in completed, "terminal day missing or duplicated")
        day = load_json(days[s])
        require(set(day) == {"schema", "schema_version", "campaign_id", "logical_day_sequence", "plan", "verdict", "independent_verification_sha256"}, f"day {s}: record fields mismatch")
        require(day["schema"] == DAY_SCHEMA and day["schema_version"] == 1 and day["campaign_id"] == identity["campaign_id"], f"day {s}: schema or campaign mismatch")
        require(day["logical_day_sequence"] == s and day["plan"] == plan_day(s), f"day {s}: plan identity mismatch")
        require(day["verdict"] == "PASS" and s in verifications, f"day {s}: verification file missing")
        require(day["independent_verification_sha256"] == validate_verification(verifications[s], campaign_path, identity, s), f"day {s}: verification digest mismatch")
        require(event["detail"].get("day_sha256") == sha256(days[s]), f"day {s}: terminal digest mismatch")
        frontier = s
        active = None
        completed.add(s)
    require(set(days) == completed and set(verifications) == completed, "orphan/extra terminal day or verification file")
    if failures:
        return {"decision": "BLOCK", "reason": "failure artifacts retained", "frontier_sequence": frontier}
    if active is not None:
        return {"decision": "BLOCK", "reason": f"nonterminal day {active}", "frontier_sequence": frontier}
    return {"decision": "READY", "frontier_sequence": frontier, "next_day": plan_day(frontier + 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#318 non-executing campaign evidence and day planning")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--day", type=int, required=True)
    p = sub.add_parser("init")
    p.add_argument("--identity", type=Path, required=True)
    p.add_argument("--provenance-dir", type=Path, required=True)
    p.add_argument("--evidence-root", type=Path, required=True)
    p = sub.add_parser("status")
    p.add_argument("--campaign-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            output = plan_day(args.day)
        elif args.command == "init":
            path = initialize(args.evidence_root, load_json(args.identity), args.provenance_dir)
            output = {"decision": "INITIALIZED_NOT_EXECUTED", "campaign_dir": str(path)}
        else:
            output = inspect(args.campaign_dir)
        print(json.dumps(output, sort_keys=True))
        return 0 if output.get("decision") != "BLOCK" else 75
    except (OSError, ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError) as exc:
        print(json.dumps({"decision": "BLOCK", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
