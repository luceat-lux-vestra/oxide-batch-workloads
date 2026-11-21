#!/usr/bin/env python3
"""#318 independent 100-slot provider effective-mutation proof, not day acceptance.

An authenticated payment-sandbox timeline is the only provider mutation
authority. The settlement roster supplies independently verified Gateway
mapping; the deterministic plan supplies expected business semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Callable

import campaign_fault_catalog as faults

HERE = Path(__file__).resolve().parent
SCHEMA = "oxide-batch-workloads.payment-reference-day-provider-cardinality-verification"
DECISION = "PROVIDER_DAY_CARDINALITY_VERIFIED_NOT_DAY_ACCEPTED"


def _module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load " + file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


traffic = _module("day_provider_traffic_plan", "campaign-traffic-plan.py")
provider = _module("day_provider_cardinality", "campaign-provider-cardinality.py")


class DayCardinalityError(traffic.evidence.EvidenceError):
    """Fail-closed proof error compatible with the campaign frontier scanner."""
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DayCardinalityError(message)


def object_(value: Any, name: str) -> dict[str, Any]:
    require(type(value) is dict, name + ": expected object")
    return value


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def plan_for(campaign_id: str, day: int) -> dict[str, Any]:
    return traffic.build(campaign_id, day)


def expected(item: dict[str, Any]) -> dict[str, Any]:
    kind = item["kind"]
    if kind == "auth_capture_purchase":
        status, mutations, authorized, captured, refunded = "captured", 2, 1000, 1000, 0
    elif kind == "authorize_capture":
        status, mutations, authorized, captured, refunded = "captured", 3, 1000, 1000, 0
    elif kind == "authorize_cancel":
        status, mutations, authorized, captured, refunded = "cancelled", 2, 1000, 0, 0
    elif kind == "auth_capture_refund":
        status, mutations, authorized, captured, refunded = "refunded", 4, 1000, 1000, 1000
    else:
        require(kind == "fault_overlay", "unknown slot kind")
        truth = faults.end_of_day_provider_truth(item["fault_class"])
        require(truth in {"AUTHORIZED", "FAILED"}, "unsupported end-of-day provider fault truth")
        status = {"AUTHORIZED": "authorized", "FAILED": "failed"}[truth]
        mutations = 1
        authorized = 1000 if status == "authorized" else 0
        captured = refunded = 0
    return {
        "provider_status": status,
        "effective_mutation_count": mutations,
        "authorized_amount_minor": authorized,
        "captured_amount_minor": captured,
        "refunded_amount_minor": refunded,
    }


def verify_slot(
    item: dict[str, Any],
    roster_row: dict[str, Any],
    observation: dict[str, Any],
) -> int:
    slot = item["slot"]
    context = f"slot {slot}"
    roster_fields = {
        "slot", "kind", "fault_class", "merchant_payment_id", "provider_payment_id",
        "provider_status", "provider_version", "captured_amount_minor",
        "refunded_amount_minor", "settlement_eligible",
    }
    require(set(roster_row) == roster_fields, context + ": roster observation fields mismatch")
    for name in ("slot", "kind", "fault_class", "merchant_payment_id"):
        require(roster_row[name] == item[name], context + ": roster plan " + name + " mismatch")
    payment_id = roster_row["provider_payment_id"]
    require(isinstance(payment_id, str) and provider.SAFE_ID.fullmatch(payment_id) is not None,
            context + ": unsafe provider payment identity")
    require(
        set(observation) == {
            "slot", "kind", "fault_class", "merchant_payment_id", "provider_payment_id",
            "http_status", "timeline", "verification",
        },
        context + ": provider observation fields mismatch",
    )
    for name in ("slot", "kind", "fault_class", "merchant_payment_id", "provider_payment_id"):
        require(observation[name] == roster_row[name], context + ": provider mapping/plan mismatch: " + name)
    contract = expected(item)
    status = contract["provider_status"]
    require(roster_row["provider_status"] == status, context + ": roster provider status mismatch")
    require(
        roster_row["captured_amount_minor"] == contract["captured_amount_minor"]
        and roster_row["refunded_amount_minor"] == contract["refunded_amount_minor"],
        context + ": roster financial truth mismatch",
    )
    require(
        roster_row["settlement_eligible"] is (status in {"captured", "refunded"}),
        context + ": roster settlement eligibility mismatch",
    )
    http_status = observation["http_status"]
    require(type(http_status) is int, context + ": HTTP status must be integer")
    if status == "ABSENT":
        require(http_status == 404 and observation["timeline"] is None,
                context + ": absent provider requires HTTP 404 without timeline")
        expected_proof = provider.verify_absent(payment_id, 404)
        require(roster_row["provider_version"] is None, context + ": absent provider has a version")
    else:
        require(http_status == 200, context + ": present provider requires HTTP 200")
        timeline = object_(observation["timeline"], context + " provider timeline")
        expected_proof = provider.verify_cardinality(
            provider_payment_id=payment_id,
            timeline=timeline,
            expected_effective_mutations=contract["effective_mutation_count"],
            expected_status=status,
            expected_authorized=contract["authorized_amount_minor"],
            expected_captured=contract["captured_amount_minor"],
            expected_refunded=contract["refunded_amount_minor"],
        )
        require(timeline["payment"]["currency"] == traffic.CURRENCY, context + ": provider currency mismatch")
        require(timeline["payment"]["amount"] == traffic.AMOUNT_MINOR, context + ": provider original amount mismatch")
        require(roster_row["provider_version"] == expected_proof["provider_version"],
                context + ": roster/provider version drift")
    require(observation["verification"] == expected_proof,
            context + ": retained cardinality proof does not match provider timeline")
    return contract["effective_mutation_count"]


def validate(
    artifact: Any,
    *,
    plan: dict[str, Any],
    source_revision: str,
    roster_verification: dict[str, Any],
) -> dict[str, Any]:
    artifact = object_(artifact, "day provider cardinality")
    require(
        set(artifact) == {
            "schema", "schema_version", "decision", "campaign_id", "logical_day_sequence",
            "source_revision", "day_plan_sha256", "roster_sha256",
            "roster_verification_sha256", "provider_authority", "provider_source_revision",
            "observed_slot_count", "effective_mutation_total", "observations",
        },
        "day provider cardinality fields mismatch",
    )
    require(artifact["schema"] == SCHEMA and type(artifact["schema_version"]) is int
            and artifact["schema_version"] == 1 and artifact["decision"] == DECISION,
            "day provider cardinality schema/decision mismatch")
    require(
        artifact["campaign_id"] == plan["campaign_id"]
        and type(artifact["logical_day_sequence"]) is int
        and artifact["logical_day_sequence"] == plan["logical_day_sequence"]
        and artifact["source_revision"] == source_revision,
        "day provider cardinality campaign/day/source mismatch",
    )
    require(
        artifact["day_plan_sha256"] == digest(plan).removeprefix("sha256:")
        and artifact["roster_sha256"] == roster_verification.get("roster_sha256")
        and artifact["roster_verification_sha256"] == digest(roster_verification),
        "day provider cardinality plan/roster digest mismatch",
    )
    require(
        artifact["provider_authority"] == provider.AUTHORITY
        and artifact["provider_source_revision"] == provider.PAYMENT_SANDBOX_SOURCE_REVISION,
        "day provider authority/source revision mismatch",
    )
    require(
        roster_verification.get("schema") == "oxide-batch-workloads.payment-reference-settlement-roster-verification"
        and roster_verification.get("schema_version") == 1
        and roster_verification.get("decision") == "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED"
        and roster_verification.get("campaign_id") == plan["campaign_id"]
        and roster_verification.get("logical_day_sequence") == plan["logical_day_sequence"]
        and roster_verification.get("source_revision") == source_revision
        and roster_verification.get("day_plan_sha256") == digest(plan).removeprefix("sha256:")
        and roster_verification.get("plan_contract_sha256") == plan["plan_contract_sha256"]
        and roster_verification.get("fault_catalog_sha256") == plan["fault_catalog_sha256"],
        "day provider cardinality roster provenance mismatch",
    )
    rows = roster_verification.get("observations")
    observations = artifact["observations"]
    require(
        isinstance(roster_verification.get("provider_account_id"), str)
        and bool(roster_verification["provider_account_id"].strip())
        and roster_verification.get("settlement_date") == plan["logical_business_date"]
        and roster_verification.get("currency") == traffic.CURRENCY,
        "day provider cardinality roster account/date/currency mismatch",
    )
    require(isinstance(rows, list) and isinstance(observations, list)
            and len(rows) == len(observations) == len(plan["slots"]) == 100,
            "day provider cardinality requires exactly 100 slots")
    require(
        type(roster_verification.get("observed_slot_count")) is int
        and roster_verification["observed_slot_count"] == 100
        and type(artifact["observed_slot_count"]) is int
        and artifact["observed_slot_count"] == 100,
        "day provider cardinality slot count mismatch",
    )
    require(
        type(roster_verification.get("eligible_payment_count")) is int
        and roster_verification["eligible_payment_count"] ==
        sum(row.get("settlement_eligible") is True for row in rows if type(row) is dict),
        "day provider cardinality roster eligibility count mismatch",
    )
    seen: set[str] = set()
    total = 0
    for index, (item, row, observation) in enumerate(zip(plan["slots"], rows, observations), 1):
        require(item["slot"] == index, "day provider plan slot order mismatch")
        row = object_(row, f"roster slot {index}")
        observation = object_(observation, f"provider slot {index}")
        payment_id = row.get("provider_payment_id")
        require(payment_id not in seen, f"duplicate provider mapping in slot {index}")
        seen.add(payment_id)
        total += verify_slot(item, row, observation)
    require(type(artifact["effective_mutation_total"]) is int
            and artifact["effective_mutation_total"] == total,
            "day provider effective mutation total mismatch")
    return {
        "observed_slot_count": 100,
        "effective_mutation_total": total,
        "roster_sha256": roster_verification["roster_sha256"],
        "roster_verification_sha256": digest(roster_verification),
        "provider_source_revision": provider.PAYMENT_SANDBOX_SOURCE_REVISION,
    }


def collect(
    *,
    plan: dict[str, Any],
    source_revision: str,
    roster_verification: dict[str, Any],
    read: Callable[[str], tuple[int, dict[str, Any] | None]],
) -> dict[str, Any]:
    rows = roster_verification.get("observations")
    require(isinstance(rows, list) and len(rows) == 100 and len(plan["slots"]) == 100,
            "cannot collect partial day provider cardinality")
    observations = []
    for item, row in zip(plan["slots"], rows):
        payment_id = object_(row, "roster row")["provider_payment_id"]
        require(isinstance(payment_id, str) and provider.SAFE_ID.fullmatch(payment_id) is not None,
                "unsafe provider payment identity")
        http_status, timeline = read(payment_id)
        contract = expected(item)
        if contract["provider_status"] == "ABSENT":
            require(type(http_status) is int and http_status == 404 and timeline is None,
                    "expected absent provider was not HTTP 404")
            proof = provider.verify_absent(payment_id, 404)
        else:
            require(type(http_status) is int and http_status == 200 and type(timeline) is dict,
                    "expected present provider timeline was not HTTP 200")
            proof = provider.verify_cardinality(
                provider_payment_id=payment_id, timeline=timeline,
                expected_effective_mutations=contract["effective_mutation_count"],
                expected_status=contract["provider_status"],
                expected_authorized=contract["authorized_amount_minor"],
                expected_captured=contract["captured_amount_minor"],
                expected_refunded=contract["refunded_amount_minor"],
            )
        observations.append({
            "slot": item["slot"], "kind": item["kind"], "fault_class": item["fault_class"],
            "merchant_payment_id": item["merchant_payment_id"],
            "provider_payment_id": payment_id,
            "http_status": http_status, "timeline": timeline, "verification": proof,
        })
    artifact = {
        "schema": SCHEMA,
        "schema_version": 1,
        "decision": DECISION,
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_revision,
        "day_plan_sha256": digest(plan).removeprefix("sha256:"),
        "roster_sha256": roster_verification["roster_sha256"],
        "roster_verification_sha256": digest(roster_verification),
        "provider_authority": provider.AUTHORITY,
        "provider_source_revision": provider.PAYMENT_SANDBOX_SOURCE_REVISION,
        "observed_slot_count": 100,
        "effective_mutation_total": sum(x["verification"]["effective_mutation_count"] for x in observations),
        "observations": observations,
    }
    validate(artifact, plan=plan, source_revision=source_revision,
             roster_verification=roster_verification)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 independent 100-slot provider mutation proof")
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--day", type=int, required=True)
    args = parser.parse_args()
    try:
        roster_builder = _module("day_provider_settlement_roster", "campaign-settlement-roster.py")
        campaign_id, source_revision = roster_builder.load_campaign(args.campaign_dir)
        plan = plan_for(campaign_id, args.day)
        directory = args.campaign_dir / "statements"
        label = f"day-{args.day:06}"
        roster_path = directory / (label + ".roster.json")
        roster_verification_path = directory / (label + ".roster.verification.json")
        output = directory / (label + ".provider-cardinality.verification.json")
        for path in (roster_path, roster_verification_path):
            require(path.is_file() and not path.is_symlink(), "required immutable settlement roster input missing/unsafe")
        require(not output.is_symlink(), "provider cardinality output must not be a symlink")
        roster = json.loads(roster_path.read_text(encoding="utf-8"))
        roster_verification = json.loads(roster_verification_path.read_text(encoding="utf-8"))
        roster_builder.statement.validate_roster(roster)
        roster_builder.validate_verification(roster_verification, roster, plan)
        require(roster["source_revision"] == source_revision, "roster source revision mismatch")

        def read(payment_id: str) -> tuple[int, dict[str, Any] | None]:
            status = provider.provider_timeline_status(payment_id)
            if status == 404:
                return 404, None
            require(status == 200, "provider timeline returned unexpected HTTP status")
            return 200, provider.provider_timeline(payment_id)

        result = collect(
            plan=plan, source_revision=source_revision,
            roster_verification=roster_verification, read=read,
        )
        retained = roster_builder.retain_immutable(output, result)
        print(json.dumps({
            "decision": DECISION, "verification_path": str(output),
            "retention": retained, "observed_slot_count": 100,
            "effective_mutation_total": result["effective_mutation_total"],
        }, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError,
            json.JSONDecodeError, provider.Block) as exc:
        print(json.dumps({"decision": "BLOCK", "error": str(exc)}, sort_keys=True),
              file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
