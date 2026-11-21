#!/usr/bin/env python3
"""#318 independent per-day cross-system financial invariant proof.

The verifier replays retained Merchant/Gateway/Kill Bill/provider snapshots for
all 100 deterministic slots. Provider mutation cardinality and recovery truth
remain separate authorities and are cross-bound by the day frontier.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import campaign_core as core
import campaign_day_cardinality as day_cardinality

HERE = Path(__file__).resolve().parent
FINANCIAL_VERIFIER = HERE / "campaign-financial-consistency.py"

SCHEMA = "oxide-batch-workloads.payment-reference-day-financial-invariants-verification"
SCHEMA_VERSION = 1
DECISION = "FINANCIAL_INVARIANTS_VERIFIED_NOT_DAY_ACCEPTED"


class FinancialInvariantError(core.EvidenceError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FinancialInvariantError(message)


def object_(value: Any, name: str) -> dict[str, Any]:
    require(type(value) is dict, f"{name}: expected object")
    return value


def array(value: Any, name: str) -> list[Any]:
    require(type(value) is list, f"{name}: expected array")
    return value


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


financial = _module("payment_reference_cross_system_financial", FINANCIAL_VERIFIER)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_digest(plan: dict[str, Any]) -> str:
    return json_sha256(plan)


def validate(
    artifact: Any,
    *,
    plan: dict[str, Any],
    source_revision: str,
) -> dict[str, Any]:
    artifact = object_(artifact, "financial invariant artifact")
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "day_plan_sha256",
        "financial_semantics_sha256", "observed_slot_count",
        "normal_slot_count", "fault_slot_count",
        "authorized_total_minor", "captured_total_minor",
        "refunded_total_minor", "net_settlement_total_minor",
        "settlement_eligible_count", "observations",
    }
    require(set(artifact) == expected_fields, "financial invariant artifact fields mismatch")
    require(
        artifact["schema"] == SCHEMA
        and artifact["schema_version"] == SCHEMA_VERSION
        and artifact["decision"] == DECISION,
        "financial invariant artifact schema/decision mismatch",
    )
    require(
        artifact["campaign_id"] == plan.get("campaign_id")
        and artifact["logical_day_sequence"] == plan.get("logical_day_sequence")
        and artifact["source_revision"] == source_revision,
        "financial invariant campaign/day/source mismatch",
    )
    require(artifact["day_plan_sha256"] == plan_digest(plan),
            "financial invariant day-plan digest mismatch")
    require(artifact["financial_semantics_sha256"] == file_sha256(FINANCIAL_VERIFIER),
            "financial invariant verifier-source identity mismatch")

    observations = array(artifact["observations"], "financial invariant observations")
    slots = array(plan.get("slots"), "financial invariant day plan slots")
    require(len(observations) == len(slots) == 100,
            "financial invariant proof requires exactly 100 slots")
    require(artifact["observed_slot_count"] == 100,
            "financial invariant observed slot count mismatch")

    normal_count = 0
    fault_count = 0
    authorized_total = 0
    captured_total = 0
    refunded_total = 0
    settlement_eligible = 0
    seen_payments: set[str] = set()
    proofs: list[dict[str, Any]] = []

    observation_fields = {
        "slot", "kind", "fault_class", "merchant_payment_id",
        "merchant_order_id", "merchant", "gateway", "provider",
        "killbill", "proof",
    }

    for ordinal, (item, observation) in enumerate(zip(slots, observations), 1):
        item = object_(item, f"financial plan slot {ordinal}")
        observation = object_(observation, f"financial observation {ordinal}")
        require(set(observation) == observation_fields,
                f"financial slot {ordinal}: observation fields mismatch")
        require(item["slot"] == ordinal and observation["slot"] == ordinal,
                f"financial slot {ordinal}: slot order mismatch")
        for field in ("kind", "fault_class", "merchant_payment_id", "merchant_order_id"):
            require(observation[field] == item[field],
                    f"financial slot {ordinal}: plan {field} mismatch")

        payment_id = item["merchant_payment_id"]
        require(payment_id not in seen_payments,
                f"financial slot {ordinal}: duplicate Merchant payment identity")
        seen_payments.add(payment_id)

        merchant = object_(observation["merchant"], f"financial slot {ordinal} Merchant")
        gateway = object_(observation["gateway"], f"financial slot {ordinal} Gateway")
        provider = object_(observation["provider"], f"financial slot {ordinal} provider")
        killbill = object_(observation["killbill"], f"financial slot {ordinal} Kill Bill")

        mapping = object_(gateway.get("mapping"), f"financial slot {ordinal} Gateway mapping")
        provider_id = mapping.get("providerPaymentId")
        require(isinstance(provider_id, str) and provider_id,
                f"financial slot {ordinal}: provider mapping missing")
        require(provider.get("id") == provider_id,
                f"financial slot {ordinal}: provider identity mismatch")
        require(provider.get("amount") == 1000 and provider.get("currency") == "USD",
                f"financial slot {ordinal}: provider original amount/currency mismatch")
        require(type(provider.get("version")) is int and provider["version"] > 0,
                f"financial slot {ordinal}: provider version invalid")

        if item["kind"] == "fault_overlay":
            fault_count += 1
            proof = financial.canonical_fault_consistency(
                payment_id=payment_id,
                order_id=item["merchant_order_id"],
                fault_class=item["fault_class"],
                merchant=merchant,
                gateway=gateway,
                provider=provider,
                killbill=killbill,
            )
        else:
            normal_count += 1
            proof = financial.canonical_consistency(
                payment_id=payment_id,
                order_id=item["merchant_order_id"],
                case=item["kind"],
                merchant=merchant,
                gateway=gateway,
                provider=provider,
                killbill=killbill,
            )
        require(observation["proof"] == proof,
                f"financial slot {ordinal}: retained cross-system proof mismatch")

        expected = day_cardinality.expected(item)
        require(provider["status"] == expected["provider_status"],
                f"financial slot {ordinal}: provider EOD status differs from plan")
        require(provider["authorized_amount"] == expected["authorized_amount_minor"],
                f"financial slot {ordinal}: provider authorized amount differs from plan")
        require(provider["captured_amount"] == expected["captured_amount_minor"],
                f"financial slot {ordinal}: provider captured amount differs from plan")
        require(provider["refunded_amount"] == expected["refunded_amount_minor"],
                f"financial slot {ordinal}: provider refunded amount differs from plan")

        authorized_total += expected["authorized_amount_minor"]
        captured_total += expected["captured_amount_minor"]
        refunded_total += expected["refunded_amount_minor"]
        if expected["provider_status"] in {"captured", "refunded"}:
            settlement_eligible += 1
        proofs.append(proof)

    expected_normal = sum(item["kind"] != "fault_overlay" for item in slots)
    expected_fault = sum(item["kind"] == "fault_overlay" for item in slots)
    require(normal_count == expected_normal == 90,
            "financial invariant normal slot count mismatch")
    require(fault_count == expected_fault == 10,
            "financial invariant fault slot count mismatch")
    require(
        artifact["normal_slot_count"] == normal_count
        and artifact["fault_slot_count"] == fault_count,
        "financial invariant retained slot totals mismatch",
    )
    require(
        artifact["authorized_total_minor"] == authorized_total
        and artifact["captured_total_minor"] == captured_total
        and artifact["refunded_total_minor"] == refunded_total
        and artifact["net_settlement_total_minor"] == captured_total - refunded_total,
        "financial invariant retained financial totals mismatch",
    )
    require(artifact["settlement_eligible_count"] == settlement_eligible,
            "financial invariant settlement-eligible count mismatch")

    return {
        "observed_slot_count": 100,
        "normal_slot_count": normal_count,
        "fault_slot_count": fault_count,
        "authorized_total_minor": authorized_total,
        "captured_total_minor": captured_total,
        "refunded_total_minor": refunded_total,
        "net_settlement_total_minor": captured_total - refunded_total,
        "settlement_eligible_count": settlement_eligible,
        "slot_proofs_sha256": json_sha256(proofs),
    }
