#!/usr/bin/env python3
"""Independent retained proof for one canonical payment-reference online day.

The canonical controller owns mutation ordering. This verifier is read-only:
it replays the retained journal, exact slot intents/completions, slot evidence
validators, and immutable online summary, then materializes a compact campaign-
local proof. It never advances the campaign frontier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import campaign_core as core
import campaign_day_online_canonical as canonical

SCHEMA = "oxide-batch-workloads.payment-reference-day-online-verification"
SCHEMA_VERSION = 1
DECISION = "ONLINE_EXECUTION_VERIFIED_NOT_DAY_ACCEPTED"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")

pilot = canonical.pilot


def require(condition: bool, message: str) -> None:
    if not condition:
        raise core.EvidenceError(message)


def file_sha256() -> str:
    return core.sha256(Path(__file__).resolve())


def plan_for(campaign_id: str, sequence: int) -> dict[str, Any]:
    require(
        isinstance(campaign_id, str)
        and core.CAMPAIGN_ID.fullmatch(campaign_id) is not None,
        "online verification campaign id invalid",
    )
    require(type(sequence) is int and sequence > 0, "online verification day invalid")
    return pilot.traffic.build(campaign_id, sequence)


def _stable_sha(value: Any) -> str:
    return pilot.sha(pilot.stable(value))


def _summary_subject(
    summary: Any,
    *,
    plan: dict[str, Any],
    source_revision: str,
    provenance_hashes: dict[str, str],
    journal_identity_sha256: str,
    completion_hashes: list[dict[str, Any]],
) -> dict[str, Any]:
    require(isinstance(summary, dict), "online summary must be an object")
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "day_plan_sha256",
        "fault_catalog_sha256", "provenance_sha256",
        "order_journal_identity_sha256", "primary_lifecycle_count",
        "normal_slot_count", "fault_slot_count", "slot_order",
        "slot_completion_sha256", "order_chain_head_sha256",
        "combined_normal_fault_ordering_authorized",
        "online_completion_authorized", "scheduled_hard_crash",
        "recovery_reconciliation_required",
        "independent_financial_verification_required",
        "day_completion_authorized",
    }
    require(set(summary) == expected_fields, "online summary fields mismatch")
    normal, faults = canonical.normal_day.partition(plan)
    require(
        summary["schema"] == canonical.SCHEMA
        and summary["schema_version"] == canonical.SCHEMA_VERSION
        and summary["decision"] == canonical.DECISION,
        "online summary schema/decision mismatch",
    )
    require(
        summary["campaign_id"] == plan["campaign_id"]
        and summary["logical_day_sequence"] == plan["logical_day_sequence"]
        and summary["source_revision"] == source_revision,
        "online summary campaign/day/source mismatch",
    )
    require(
        summary["day_plan_sha256"] == _stable_sha(plan)
        and summary["fault_catalog_sha256"] == plan["fault_catalog_sha256"]
        and summary["provenance_sha256"] == provenance_hashes,
        "online summary plan/provenance mismatch",
    )
    require(
        summary["order_journal_identity_sha256"] == journal_identity_sha256,
        "online summary journal identity digest mismatch",
    )
    require(
        summary["primary_lifecycle_count"] == plan["primary_lifecycles"]
        and summary["normal_slot_count"] == len(normal)
        and summary["fault_slot_count"] == len(faults),
        "online summary slot cardinality mismatch",
    )
    require(
        summary["slot_order"] == [item["slot"] for item in plan["slots"]]
        and summary["slot_completion_sha256"] == completion_hashes,
        "online summary canonical slot order/chain mismatch",
    )
    require(
        completion_hashes
        and summary["order_chain_head_sha256"] == completion_hashes[-1]["sha256"],
        "online summary chain head mismatch",
    )
    require(
        summary["combined_normal_fault_ordering_authorized"] is True
        and summary["online_completion_authorized"] is True
        and summary["independent_financial_verification_required"] is True
        and summary["day_completion_authorized"] is False,
        "online summary authority boundary mismatch",
    )
    require(
        type(summary["recovery_reconciliation_required"]) is bool,
        "online summary recovery/reconciliation requirement invalid",
    )
    require(
        summary["scheduled_hard_crash"] == canonical.hard_crash.scheduled_spec(plan),
        "online summary hard-crash schedule mismatch",
    )
    return {
        "day_plan_sha256": summary["day_plan_sha256"],
        "fault_catalog_sha256": summary["fault_catalog_sha256"],
        "primary_lifecycle_count": summary["primary_lifecycle_count"],
        "order_journal_identity_sha256": journal_identity_sha256,
        "order_chain_head_sha256": summary["order_chain_head_sha256"],
        "slot_chain_sha256": "",
        "online_summary_sha256": _stable_sha(summary),
    }


def validate(
    artifact: Any,
    *,
    plan: dict[str, Any],
    source_revision: str,
    provenance_hashes: dict[str, str],
) -> dict[str, Any]:
    require(isinstance(artifact, dict), "online verification artifact must be object")
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "day_plan_sha256",
        "fault_catalog_sha256", "provenance_sha256",
        "verifier_semantics_sha256", "journal_identity", "slot_chain",
        "online_summary",
    }
    require(set(artifact) == expected_fields, "online verification artifact fields mismatch")
    require(
        artifact["schema"] == SCHEMA
        and artifact["schema_version"] == SCHEMA_VERSION
        and artifact["decision"] == DECISION,
        "online verification schema/decision mismatch",
    )
    require(
        artifact["campaign_id"] == plan["campaign_id"]
        and artifact["logical_day_sequence"] == plan["logical_day_sequence"]
        and artifact["source_revision"] == source_revision,
        "online verification campaign/day/source mismatch",
    )
    require(
        artifact["day_plan_sha256"] == _stable_sha(plan)
        and artifact["fault_catalog_sha256"] == plan["fault_catalog_sha256"]
        and artifact["provenance_sha256"] == provenance_hashes,
        "online verification plan/provenance mismatch",
    )
    require(
        artifact["verifier_semantics_sha256"] == file_sha256(),
        "online verifier semantics source mismatch",
    )

    expected_journal = canonical.journal_identity(
        plan, source_revision, provenance_hashes,
    )
    journal = artifact["journal_identity"]
    require(journal == expected_journal, "online journal identity mismatch")
    journal_sha = _stable_sha(journal)

    chain = artifact["slot_chain"]
    require(
        isinstance(chain, list) and len(chain) == plan["primary_lifecycles"],
        "online slot chain cardinality mismatch",
    )
    previous_sha = journal_sha
    completion_hashes: list[dict[str, Any]] = []
    for index, item in enumerate(plan["slots"]):
        row = chain[index]
        require(
            isinstance(row, dict) and set(row) == {"slot", "intent", "completion"},
            "online slot-chain row fields mismatch",
        )
        require(row["slot"] == item["slot"], "online slot-chain order mismatch")
        intent = row["intent"]
        completion = row["completion"]
        require(
            intent == canonical.expected_intent(item, previous_sha),
            "online retained intent differs from exact plan/chain",
        )
        require(
            isinstance(completion, dict)
            and set(completion) == {
                "schema", "schema_version", "ordinal", "slot", "kind",
                "intent_sha256", "result_sha256", "evidence_sha256",
            },
            "online completion fields mismatch",
        )
        evidence = completion["evidence_sha256"]
        require(
            isinstance(evidence, dict)
            and "result.json" in evidence
            and all(
                isinstance(name, str)
                and name
                and isinstance(value, str)
                and HEX64.fullmatch(value) is not None
                for name, value in evidence.items()
            ),
            "online completion evidence digest map invalid",
        )
        expected_completion = canonical.expected_completion(
            item, _stable_sha(intent), evidence,
        )
        require(completion == expected_completion, "online completion proof mismatch")
        previous_sha = _stable_sha(completion)
        completion_hashes.append({"slot": item["slot"], "sha256": previous_sha})

    subject = _summary_subject(
        artifact["online_summary"],
        plan=plan,
        source_revision=source_revision,
        provenance_hashes=provenance_hashes,
        journal_identity_sha256=journal_sha,
        completion_hashes=completion_hashes,
    )
    subject["slot_chain_sha256"] = _stable_sha(chain)
    return subject


def artifact_from_components(
    *,
    plan: dict[str, Any],
    source_revision: str,
    provenance_hashes: dict[str, str],
    journal_identity: dict[str, Any],
    slot_chain: list[dict[str, Any]],
    online_summary: dict[str, Any],
) -> dict[str, Any]:
    artifact = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_revision,
        "day_plan_sha256": _stable_sha(plan),
        "fault_catalog_sha256": plan["fault_catalog_sha256"],
        "provenance_sha256": provenance_hashes,
        "verifier_semantics_sha256": file_sha256(),
        "journal_identity": journal_identity,
        "slot_chain": slot_chain,
        "online_summary": online_summary,
    }
    validate(
        artifact,
        plan=plan,
        source_revision=source_revision,
        provenance_hashes=provenance_hashes,
    )
    return artifact


def collect(
    *,
    plan: dict[str, Any],
    online_evidence_root: Path,
    source_revision: str,
    provenance_hashes: dict[str, str],
) -> dict[str, Any]:
    require(
        online_evidence_root.is_dir() and not online_evidence_root.is_symlink(),
        "online evidence root missing/unsafe",
    )
    day = pilot.day_path(online_evidence_root, plan)
    require(day.is_dir() and not day.is_symlink(), "canonical online day evidence missing/unsafe")
    require(
        pilot.read_object(day / "identity.json")
        == pilot.day_identity(plan, source_revision, provenance_hashes),
        "canonical online day identity mismatch",
    )
    order_dir = day / canonical.ORDER_DIR
    require(order_dir.is_dir() and not order_dir.is_symlink(), "canonical online journal missing/unsafe")
    journal = pilot.read_object(order_dir / "identity.json")
    require(
        journal == canonical.journal_identity(plan, source_revision, provenance_hashes),
        "canonical online journal identity mismatch",
    )

    previous_sha = pilot.sha((order_dir / "identity.json").read_bytes())
    slot_chain: list[dict[str, Any]] = []
    completion_hashes: list[dict[str, Any]] = []
    fault_results: list[dict[str, Any]] = []
    for item in plan["slots"]:
        intent_path, completion_path = canonical.order_files(order_dir, item["slot"])
        require(
            intent_path.is_file() and not intent_path.is_symlink()
            and completion_path.is_file() and not completion_path.is_symlink(),
            "canonical online slot intent/completion missing/unsafe",
        )
        intent = pilot.read_object(intent_path)
        completion = pilot.read_object(completion_path)
        require(
            intent == canonical.expected_intent(item, previous_sha),
            "canonical online slot intent drift",
        )
        result = canonical.validate_slot(
            plan=plan,
            item=item,
            evidence_root=online_evidence_root,
            source_sha=source_revision,
            provenance_hashes=provenance_hashes,
            validate_normal=pilot.validate_completed_slot,
            validate_fault=canonical.fault_day.validate_completed_fault_slot,
        )
        evidence = canonical.slot_evidence_sha256(day / f"slot-{item['slot']:03d}")
        require(
            completion == canonical.expected_completion(
                item, pilot.sha(intent_path.read_bytes()), evidence,
            ),
            "canonical online slot completion drift",
        )
        previous_sha = pilot.sha(completion_path.read_bytes())
        completion_hashes.append({"slot": item["slot"], "sha256": previous_sha})
        slot_chain.append({"slot": item["slot"], "intent": intent, "completion": completion})
        if item["kind"] == "fault_overlay":
            fault_results.append(result)

    expected_order_files = {order_dir / "identity.json"}
    for item in plan["slots"]:
        expected_order_files.update(canonical.order_files(order_dir, item["slot"]))
    require(
        set(order_dir.iterdir()) == expected_order_files,
        "canonical online journal contains unexpected files",
    )

    summary_path = day / canonical.SUMMARY_FILE
    summary = pilot.read_object(summary_path)
    expected_summary = canonical.summary(
        plan=plan,
        source_sha=source_revision,
        provenance_hashes=provenance_hashes,
        journal_identity_sha256=pilot.sha((order_dir / "identity.json").read_bytes()),
        completion_hashes=completion_hashes,
        fault_results=fault_results,
    )
    require(summary == expected_summary, "canonical online retained summary drift")
    return artifact_from_components(
        plan=plan,
        source_revision=source_revision,
        provenance_hashes=provenance_hashes,
        journal_identity=journal,
        slot_chain=slot_chain,
        online_summary=summary,
    )


def retain_exact(path: Path, artifact: dict[str, Any]) -> str:
    raw = core.canonical_bytes(artifact)
    if path.exists():
        require(path.is_file() and not path.is_symlink(), "online proof path unsafe")
        require(path.read_bytes() == raw, "online proof immutable conflict")
        return "replay"
    flags = 0
    pilot.exclusive_write(path, artifact)
    return "created"


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 independent canonical-online day verifier")
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--online-evidence-root", type=Path, required=True)
    parser.add_argument("--day", type=int, required=True)
    args = parser.parse_args()
    try:
        campaign_dir = args.campaign_dir.resolve()
        record = core.load_json(campaign_dir / "campaign.json")
        identity = record.get("identity")
        require(isinstance(identity, dict), "campaign identity missing")
        campaign_id = identity.get("campaign_id")
        source_revision = identity.get("source_revision")
        provenance = identity.get("provenance_sha256")
        require(
            campaign_id == campaign_dir.name
            and isinstance(source_revision, str)
            and HEX40.fullmatch(source_revision) is not None
            and isinstance(provenance, dict),
            "campaign identity invalid for online verification",
        )
        plan = plan_for(campaign_id, args.day)
        artifact = collect(
            plan=plan,
            online_evidence_root=args.online_evidence_root.resolve(),
            source_revision=source_revision,
            provenance_hashes=provenance,
        )
        output = campaign_dir / "days" / f"day-{args.day:06}.online.verification.json"
        retention = retain_exact(output, artifact)
        subject = validate(
            artifact,
            plan=plan,
            source_revision=source_revision,
            provenance_hashes=provenance,
        )
        print(json.dumps({
            "decision": DECISION,
            "verification_path": str(output),
            "retention": retention,
            "subject": subject,
        }, sort_keys=True))
        return 0
    except (core.EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        print(
            json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
