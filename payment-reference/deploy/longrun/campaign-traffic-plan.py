#!/usr/bin/env python3
"""Deterministic Merchant-side primary traffic planner for #318.

Read-only planning only. Executing these requests requires separate controller,
fault-engine and independent financial verification integrations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

import campaign_core as evidence


SCHEMA = "oxide-batch-workloads.payment-reference-online-traffic-plan"
SCHEMA_VERSION = 1
AMOUNT_MINOR = 1000
CURRENCY = "USD"
# These are existing deterministic fault-matrix classes, not newly invented chaos.
FAULT_CLASSES = (
    "plan-503-before-side-effect",
    "post-commit-response-loss",
    "async-success-stale-convergence",
    "async-failure",
    "webhook-ordering-duplicate-drop-retry",
)
KINDS = (
    "auth_capture_purchase",
    "authorize_capture",
    "authorize_cancel",
    "auth_capture_refund",
    "fault_overlay",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise evidence.EvidenceError(message)


def mutation(payment: str, prefix: str, index: int, kind: str, amount: int) -> dict[str, Any]:
    return {
        "method": "POST",
        "path": f"/payments/{payment}/{kind}",
        "body": {
            "operationId": f"op-{prefix}-{index:02d}-{kind}",
            "amountMinor": amount,
            "idempotencyKey": f"idem-{prefix}-{index:02d}-{kind}",
        },
    }


def operations(kind: str, payment: str, prefix: str) -> list[dict[str, Any]]:
    if kind == "auth_capture_purchase":
        actions = (("auth-capture", AMOUNT_MINOR),)
    elif kind == "authorize_capture":
        actions = (("authorize", AMOUNT_MINOR), ("capture", 600), ("capture", 400))
    elif kind == "authorize_cancel":
        actions = (("authorize", AMOUNT_MINOR), ("cancel", AMOUNT_MINOR))
    elif kind == "auth_capture_refund":
        actions = (("auth-capture", AMOUNT_MINOR), ("refund", 250), ("refund", 750))
    elif kind == "fault_overlay":
        # Actual fault plans must be installed by the qualified engine first.
        actions = (("authorize", AMOUNT_MINOR),)
    else:
        raise evidence.EvidenceError(f"unrecognized traffic kind: {kind}")
    return [
        mutation(payment, prefix, index, action, amount)
        for index, (action, amount) in enumerate(actions, 1)
    ]


def build(campaign_id: str, sequence: int) -> dict[str, Any]:
    require(isinstance(campaign_id, str) and evidence.CAMPAIGN_ID.fullmatch(campaign_id) is not None, "unsafe campaign id")
    day = evidence.plan_day(sequence)
    contract_counts = day["traffic"]
    require(set(contract_counts) == set(KINDS), "unknown traffic class")

    # The SHA order is frozen across hosts/Python versions; no wall-clock input
    # or process-global pseudo-random state is involved.
    labeled = [
        (kind, ordinal)
        for kind in KINDS
        for ordinal in range(1, contract_counts[kind] + 1)
    ]
    labeled.sort(key=lambda pair: hashlib.sha256(
        f'{day["day_seed"]}:{pair[0]}:{pair[1]}'.encode("ascii")
    ).digest())

    require(len(labeled) == 100, "traffic count must be 100")
    merchant = f"m-{campaign_id}-d{sequence:06d}"
    customer = f"c-{campaign_id}-d{sequence:06d}"
    result = []
    fault_ordinal = 0

    for slot, (kind, category_ordinal) in enumerate(labeled, 1):
        prefix = f"{campaign_id}-d{sequence:06d}-s{slot:03d}"
        payment = f"p-{prefix}"
        order = f"o-{prefix}"
        fault_class = None
        if kind == "fault_overlay":
            fault_class = FAULT_CLASSES[
                (day["fault_overlay_rotation_offset"] + fault_ordinal) % len(FAULT_CLASSES)
            ]
            fault_ordinal += 1
        result.append({
            "slot": slot,
            "kind": kind,
            "kind_ordinal": category_ordinal,
            "merchant_payment_id": payment,
            "merchant_order_id": order,
            "fault_class": fault_class,
            "expected_normal_terminal_state": {
                "auth_capture_purchase": "PAID",
                "authorize_capture": "PAID",
                "authorize_cancel": "CANCELLED",
                "auth_capture_refund": "REFUNDED",
                "fault_overlay": None,
            }[kind],
            "create_order": {
                "method": "POST",
                "path": "/orders",
                "body": {
                    "merchantOrderId": order,
                    "merchantId": merchant,
                    "customerId": customer,
                    "amountMinor": AMOUNT_MINOR,
                    "currency": CURRENCY,
                },
            },
            "prepare_payment": {
                "method": "POST",
                "path": f"/orders/{order}/payments",
                "body": {
                    "merchantPaymentId": payment,
                    "amountMinor": AMOUNT_MINOR,
                    "currency": CURRENCY,
                    "paymentMethodToken": f"tok-{prefix}",
                },
            },
            "mutations": operations(kind, payment, prefix),
        })

    fault_counts = {c: sum(row["fault_class"] == c for row in result) for c in FAULT_CLASSES}
    require(all(count == 2 for count in fault_counts.values()), "fault overlays must cover five cases twice per day")
    ids = [row["merchant_payment_id"] for row in result]
    require(len(set(ids)) == 100, "payment identity collision")

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "logical_day_sequence": sequence,
        "logical_business_date": day["logical_business_date"],
        "day_seed": day["day_seed"],
        "plan_contract_sha256": evidence.sha256(evidence.CONTRACT_PATH),
        "primary_lifecycles": len(result),
        "traffic_counts": contract_counts,
        "fault_counts": fault_counts,
        "merchant_setup": [
            {"method": "POST", "path": "/merchants", "body": {"merchantId": merchant, "displayName": "Payment reference campaign merchant"}},
            {"method": "POST", "path": "/customers", "body": {"customerId": customer, "displayName": "Payment reference campaign customer"}},
        ],
        "slots": result,
        "executable": False,
        "external_verification_required": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#318 pure Merchant online request plan; no HTTP calls")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--day", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.campaign_id, args.day), sort_keys=True))
        return 0
    except (ValueError, KeyError, OverflowError, OSError) as exc:
        print(json.dumps({"decision": "BLOCK", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
