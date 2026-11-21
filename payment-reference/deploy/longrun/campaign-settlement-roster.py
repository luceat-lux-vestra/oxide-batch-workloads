#!/usr/bin/env python3
"""#318 canonical per-day provider settlement roster builder.

The builder is read-only against Gateway and direct payment-sandbox provider
truth. It never derives membership from controller intent, Kill Bill journals,
or fault-proxy forwarding counters.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

import campaign_fault_catalog as fault_catalog

HERE = Path(__file__).resolve().parent
PAYMENT_REFERENCE = HERE.parent.parent
STATEMENT_PRODUCER = PAYMENT_REFERENCE / "scenarios" / "v1" / "produce-provider-settlement-statement.py"
TRAFFIC_PLANNER = HERE / "campaign-traffic-plan.py"
CAMPAIGN_EVIDENCE = HERE / "campaign-evidence.py"

VERIFICATION_SCHEMA = "oxide-batch-workloads.payment-reference-settlement-roster-verification"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
HEX40 = re.compile(r"[0-9a-f]{40}")
ELIGIBLE_PROVIDER_STATUSES = {"captured", "refunded"}
NORMAL_EXPECTED_PROVIDER = {
    "auth_capture_purchase": "captured",
    "authorize_capture": "captured",
    "authorize_cancel": "cancelled",
    "auth_capture_refund": "refunded",
}


class RosterError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RosterError(message)


def object_(value: Any, name: str) -> dict[str, Any]:
    require(type(value) is dict, f"{name}: expected object")
    return value


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    search_path = str(HERE)
    inserted = search_path not in sys.path
    if inserted:
        sys.path.insert(0, search_path)
    try:
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(search_path)
    return module


statement = load_module("payment_reference_settlement_statement", STATEMENT_PRODUCER)
traffic = load_module("payment_reference_campaign_traffic", TRAFFIC_PLANNER)
evidence = load_module("payment_reference_campaign_evidence", CAMPAIGN_EVIDENCE)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_campaign(campaign_dir: Path) -> tuple[str, str]:
    require(campaign_dir.is_dir() and not campaign_dir.is_symlink(), "unsafe campaign directory")
    record = object_(json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8")), "campaign record")
    require(record.get("schema") == evidence.SCHEMA and record.get("schema_version") == evidence.SCHEMA_VERSION, "campaign schema mismatch")
    require(record.get("contract_sha256") == evidence.sha256(evidence.CONTRACT_PATH), "campaign contract digest mismatch")
    identity = object_(record.get("identity"), "campaign identity")
    evidence.verify_identity(identity)
    campaign_id = identity["campaign_id"]
    source_revision = identity["source_revision"]
    require(campaign_dir.name == campaign_id, "campaign directory identity mismatch")
    require(HEX40.fullmatch(source_revision) is not None, "campaign source revision invalid")
    return campaign_id, source_revision


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RosterError(f"GET redirect refused: HTTP {code}")


def loopback_base(url: str, expected_port: int, role: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1", f"{role} must use HTTP loopback 127.0.0.1")
    require(parsed.username is None and parsed.password is None, f"{role} URL credentials forbidden")
    require(parsed.path in ("", "/") and not parsed.query and not parsed.fragment, f"{role} base URL cannot contain path/query")
    require(parsed.port == expected_port, f"{role} port must be {expected_port}")
    return f"http://127.0.0.1:{expected_port}"


def http_json(base: str, path: str, *, allow_404: bool) -> tuple[int, dict[str, Any] | None]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        base + path,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=8) as response:
            require(response.status == 200, f"GET {path}: unexpected HTTP {response.status}")
            data = response.read(2_000_001)
            require(len(data) <= 2_000_000, f"GET {path}: oversized JSON")
            return 200, object_(json.loads(data), f"GET {path}")
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return 404, None
        raise RosterError(f"GET {path}: HTTP {exc.code}") from exc


def expected_provider_status(item: dict[str, Any]) -> str:
    kind = item["kind"]
    if kind in NORMAL_EXPECTED_PROVIDER:
        return NORMAL_EXPECTED_PROVIDER[kind]
    require(kind == "fault_overlay", f"unknown traffic kind: {kind}")
    fault_class = item.get("fault_class")
    require(isinstance(fault_class, str), "fault overlay missing fault class")
    expected = fault_catalog.end_of_day_provider_truth(fault_class)
    require(expected in {"AUTHORIZED", "FAILED"}, f"unsupported end-of-day fault provider truth: {expected}")
    return expected.lower()


def validate_provider_payment(
    provider_payment_id: str,
    payload: dict[str, Any],
    currency: str,
) -> dict[str, Any]:
    required = {
        "id", "amount", "currency", "status",
        "authorized_amount", "captured_amount", "refunded_amount", "version",
    }
    require(required <= set(payload), f"provider {provider_payment_id}: missing payment fields")
    require(payload["id"] == provider_payment_id, "provider payment identity mismatch")
    require(payload["currency"] == currency, f"provider {provider_payment_id}: currency mismatch")
    for field in ("amount", "authorized_amount", "captured_amount", "refunded_amount", "version"):
        value = payload[field]
        require(type(value) is int and value >= 0, f"provider {provider_payment_id}: {field} must be nonnegative integer")
    require(payload["amount"] == 1000, f"provider {provider_payment_id}: campaign amount drift")
    require(payload["version"] > 0, f"provider {provider_payment_id}: version must be positive")
    require(
        0 <= payload["refunded_amount"] <= payload["captured_amount"] <= payload["authorized_amount"] <= payload["amount"],
        f"provider {provider_payment_id}: amount invariant violation",
    )
    status = payload["status"]
    require(
        status in {"pending", "authorized", "failed", "cancelled", "captured", "refunded"},
        f"provider {provider_payment_id}: unsupported/nonfinal status {status!r}",
    )
    return payload


def collect(
    *,
    plan: dict[str, Any],
    source_revision: str,
    provider_account_id: str,
    gateway_read: Callable[[str], dict[str, Any]],
    provider_read: Callable[[str], tuple[int, dict[str, Any] | None]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    require(HEX40.fullmatch(source_revision) is not None, "source revision must be lowercase 40-hex")
    require(isinstance(provider_account_id, str) and provider_account_id.strip(), "provider account id must be non-blank")
    require(plan.get("schema") == traffic.SCHEMA and plan.get("schema_version") == traffic.SCHEMA_VERSION, "traffic plan schema mismatch")
    require(plan.get("primary_lifecycles") == 100 and len(plan.get("slots", [])) == 100, "settlement roster requires complete 100-slot day plan")
    currency = "USD"
    observations = []
    eligible = []
    provider_ids = set()

    for expected_slot, item in enumerate(plan["slots"], 1):
        require(item.get("slot") == expected_slot, "traffic slot sequence mismatch")
        merchant_payment_id = item["merchant_payment_id"]
        require(SAFE_ID.fullmatch(merchant_payment_id) is not None, "unsafe merchant payment id")
        gateway = object_(gateway_read(merchant_payment_id), f"Gateway {merchant_payment_id}")
        require(gateway.get("merchantPaymentId") == merchant_payment_id, "Gateway payment identity mismatch")
        if item["kind"] in NORMAL_EXPECTED_PROVIDER:
            require(
                gateway.get("paymentState") == item["expected_normal_terminal_state"],
                f"slot {expected_slot}: Gateway normal terminal state mismatch",
            )
            require(
                gateway.get("lastOperationState") == "SUCCEEDED",
                f"slot {expected_slot}: Gateway normal operation is not SUCCEEDED",
            )
        mapping = object_(gateway.get("mapping"), f"Gateway mapping {merchant_payment_id}")
        require(mapping.get("merchantPaymentId") == merchant_payment_id, "Gateway mapping payment identity mismatch")
        provider_payment_id = mapping.get("providerPaymentId")
        require(
            isinstance(provider_payment_id, str) and SAFE_ID.fullmatch(provider_payment_id) is not None,
            "Gateway provider mapping missing/unsafe",
        )
        require(provider_payment_id not in provider_ids, "provider payment mapping collision across campaign slots")
        provider_ids.add(provider_payment_id)

        status_code, provider = provider_read(provider_payment_id)
        expected_status = expected_provider_status(item)
        if status_code == 404:
            require(provider is None, "provider 404 must not include payload")
            provider_status = "ABSENT"
            version = None
            captured = 0
            refunded = 0
        else:
            require(status_code == 200 and provider is not None, "provider read must be HTTP 200 or 404")
            provider = validate_provider_payment(provider_payment_id, provider, currency)
            provider_status = provider["status"]
            version = provider["version"]
            captured = provider["captured_amount"]
            refunded = provider["refunded_amount"]

        require(
            provider_status == expected_status,
            f"slot {expected_slot}: provider truth {provider_status!r} != expected {expected_status!r}",
        )

        is_eligible = provider_status in ELIGIBLE_PROVIDER_STATUSES
        if is_eligible:
            require(captured == 1000, f"slot {expected_slot}: settlement-eligible capture must equal 1000")
            if provider_status == "captured":
                require(refunded == 0, f"slot {expected_slot}: captured payment cannot already be refunded")
            else:
                require(refunded == 1000, f"slot {expected_slot}: refunded campaign payment must be fully refunded")
            eligible.append(provider_payment_id)
        else:
            require(captured == 0 and refunded == 0, f"slot {expected_slot}: noneligible payment carries settlement amounts")

        observations.append({
            "slot": expected_slot,
            "kind": item["kind"],
            "fault_class": item.get("fault_class"),
            "merchant_payment_id": merchant_payment_id,
            "provider_payment_id": provider_payment_id,
            "provider_status": provider_status,
            "provider_version": version,
            "captured_amount_minor": captured,
            "refunded_amount_minor": refunded,
            "settlement_eligible": is_eligible,
        })

    eligible = sorted(eligible)
    roster = statement.validate_roster({
        "contract_version": statement.ROSTER_VERSION,
        "scenario_version": 1,
        "scenario_seed": plan["day_seed"],
        "source_revision": source_revision,
        "provider_account_id": provider_account_id,
        "settlement_date": plan["logical_business_date"],
        "currency": currency,
        "provider_payment_ids": eligible,
    })
    verification = {
        "schema": VERIFICATION_SCHEMA,
        "schema_version": 1,
        "decision": "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED",
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_revision,
        "day_plan_sha256": sha256(plan),
        "plan_contract_sha256": plan["plan_contract_sha256"],
        "fault_catalog_sha256": plan["fault_catalog_sha256"],
        "provider_account_id": provider_account_id,
        "settlement_date": plan["logical_business_date"],
        "currency": currency,
        "observed_slot_count": len(observations),
        "eligible_payment_count": len(eligible),
        "roster_sha256": statement.sha256_digest(roster),
        "observations": observations,
    }
    validate_verification(verification, roster, plan)
    return roster, verification


def validate_verification(
    value: Any,
    roster: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    value = object_(value, "settlement roster verification")
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "day_plan_sha256",
        "plan_contract_sha256", "fault_catalog_sha256", "provider_account_id",
        "settlement_date", "currency", "observed_slot_count",
        "eligible_payment_count", "roster_sha256", "observations",
    }
    require(set(value) == expected_fields, "settlement roster verification fields mismatch")
    require(value["schema"] == VERIFICATION_SCHEMA and value["schema_version"] == 1, "settlement roster verification schema mismatch")
    require(value["decision"] == "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED", "settlement roster verification decision mismatch")
    require(value["campaign_id"] == plan["campaign_id"], "settlement roster campaign mismatch")
    require(value["logical_day_sequence"] == plan["logical_day_sequence"], "settlement roster day mismatch")
    require(value["source_revision"] == roster["source_revision"], "settlement roster source revision mismatch")
    require(value["day_plan_sha256"] == sha256(plan), "settlement roster day plan digest mismatch")
    require(value["plan_contract_sha256"] == plan["plan_contract_sha256"], "settlement roster contract digest mismatch")
    require(value["fault_catalog_sha256"] == plan["fault_catalog_sha256"], "settlement roster fault catalog digest mismatch")
    require(value["provider_account_id"] == roster["provider_account_id"], "settlement roster provider account mismatch")
    require(value["settlement_date"] == roster["settlement_date"], "settlement roster date mismatch")
    require(value["currency"] == roster["currency"], "settlement roster currency mismatch")
    observations = value["observations"]
    require(isinstance(observations, list) and len(observations) == 100, "settlement roster must verify exactly 100 slots")
    require(value["observed_slot_count"] == 100, "observed slot count mismatch")
    require([row["slot"] for row in observations] == list(range(1, 101)), "settlement observations must be ordered 1..100")
    observed_eligible = sorted(row["provider_payment_id"] for row in observations if row["settlement_eligible"])
    require(observed_eligible == roster["provider_payment_ids"], "settlement roster membership differs from verified observations")
    require(value["eligible_payment_count"] == len(observed_eligible), "eligible payment count mismatch")
    require(value["roster_sha256"] == statement.sha256_digest(roster), "settlement roster digest mismatch")
    return value


def render_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def retain_immutable(path: Path, value: Any) -> str:
    require(not path.is_symlink(), f"immutable path must not be a symlink: {path}")
    raw = render_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        require(not path.is_symlink(), f"immutable path became symlink: {path}")
        existing = path.read_bytes()
        require(existing == raw, f"immutable content conflict: {path}")
        return "replay"
    with os.fdopen(fd, "wb") as file:
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return "created"


def require_distinct_outputs(roster_path: Path, verification_path: Path) -> None:
    require(
        os.path.abspath(roster_path) != os.path.abspath(verification_path),
        "roster and verification outputs must be distinct paths",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 independent per-day settlement roster builder")
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--provider-account-id", required=True)
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--provider-url", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verification-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        require(type(args.day) is int and args.day > 0, "day must be positive")
        require_distinct_outputs(args.output, args.verification_output)
        campaign_id, source_revision = load_campaign(args.campaign_dir)
        plan = traffic.build(campaign_id, args.day)
        gateway_port = int(os.environ.get("GATEWAY_PORT", "18082"))
        provider_port = int(os.environ.get("PAYMENT_SANDBOX_DIRECT_PORT", "18084"))
        gateway_base = loopback_base(args.gateway_url or f"http://127.0.0.1:{gateway_port}", gateway_port, "Gateway")
        provider_base = loopback_base(args.provider_url or f"http://127.0.0.1:{provider_port}", provider_port, "provider")

        def gateway_read(payment_id: str) -> dict[str, Any]:
            status, body = http_json(
                gateway_base,
                "/payments/" + urllib.parse.quote(payment_id, safe=""),
                allow_404=False,
            )
            require(status == 200 and body is not None, "Gateway payment missing")
            return body

        def provider_read(payment_id: str) -> tuple[int, dict[str, Any] | None]:
            return http_json(
                provider_base,
                "/payments/" + urllib.parse.quote(payment_id, safe=""),
                allow_404=True,
            )

        roster, verification = collect(
            plan=plan,
            source_revision=source_revision,
            provider_account_id=args.provider_account_id,
            gateway_read=gateway_read,
            provider_read=provider_read,
        )
        roster_state = retain_immutable(args.output, roster)
        verification_state = retain_immutable(args.verification_output, verification)
        print(json.dumps({
            "decision": "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED",
            "roster_path": str(args.output),
            "verification_path": str(args.verification_output),
            "roster_retention": roster_state,
            "verification_retention": verification_state,
            "roster_sha256": verification["roster_sha256"],
            "eligible_payment_count": verification["eligible_payment_count"],
        }, sort_keys=True))
        return 0
    except (
        OSError, ValueError, KeyError, TypeError, OverflowError,
        json.JSONDecodeError, urllib.error.URLError,
    ) as exc:
        print(json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
