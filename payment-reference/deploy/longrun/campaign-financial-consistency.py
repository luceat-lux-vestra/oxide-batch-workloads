#!/usr/bin/env python3
"""#318: read-only cross-system payment consistency probe.

A consistent GET snapshot is NOT accepted financial campaign evidence: it does
not independently validate provider mutation cardinality, settlement or jobs.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "oxide-batch-workloads.payment-reference-cross-system-consistency"
KINDS = {"auth_capture_purchase", "authorize_capture", "authorize_cancel", "auth_capture_refund"}
SAFE_ID = re.compile(r"[a-zA-Z0-9_-]{1,120}\Z")
EXPECTED = {
    "auth_capture_purchase": ("PAID", "captured", {"PURCHASE": 1}, 1000, 0),
    "authorize_capture": ("PAID", "captured", {"AUTHORIZE": 1, "CAPTURE": 2}, 1000, 0),
    "authorize_cancel": ("CANCELLED", "cancelled", {"AUTHORIZE": 1, "VOID": 1}, 0, 0),
    "auth_capture_refund": ("REFUNDED", "refunded", {"PURCHASE": 1, "REFUND": 2}, 1000, 1000),
}


class Block(ValueError):
    pass


def demand(ok: bool, reason: str) -> None:
    if not ok:
        raise Block(reason)


def object_(value: Any, name: str) -> dict[str, Any]:
    demand(type(value) is dict, f"{name}: expected object")
    return value


def canonical_consistency(
    *,
    payment_id: str,
    order_id: str,
    case: str,
    merchant: dict[str, Any],
    gateway: dict[str, Any],
    provider: dict[str, Any],
    killbill: dict[str, Any],
) -> dict[str, Any]:
    """Pure exact-state invariant checker; no generated PASS campaign artifact."""
    demand(isinstance(payment_id, str) and SAFE_ID.fullmatch(payment_id) is not None, "invalid payment identity")
    demand(isinstance(order_id, str) and SAFE_ID.fullmatch(order_id) is not None, "invalid order identity")
    demand(case in KINDS, "unsupported/ambiguous case; independently reconcile first")
    for name, value in (("merchant", merchant), ("gateway", gateway), ("provider", provider), ("killbill", killbill)):
        object_(value, name)
    expected_state, provider_status, expected_transactions, captured, refunded = EXPECTED[case]

    demand(merchant.get("merchantPaymentId") == payment_id, "Merchant payment identity conflict")
    demand(merchant.get("merchantOrderId") == order_id, "Merchant order identity conflict")
    demand(type(merchant.get("amountMinor")) is int and merchant["amountMinor"] == 1000, "Merchant prepared amount drift")
    demand(merchant.get("currency") == "USD", "Merchant currency drift")
    demand(merchant.get("paymentState") == expected_state, "Merchant terminal state conflict")
    demand(gateway.get("merchantPaymentId") == payment_id, "Gateway payment identity conflict")
    demand(gateway.get("paymentState") == expected_state, "Gateway terminal state conflict")
    demand(gateway.get("lastOperationState") == "SUCCEEDED", "Gateway operation not final")
    mapping = object_(gateway.get("mapping"), "Gateway mapping")
    demand(mapping.get("merchantPaymentId") == payment_id, "Gateway mapping payment identity conflict")
    kb_id = mapping.get("killbillPaymentId")
    provider_id = mapping.get("providerPaymentId")
    demand(isinstance(kb_id, str) and SAFE_ID.fullmatch(kb_id) is not None, "missing Kill Bill payment mapping")
    demand(isinstance(provider_id, str) and SAFE_ID.fullmatch(provider_id) is not None, "missing provider payment mapping")
    demand(killbill.get("paymentId") == kb_id, "Kill Bill payment identity conflict")
    demand(provider.get("status") == provider_status, "provider terminal state conflict")
    for field, amount in (("authorized_amount", 1000), ("captured_amount", captured), ("refunded_amount", refunded)):
        demand(type(provider.get(field)) is int and provider[field] == amount, f"provider {field} mismatch")
    demand(0 <= provider["refunded_amount"] <= provider["captured_amount"] <= provider["authorized_amount"], "provider amount invariant violation")
    transactions = killbill.get("transactions")
    demand(isinstance(transactions, list) and len(transactions) == sum(expected_transactions.values()), "Kill Bill transaction cardinality mismatch")
    observed: dict[str, int] = {}
    for transaction in transactions:
        row = object_(transaction, "Kill Bill transaction")
        kind = row.get("transactionType")
        demand(isinstance(kind, str) and kind in expected_transactions, "unexpected Kill Bill transaction type")
        observed[kind] = observed.get(kind, 0) + 1
        demand(row.get("transactionStatus") == "SUCCESS", "Kill Bill transaction not successful")
    demand(observed == expected_transactions, "Kill Bill transaction type cardinality mismatch")
    return {
        "schema": SCHEMA, "schema_version": 1,
        "decision": "CROSS_SYSTEM_CONSISTENT_NOT_CAMPAIGN_ACCEPTED",
        "payment_id": payment_id,
        "order_id": order_id,
        "case": case,
        "killbill_payment_id": kb_id,
        "provider_payment_id": provider_id,
        "authorized_amount_minor": 1000,
        "captured_amount_minor": captured,
        "refunded_amount_minor": refunded,
        "net_settlement_amount_minor": captured - refunded,
        "killbill_transactions_by_type": observed,
        "unverified": [
            "provider_mutation_side_effect_cardinality",
            "provider_settlement_statement",
            "merchant_settlement_close",
            "batch_reconciliation_recovery_truth",
            "campaign_evidence_ownership",
        ],
    }


def local_base(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    demand(parsed.scheme == "http" and parsed.hostname == "127.0.0.1", "only HTTP loopback 127.0.0.1 is allowed")
    demand(parsed.username is None and parsed.password is None, "credentials in URL forbidden")
    demand(parsed.path in ("", "/") and not parsed.query and not parsed.fragment, "base URL cannot have a path/query")
    demand(parsed.port in {18082, 18083, 8080}, "nonqualified loopback port")
    return f"http://127.0.0.1:{parsed.port}"


def local_json(base: str, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    # Never follow an attacker-controlled redirect or a configured HTTP proxy.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    req = urllib.request.Request(base + path, headers=headers or {}, method="GET")
    with opener.open(req, timeout=8) as response:
        demand(response.status == 200, f"GET {path}: HTTP {response.status}")
        data = response.read(2_000_001)
        demand(len(data) <= 2_000_000, f"GET {path}: oversized JSON")
        return object_(json.loads(data), f"GET {path}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Block(f"GET redirect refused: HTTP {code}")


def provider_json(provider_id: str) -> dict[str, Any]:
    # One read-only request inside the known existing Compose project service.
    result = subprocess.run(
        ["docker", "exec", "oxide-payment-reference-payment-sandbox-1",
         "curl", "--fail", "--silent", "--show-error", "--max-time", "8",
         f"http://127.0.0.1:8080/payments/{provider_id}"],
        capture_output=True, timeout=12, check=False,
    )
    demand(result.returncode == 0 and len(result.stdout) <= 2_000_000, "provider GET failed/oversized")
    return object_(json.loads(result.stdout), "provider GET")


def collect_and_verify(payment_id: str, order_id: str, case: str, merchant_url: str, gateway_url: str, kb_url: str) -> dict[str, Any]:
    merchant_base, gateway_base, kb_base = map(local_base, (merchant_url, gateway_url, kb_url))
    demand([urllib.parse.urlsplit(v).port for v in (merchant_base, gateway_base, kb_base)] == [18083, 18082, 8080], "Merchant/Gateway/Kill Bill ports must be distinct and canonical")
    merchant = local_json(merchant_base, f"/payments/{payment_id}")
    gateway = local_json(gateway_base, f"/payments/{payment_id}")
    mapping = object_(gateway.get("mapping"), "Gateway mapping")
    kb_id, provider_id = mapping.get("killbillPaymentId"), mapping.get("providerPaymentId")
    demand(isinstance(kb_id, str) and SAFE_ID.fullmatch(kb_id) is not None, "unsafe Kill Bill payment ID")
    demand(isinstance(provider_id, str) and SAFE_ID.fullmatch(provider_id) is not None, "unsafe provider payment ID")
    username = os.environ.get("KILLBILL_USERNAME", "")
    password = os.environ.get("KILLBILL_PASSWORD", "")
    api_key = os.environ.get("KILLBILL_API_KEY", "")
    api_secret = os.environ.get("KILLBILL_API_SECRET", "")
    demand(all((username, password, api_key, api_secret)), "Kill Bill credentials must be supplied by environment")
    basic = base64.b64encode(f"{username}:{password}".encode()).decode()
    kb = local_json(kb_base, f"/1.0/kb/payments/{kb_id}/?withPluginInfo=true", {
        "Authorization": f"Basic {basic}",
        "X-Killbill-ApiKey": api_key,
        "X-Killbill-ApiSecret": api_secret,
        "X-Killbill-CreatedBy": "payment-reference-campaign-verifier",
    })
    provider = provider_json(provider_id)
    return canonical_consistency(
        payment_id=payment_id, order_id=order_id, case=case,
        merchant=merchant, gateway=gateway, provider=provider, killbill=kb,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only payment consistency (not a campaign PASS)")
    parser.add_argument("--payment-id", required=True)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--case", choices=sorted(KINDS), required=True)
    parser.add_argument("--merchant-url", default="http://127.0.0.1:18083")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18082")
    parser.add_argument("--killbill-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    try:
        demand(SAFE_ID.fullmatch(args.payment_id) is not None and SAFE_ID.fullmatch(args.order_id) is not None, "unsafe payment/order identity")
        result = collect_and_verify(args.payment_id, args.order_id, args.case, args.merchant_url, args.gateway_url, args.killbill_url)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Block, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired, urllib.error.URLError) as e:
        # Do not echo request URLs containing or referencing credentials.
        print(json.dumps({"decision": "BLOCK", "reason": type(e).__name__}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
