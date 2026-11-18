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
      Optional retained statement path. "-" (default) writes canonical JSON to
      stdout. A file path is immutable: first creation is published atomically,
      exact replay succeeds without rewriting, and any conflicting content
      fails closed.

Digest/identity contract:
  * statement_id = "statement-" + sha256(canonical JSON statement identity)
  * line_id = "line-" + sha256(canonical JSON {statement_id, provider_payment_id})
  * source_roster_digest = "sha256:" + sha256(canonical JSON roster)
  * statement_digest = "sha256:" + sha256(canonical JSON statement with the
    statement_digest field omitted)

A file output is an immutable retained statement. The producer never replaces
an existing retained file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
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
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STATEMENT_ID_RE = re.compile(r"^statement-[0-9a-f]{64}$")
LINE_ID_RE = re.compile(r"^line-[0-9a-f]{64}$")


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



def statement_identity(statement: dict[str, Any]) -> dict[str, Any]:
    return {
        "statement_contract_version": statement["statement_contract_version"],
        "provider_account_id": statement["provider_account_id"],
        "settlement_date": statement["settlement_date"],
        "currency": statement["currency"],
    }


def validate_statement(
    statement: Any,
    expected_roster: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(statement, dict):
        raise SettlementStatementError("statement must be a JSON object")

    expected_fields = {
        "statement_contract_version",
        "statement_id",
        "statement_digest",
        "source_roster_digest",
        "provider_account_id",
        "settlement_date",
        "currency",
        "lines",
        "line_count",
        "captured_total_minor",
        "refunded_total_minor",
        "net_settlement_total_minor",
    }
    if set(statement) != expected_fields:
        raise SettlementStatementError(
            "statement fields must exactly match provider-settlement-statement-v1"
        )
    if statement["statement_contract_version"] != STATEMENT_VERSION:
        raise SettlementStatementError(
            f"statement_contract_version must be {STATEMENT_VERSION}"
        )
    if (
        not isinstance(statement["provider_account_id"], str)
        or not statement["provider_account_id"].strip()
    ):
        raise SettlementStatementError("provider_account_id must not be blank")
    try:
        settlement_date = dt.date.fromisoformat(statement["settlement_date"])
    except (TypeError, ValueError) as exc:
        raise SettlementStatementError(
            "settlement_date must be an ISO date"
        ) from exc
    if settlement_date.isoformat() != statement["settlement_date"]:
        raise SettlementStatementError("settlement_date must use YYYY-MM-DD")
    if (
        not isinstance(statement["currency"], str)
        or not CURRENCY_RE.fullmatch(statement["currency"])
    ):
        raise SettlementStatementError(
            "currency must be an uppercase three-letter code"
        )

    if (
        not isinstance(statement["statement_id"], str)
        or not STATEMENT_ID_RE.fullmatch(statement["statement_id"])
    ):
        raise SettlementStatementError("statement_id has invalid format")
    expected_statement_id = deterministic_id(
        "statement",
        statement_identity(statement),
    )
    if statement["statement_id"] != expected_statement_id:
        raise SettlementStatementError(
            "statement_id does not match canonical statement identity"
        )

    if (
        not isinstance(statement["source_roster_digest"], str)
        or not DIGEST_RE.fullmatch(statement["source_roster_digest"])
    ):
        raise SettlementStatementError(
            "source_roster_digest has invalid format"
        )
    if (
        expected_roster is not None
        and statement["source_roster_digest"] != sha256_digest(expected_roster)
    ):
        raise SettlementStatementError(
            "source_roster_digest does not match input roster"
        )

    lines = statement["lines"]
    if not isinstance(lines, list) or not lines:
        raise SettlementStatementError("lines must be a non-empty array")

    observed_payment_ids = []
    captured_total = 0
    refunded_total = 0
    net_total = 0
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            raise SettlementStatementError(
                f"lines[{index}] must be a JSON object"
            )
        expected_line_fields = {
            "line_id",
            "provider_payment_id",
            "provider_version",
            "currency",
            "captured_amount_minor",
            "refunded_amount_minor",
            "net_settlement_amount_minor",
        }
        if set(line) != expected_line_fields:
            raise SettlementStatementError(
                f"lines[{index}] fields do not match settlement line v1"
            )
        payment_id = line["provider_payment_id"]
        if not isinstance(payment_id, str) or not payment_id.strip():
            raise SettlementStatementError(
                f"lines[{index}].provider_payment_id must not be blank"
            )
        observed_payment_ids.append(payment_id)
        if line["currency"] != statement["currency"]:
            raise SettlementStatementError(
                f"lines[{index}].currency does not match statement currency"
            )

        provider_version = checked_nonnegative_i64(
            line["provider_version"],
            f"lines[{index}].provider_version",
        )
        captured = checked_nonnegative_i64(
            line["captured_amount_minor"],
            f"lines[{index}].captured_amount_minor",
        )
        refunded = checked_nonnegative_i64(
            line["refunded_amount_minor"],
            f"lines[{index}].refunded_amount_minor",
        )
        if refunded > captured:
            raise SettlementStatementError(
                f"lines[{index}].refunded_amount_minor exceeds captured amount"
            )
        net = checked_nonnegative_i64(
            line["net_settlement_amount_minor"],
            f"lines[{index}].net_settlement_amount_minor",
        )
        if net != captured - refunded:
            raise SettlementStatementError(
                f"lines[{index}].net_settlement_amount_minor is inconsistent"
            )
        if (
            not isinstance(line["line_id"], str)
            or not LINE_ID_RE.fullmatch(line["line_id"])
        ):
            raise SettlementStatementError(
                f"lines[{index}].line_id has invalid format"
            )
        expected_line_id = deterministic_id(
            "line",
            {
                "statement_id": statement["statement_id"],
                "provider_payment_id": payment_id,
            },
        )
        if line["line_id"] != expected_line_id:
            raise SettlementStatementError(
                f"lines[{index}].line_id does not match canonical identity"
            )

        # Force integer validation above to be retained even when a future
        # refactor no longer uses provider_version in arithmetic.
        _ = provider_version
        captured_total = checked_add(
            captured_total,
            captured,
            "captured_total_minor",
        )
        refunded_total = checked_add(
            refunded_total,
            refunded,
            "refunded_total_minor",
        )
        net_total = checked_add(
            net_total,
            net,
            "net_settlement_total_minor",
        )

    if observed_payment_ids != sorted(observed_payment_ids):
        raise SettlementStatementError(
            "statement lines must be sorted by provider_payment_id"
        )
    if len(observed_payment_ids) != len(set(observed_payment_ids)):
        raise SettlementStatementError(
            "statement lines must contain unique provider_payment_ids"
        )

    line_count = checked_nonnegative_i64(
        statement["line_count"],
        "line_count",
    )
    if line_count == 0 or line_count != len(lines):
        raise SettlementStatementError("line_count does not match lines")
    if checked_nonnegative_i64(
        statement["captured_total_minor"],
        "captured_total_minor",
    ) != captured_total:
        raise SettlementStatementError(
            "captured_total_minor does not match lines"
        )
    if checked_nonnegative_i64(
        statement["refunded_total_minor"],
        "refunded_total_minor",
    ) != refunded_total:
        raise SettlementStatementError(
            "refunded_total_minor does not match lines"
        )
    if checked_nonnegative_i64(
        statement["net_settlement_total_minor"],
        "net_settlement_total_minor",
    ) != net_total:
        raise SettlementStatementError(
            "net_settlement_total_minor does not match lines"
        )

    if (
        not isinstance(statement["statement_digest"], str)
        or not DIGEST_RE.fullmatch(statement["statement_digest"])
    ):
        raise SettlementStatementError("statement_digest has invalid format")
    unsigned = dict(statement)
    del unsigned["statement_digest"]
    expected_digest = sha256_digest(unsigned)
    if statement["statement_digest"] != expected_digest:
        raise SettlementStatementError(
            "statement_digest does not match canonical statement content"
        )
    return statement


def render_statement(statement: dict[str, Any]) -> str:
    return json.dumps(
        statement,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def load_retained_statement(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise SettlementStatementError(
            f"retained statement path must not be a symlink: {path}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettlementStatementError(
            f"unable to read retained statement {path}: {exc}"
        ) from exc
    return validate_statement(value)


def fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def retain_immutable_statement(
    path: Path,
    statement: dict[str, Any],
    roster: dict[str, Any],
) -> str:
    validate_statement(statement, roster)
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SettlementStatementError(
            f"unable to create retained statement directory {parent}: {exc}"
        ) from exc

    rendered = render_statement(statement).encode("utf-8")
    temporary_fd = -1
    temporary_name = None
    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        os.fchmod(temporary_fd, 0o644)
        with os.fdopen(temporary_fd, "wb") as temporary:
            temporary_fd = -1
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())

        try:
            os.link(temporary_name, path)
        except FileExistsError:
            existing = load_retained_statement(path)
            if statement_identity(existing) != statement_identity(statement):
                raise SettlementStatementError(
                    "retained statement path already contains a different "
                    "statement identity"
                )
            if existing != statement:
                raise SettlementStatementError(
                    "settlement statement conflict: same identity has "
                    "different immutable content"
                )
            return "replay"
        except OSError as exc:
            raise SettlementStatementError(
                f"unable to publish retained statement {path}: {exc}"
            ) from exc

        fsync_directory(parent)
        return "created"
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster", required=True, type=Path)
    parser.add_argument("--provider-base-url", required=True)
    parser.add_argument(
        "--output",
        default="-",
        help=(
            'Output path, or "-" for stdout. File output is immutable: '
            "first publish is atomic, exact replay is accepted without a "
            "rewrite, and any conflict fails closed."
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
        validate_statement(statement, roster)
        rendered = render_statement(statement)
        if args.output == "-":
            sys.stdout.write(rendered)
        else:
            outcome = retain_immutable_statement(
                Path(args.output),
                statement,
                roster,
            )
            print(
                f"retained settlement statement {outcome}: {args.output}",
                file=sys.stderr,
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
