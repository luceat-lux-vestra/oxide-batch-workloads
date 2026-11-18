#!/usr/bin/env python3
"""Deterministic provider settlement statement producer.

Input:
  --roster PATH
      settlement-roster-v1 JSON.
  --provider-base-url URL
      Direct payment-sandbox provider-truth base URL. The producer performs
      read-only GET /payments/{provider_payment_id}; it must not point at
      Merchant, Gateway, Kill Bill, plugin-private storage, or a fault proxy.
  --output PATH
      Optional output path. "-" (default) writes canonical JSON to stdout.

Digest/identity contract:
  * statement_id = "statement-" + sha256(canonical JSON statement identity)
  * line_id = "line-" + sha256(canonical JSON {statement_id, provider_payment_id})
  * source_roster_digest = "sha256:" + sha256(canonical JSON roster)
  * statement_digest = "sha256:" + sha256(canonical JSON statement with the
    statement_digest field omitted)

This producer intentionally does not implement retained-file replay/conflict
semantics. That is activation gate 3.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

I64_MAX = 9_223_372_036_854_775_807
ROSTER_VERSION = "settlement-roster-v1"
STATEMENT_VERSION = "provider-settlement-statement-v1"
ELIGIBLE_PROVIDER_STATUSES = {"captured", "refunded"}
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class SettlementStatementError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def deterministic_id(prefix: str, value: Any) -> str:
    return prefix + "-" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def checked_nonnegative_i64(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettlementStatementError(f"{field} must be an integer")
    if value < 0 or value > I64_MAX:
        raise SettlementStatementError(f"{field} must be in [0, {I64_MAX}]")
    return value


def checked_add(left: int, right: int, field: str) -> int:
    total = left + right
    if total > I64_MAX:
        raise SettlementStatementError(f"{field} exceeds signed 64-bit range")
    return total


def validate_roster(roster: Any) -> dict[str, Any]:
    if not isinstance(roster, dict):
        raise SettlementStatementError("roster must be a JSON object")

    expected_fields = {
        "contract_version",
        "scenario_version",
        "scenario_seed",
        "source_revision",
        "provider_account_id",
        "settlement_date",
        "currency",
        "provider_payment_ids",
    }
    if set(roster) != expected_fields:
        raise SettlementStatementError(
            "roster fields must exactly match settlement-roster-v1"
        )
    if roster["contract_version"] != ROSTER_VERSION:
        raise SettlementStatementError(
            f"contract_version must be {ROSTER_VERSION}"
        )
    scenario_version = roster["scenario_version"]
    if (
        isinstance(scenario_version, bool)
        or not isinstance(scenario_version, int)
        or scenario_version < 1
    ):
        raise SettlementStatementError("scenario_version must be an integer >= 1")
    scenario_seed = roster["scenario_seed"]
    if (
        isinstance(scenario_seed, bool)
        or not isinstance(scenario_seed, int)
        or scenario_seed < 0
    ):
        raise SettlementStatementError("scenario_seed must be an integer >= 0")
    if (
        not isinstance(roster["source_revision"], str)
        or not REVISION_RE.fullmatch(roster["source_revision"])
    ):
        raise SettlementStatementError("source_revision must be lowercase 40-hex")
    if (
        not isinstance(roster["provider_account_id"], str)
        or not roster["provider_account_id"].strip()
    ):
        raise SettlementStatementError("provider_account_id must not be blank")
    try:
        settlement_date = dt.date.fromisoformat(roster["settlement_date"])
    except (TypeError, ValueError) as exc:
        raise SettlementStatementError(
            "settlement_date must be an ISO date"
        ) from exc
    if settlement_date.isoformat() != roster["settlement_date"]:
        raise SettlementStatementError("settlement_date must use YYYY-MM-DD")
    if (
        not isinstance(roster["currency"], str)
        or not CURRENCY_RE.fullmatch(roster["currency"])
    ):
        raise SettlementStatementError(
            "currency must be an uppercase three-letter code"
        )

    payment_ids = roster["provider_payment_ids"]
    if not isinstance(payment_ids, list) or not payment_ids:
        raise SettlementStatementError(
            "provider_payment_ids must be a non-empty array"
        )
    if any(
        not isinstance(item, str) or not item.strip()
        for item in payment_ids
    ):
        raise SettlementStatementError(
            "provider_payment_ids entries must be non-blank strings"
        )
    if len(payment_ids) != len(set(payment_ids)):
        raise SettlementStatementError("provider_payment_ids must be unique")
    if payment_ids != sorted(payment_ids):
        raise SettlementStatementError(
            "provider_payment_ids must be lexicographically sorted"
        )

    return roster


def load_roster(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettlementStatementError(
            f"unable to read roster {path}: {exc}"
        ) from exc
    return validate_roster(value)


def fetch_provider_payment(
    base_url: str,
    payment_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    url = (
        base_url.rstrip("/")
        + "/payments/"
        + urllib.parse.quote(payment_id, safe="")
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds,
        ) as response:
            if response.status != 200:
                raise SettlementStatementError(
                    f"provider GET {payment_id} returned HTTP {response.status}"
                )
            payload = json.load(response)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise SettlementStatementError(
            f"provider GET {payment_id} failed: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise SettlementStatementError(
            f"provider GET {payment_id} must return a JSON object"
        )
    return payload


def build_line(
    statement_id: str,
    roster_currency: str,
    expected_payment_id: str,
    payment: dict[str, Any],
) -> dict[str, Any]:
    required = {
        "id",
        "currency",
        "status",
        "captured_amount",
        "refunded_amount",
        "version",
    }
    missing = sorted(required - set(payment))
    if missing:
        raise SettlementStatementError(
            f"provider payment {expected_payment_id} missing fields: {missing}"
        )
    if payment["id"] != expected_payment_id:
        raise SettlementStatementError(
            "provider payment identity mismatch: "
            f"expected {expected_payment_id}, got {payment['id']!r}"
        )
    if payment["currency"] != roster_currency:
        raise SettlementStatementError(
            f"provider payment {expected_payment_id} currency mismatch: "
            f"expected {roster_currency}, got {payment['currency']!r}"
        )
    if payment["status"] not in ELIGIBLE_PROVIDER_STATUSES:
        raise SettlementStatementError(
            f"provider payment {expected_payment_id} is not settlement-eligible: "
            f"status={payment['status']!r}"
        )

    captured = checked_nonnegative_i64(
        payment["captured_amount"],
        f"{expected_payment_id}.captured_amount",
    )
    refunded = checked_nonnegative_i64(
        payment["refunded_amount"],
        f"{expected_payment_id}.refunded_amount",
    )
    if refunded > captured:
        raise SettlementStatementError(
            f"provider payment {expected_payment_id} "
            "refunded_amount exceeds captured_amount"
        )
    provider_version = checked_nonnegative_i64(
        payment["version"],
        f"{expected_payment_id}.version",
    )
    net = captured - refunded

    return {
        "line_id": deterministic_id(
            "line",
            {
                "statement_id": statement_id,
                "provider_payment_id": expected_payment_id,
            },
        ),
        "provider_payment_id": expected_payment_id,
        "provider_version": provider_version,
        "currency": roster_currency,
        "captured_amount_minor": captured,
        "refunded_amount_minor": refunded,
        "net_settlement_amount_minor": net,
    }


def produce_statement(
    roster: dict[str, Any],
    provider_base_url: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    identity = {
        "statement_contract_version": STATEMENT_VERSION,
        "provider_account_id": roster["provider_account_id"],
        "settlement_date": roster["settlement_date"],
        "currency": roster["currency"],
    }
    statement_id = deterministic_id("statement", identity)

    lines = []
    captured_total = 0
    refunded_total = 0
    net_total = 0

    for payment_id in roster["provider_payment_ids"]:
        payment = fetch_provider_payment(
            provider_base_url,
            payment_id,
            timeout_seconds,
        )
        line = build_line(
            statement_id,
            roster["currency"],
            payment_id,
            payment,
        )
        lines.append(line)
        captured_total = checked_add(
            captured_total,
            line["captured_amount_minor"],
            "captured_total_minor",
        )
        refunded_total = checked_add(
            refunded_total,
            line["refunded_amount_minor"],
            "refunded_total_minor",
        )
        net_total = checked_add(
            net_total,
            line["net_settlement_amount_minor"],
            "net_settlement_total_minor",
        )

    statement = {
        **identity,
        "statement_id": statement_id,
        "source_roster_digest": sha256_digest(roster),
        "lines": lines,
        "line_count": len(lines),
        "captured_total_minor": captured_total,
        "refunded_total_minor": refunded_total,
        "net_settlement_total_minor": net_total,
    }
    statement["statement_digest"] = sha256_digest(statement)
    return statement


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster", required=True, type=Path)
    parser.add_argument("--provider-base-url", required=True)
    parser.add_argument(
        "--output",
        default="-",
        help=(
            'Output path, or "-" for stdout. Existing-file replay/conflict '
            "semantics are gate 3."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=5.0,
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return args


def main() -> int:
    args = parse_args()
    try:
        roster = load_roster(args.roster)
        statement = produce_statement(
            roster,
            args.provider_base_url,
            args.timeout_seconds,
        )
        rendered = json.dumps(
            statement,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        if args.output == "-":
            sys.stdout.write(rendered)
        else:
            Path(args.output).write_text(
                rendered,
                encoding="utf-8",
            )
        return 0
    except SettlementStatementError as exc:
        print(
            f"settlement statement production failed: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
