#!/usr/bin/env python3
"""#318 independent per-day cross-system financial invariant proof.

The verifier replays retained Merchant/Gateway/Kill Bill/provider snapshots for
all 100 deterministic slots. Provider mutation cardinality and recovery truth
remain separate authorities and are cross-bound by the day frontier.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable

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

        try:
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
        except financial.Block as exc:
            raise FinancialInvariantError(
                f"financial slot {ordinal}: cross-system invariant failed: {exc}"
            ) from exc
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


Reader = Callable[[str], dict[str, Any]]


def _slot_proof(
    *,
    item: dict[str, Any],
    merchant: dict[str, Any],
    gateway: dict[str, Any],
    provider: dict[str, Any],
    killbill: dict[str, Any],
) -> dict[str, Any]:
    try:
        if item["kind"] == "fault_overlay":
            return financial.canonical_fault_consistency(
                payment_id=item["merchant_payment_id"],
                order_id=item["merchant_order_id"],
                fault_class=item["fault_class"],
                merchant=merchant,
                gateway=gateway,
                provider=provider,
                killbill=killbill,
            )
        return financial.canonical_consistency(
            payment_id=item["merchant_payment_id"],
            order_id=item["merchant_order_id"],
            case=item["kind"],
            merchant=merchant,
            gateway=gateway,
            provider=provider,
            killbill=killbill,
        )
    except financial.Block as exc:
        raise FinancialInvariantError(
            f"financial slot {item['slot']}: cross-system invariant failed: {exc}"
        ) from exc


def collect(
    *,
    plan: dict[str, Any],
    source_revision: str,
    merchant_read: Reader,
    gateway_read: Reader,
    killbill_read: Reader,
    provider_read: Reader,
) -> dict[str, Any]:
    slots = array(plan.get("slots"), "financial invariant day plan slots")
    require(len(slots) == 100, "financial invariant collector requires exactly 100 slots")

    observations: list[dict[str, Any]] = []
    normal_count = 0
    fault_count = 0
    authorized_total = 0
    captured_total = 0
    refunded_total = 0
    settlement_eligible = 0

    for ordinal, raw_item in enumerate(slots, 1):
        item = object_(raw_item, f"financial plan slot {ordinal}")
        require(item.get("slot") == ordinal, f"financial slot {ordinal}: plan slot order mismatch")
        payment_id = item.get("merchant_payment_id")
        order_id = item.get("merchant_order_id")
        require(
            isinstance(payment_id, str)
            and financial.SAFE_ID.fullmatch(payment_id) is not None
            and isinstance(order_id, str)
            and financial.SAFE_ID.fullmatch(order_id) is not None,
            f"financial slot {ordinal}: unsafe payment/order identity",
        )

        merchant = object_(merchant_read(payment_id), f"financial slot {ordinal} Merchant")
        gateway = object_(gateway_read(payment_id), f"financial slot {ordinal} Gateway")
        mapping = object_(gateway.get("mapping"), f"financial slot {ordinal} Gateway mapping")
        killbill_id = mapping.get("killbillPaymentId")
        provider_id = mapping.get("providerPaymentId")
        require(
            isinstance(killbill_id, str) and financial.SAFE_ID.fullmatch(killbill_id) is not None,
            f"financial slot {ordinal}: unsafe Kill Bill payment identity",
        )
        require(
            isinstance(provider_id, str) and financial.SAFE_ID.fullmatch(provider_id) is not None,
            f"financial slot {ordinal}: unsafe provider payment identity",
        )
        killbill = object_(killbill_read(killbill_id), f"financial slot {ordinal} Kill Bill")
        provider = object_(provider_read(provider_id), f"financial slot {ordinal} provider")
        proof = _slot_proof(
            item=item,
            merchant=merchant,
            gateway=gateway,
            provider=provider,
            killbill=killbill,
        )

        expected = day_cardinality.expected(item)
        authorized_total += expected["authorized_amount_minor"]
        captured_total += expected["captured_amount_minor"]
        refunded_total += expected["refunded_amount_minor"]
        if expected["provider_status"] in {"captured", "refunded"}:
            settlement_eligible += 1
        if item["kind"] == "fault_overlay":
            fault_count += 1
        else:
            normal_count += 1

        observations.append({
            "slot": ordinal,
            "kind": item["kind"],
            "fault_class": item["fault_class"],
            "merchant_payment_id": payment_id,
            "merchant_order_id": order_id,
            "merchant": merchant,
            "gateway": gateway,
            "provider": provider,
            "killbill": killbill,
            "proof": proof,
        })

    artifact = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_revision,
        "day_plan_sha256": plan_digest(plan),
        "financial_semantics_sha256": file_sha256(FINANCIAL_VERIFIER),
        "observed_slot_count": len(observations),
        "normal_slot_count": normal_count,
        "fault_slot_count": fault_count,
        "authorized_total_minor": authorized_total,
        "captured_total_minor": captured_total,
        "refunded_total_minor": refunded_total,
        "net_settlement_total_minor": captured_total - refunded_total,
        "settlement_eligible_count": settlement_eligible,
        "observations": observations,
    }
    validate(artifact, plan=plan, source_revision=source_revision)
    return artifact


def load_campaign(campaign_dir: Path) -> tuple[str, str]:
    require(campaign_dir.is_dir() and not campaign_dir.is_symlink(), "unsafe campaign directory")
    record = object_(
        json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8")),
        "campaign",
    )
    require(
        record.get("schema") == "oxide-batch-workloads.payment-reference-campaign"
        and record.get("schema_version") == 1,
        "campaign schema mismatch",
    )
    require(record.get("contract") == core.contract()["contract"], "campaign contract identity mismatch")
    require(record.get("contract_sha256") == core.sha256(core.CONTRACT_PATH), "campaign contract digest mismatch")
    identity = object_(record.get("identity"), "campaign identity")
    campaign_id = identity.get("campaign_id")
    source_revision = identity.get("source_revision")
    require(
        campaign_id == campaign_dir.name
        and isinstance(campaign_id, str)
        and core.CAMPAIGN_ID.fullmatch(campaign_id) is not None,
        "campaign directory identity mismatch",
    )
    require(
        isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", source_revision) is not None,
        "campaign source revision invalid",
    )
    return campaign_id, source_revision


def live_readers(
    *,
    merchant_url: str,
    gateway_url: str,
    killbill_url: str,
) -> tuple[Reader, Reader, Reader, Reader]:
    merchant_base = financial.local_base(merchant_url)
    gateway_base = financial.local_base(gateway_url)
    killbill_base = financial.local_base(killbill_url)

    username = os.environ.get("KILLBILL_USERNAME", "")
    password = os.environ.get("KILLBILL_PASSWORD", "")
    api_key = os.environ.get("KILLBILL_API_KEY", "")
    api_secret = os.environ.get("KILLBILL_API_SECRET", "")
    require(all((username, password, api_key, api_secret)),
            "Kill Bill credentials must be supplied by environment")
    authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    killbill_headers = {
        "Authorization": authorization,
        "X-Killbill-ApiKey": api_key,
        "X-Killbill-ApiSecret": api_secret,
        "X-Killbill-CreatedBy": "payment-reference-campaign-verifier",
    }

    def merchant_read(payment_id: str) -> dict[str, Any]:
        return financial.local_json(merchant_base, f"/payments/{payment_id}")

    def gateway_read(payment_id: str) -> dict[str, Any]:
        return financial.local_json(gateway_base, f"/payments/{payment_id}")

    def killbill_read(payment_id: str) -> dict[str, Any]:
        return financial.local_json(
            killbill_base,
            f"/1.0/kb/payments/{payment_id}/?withPluginInfo=true",
            killbill_headers,
        )

    def provider_read(payment_id: str) -> dict[str, Any]:
        return financial.provider_json(payment_id)

    return merchant_read, gateway_read, killbill_read, provider_read


def retain_immutable(path: Path, value: Any) -> str:
    require(not path.is_symlink(), "financial invariant output path must not be a symlink")
    raw = core.canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        require(path.is_file() and not path.is_symlink(), "existing financial invariant output is unsafe")
        require(path.read_bytes() == raw, "immutable financial invariant verification conflict")
        return "replay"
    with os.fdopen(fd, "wb") as handle:
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
    parser = argparse.ArgumentParser(
        description="Collect one logical day's read-only cross-system financial invariants"
    )
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--merchant-url", required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--killbill-url", required=True)
    args = parser.parse_args()

    try:
        require(type(args.day) is int and args.day > 0, "day must be positive")
        campaign_id, source_revision = load_campaign(args.campaign_dir)
        plan = day_cardinality.plan_for(campaign_id, args.day)
        readers = live_readers(
            merchant_url=args.merchant_url,
            gateway_url=args.gateway_url,
            killbill_url=args.killbill_url,
        )
        artifact = collect(
            plan=plan,
            source_revision=source_revision,
            merchant_read=readers[0],
            gateway_read=readers[1],
            killbill_read=readers[2],
            provider_read=readers[3],
        )
        output = (
            args.campaign_dir
            / "days"
            / f"day-{args.day:06}.financial-invariants.verification.json"
        )
        retention = retain_immutable(output, artifact)
        subject = validate(artifact, plan=plan, source_revision=source_revision)
        print(json.dumps({
            "decision": DECISION,
            "verification_path": str(output),
            "retention": retention,
            "observed_slot_count": subject["observed_slot_count"],
            "settlement_eligible_count": subject["settlement_eligible_count"],
            "net_settlement_total_minor": subject["net_settlement_total_minor"],
        }, sort_keys=True))
        return 0
    except (
        FinancialInvariantError,
        financial.Block,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        financial.subprocess.TimeoutExpired,
        financial.urllib.error.URLError,
    ) as exc:
        print(
            json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
