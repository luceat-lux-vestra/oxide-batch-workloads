#!/usr/bin/env python3
"""#318 independent per-day provider statement + Merchant close verifier.

This verifier owns settlement proof, not campaign/day acceptance. It consumes
an immutable settlement roster and its independent 100-slot verification,
produces/validates an immutable provider statement, reconciles Merchant close
state from the Merchant database before mutation, proves exact replay and
conflict rejection, and emits a non-accepting immutable verification artifact.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
ROSTER_BUILDER_PATH = HERE / "campaign-settlement-roster.py"
SCHEMA = "oxide-batch-workloads.payment-reference-settlement-verification"
DECISION = "SETTLEMENT_VERIFIED_NOT_DAY_ACCEPTED"


class SettlementVerificationError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SettlementVerificationError(message)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


roster_builder = load_module(
    "payment_reference_campaign_settlement_roster",
    ROSTER_BUILDER_PATH,
)
statement = roster_builder.statement


def load_json_artifact(path: Path, name: str) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), f"{name} must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettlementVerificationError(f"unable to read {name}") from exc
    require(type(value) is dict, f"{name} must contain a JSON object")
    return value


def artifact_digest(value: Any) -> str:
    return statement.sha256_digest(value)


def close_request(provider_statement: dict[str, Any]) -> dict[str, Any]:
    statement.validate_statement(provider_statement)
    return {
        "statement_id": provider_statement["statement_id"],
        "statement_digest": provider_statement["statement_digest"],
        "provider_account_id": provider_statement["provider_account_id"],
        "settlement_date": provider_statement["settlement_date"],
        "currency": provider_statement["currency"],
        "line_count": provider_statement["line_count"],
        "captured_total_minor": provider_statement["captured_total_minor"],
        "refunded_total_minor": provider_statement["refunded_total_minor"],
        "net_settlement_total_minor": provider_statement["net_settlement_total_minor"],
    }


def expected_persisted_row(request: dict[str, Any]) -> dict[str, Any]:
    return {**request, "settlement_state": "CLOSED"}


def validate_close_view(
    body: Any,
    request: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    require(type(body) is dict, "Merchant close response must be a JSON object")
    expected = {
        **request,
        "settlement_state": "CLOSED",
        "replayed": replayed,
    }
    require(body == expected, "Merchant close response does not match canonical statement")
    return body


def validate_snapshot(
    snapshot: Any,
    request: dict[str, Any],
    *,
    allow_absent: bool,
) -> dict[str, Any] | None:
    require(type(snapshot) is dict and set(snapshot) == {"rows", "event_count"}, "Merchant snapshot fields mismatch")
    rows = snapshot["rows"]
    event_count = snapshot["event_count"]
    require(isinstance(rows, list), "Merchant snapshot rows must be an array")
    require(type(event_count) is int and event_count >= 0, "Merchant settlement event count invalid")
    require(len(rows) <= 1, "Merchant settlement identity resolves to multiple rows")
    if not rows:
        require(allow_absent, "Merchant CLOSED settlement row missing")
        require(event_count == 0, "orphan Merchant settlement-close event")
        return None
    row = rows[0]
    require(type(row) is dict, "Merchant settlement row must be an object")
    require(row == expected_persisted_row(request), "persisted Merchant settlement differs from provider statement")
    require(event_count == 1, "Merchant settlement close event cardinality must be exactly one")
    return row


def conflict_digest(current: str) -> str:
    candidate = "sha256:" + ("0" * 64)
    if candidate == current:
        candidate = "sha256:" + ("f" * 64)
    return candidate


def totals_conflict(request: dict[str, Any]) -> dict[str, Any]:
    require(
        request["captured_total_minor"] < statement.I64_MAX
        and request["net_settlement_total_minor"] < statement.I64_MAX,
        "cannot construct bounded settlement-total negative control",
    )
    changed = copy.deepcopy(request)
    changed["captured_total_minor"] += 1
    changed["net_settlement_total_minor"] += 1
    return changed


def validate_conflict(status: int, body: Any) -> None:
    require(status == 409, f"Merchant conflict negative control returned HTTP {status}")
    require(
        type(body) is dict
        and isinstance(body.get("error"), str)
        and "different digest or totals" in body["error"],
        "Merchant conflict negative control returned unexpected body",
    )


def prove_merchant_close(
    provider_statement: dict[str, Any],
    *,
    post: Callable[[dict[str, Any]], tuple[int, dict[str, Any] | None]],
    snapshot: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Prove close/replay/conflict semantics with reconciliation before mutation."""
    request = close_request(provider_statement)

    pre = snapshot(request)
    persisted = validate_snapshot(pre, request, allow_absent=True)
    if persisted is None:
        status, body = post(request)
        require(status == 201, f"initial Merchant settlement close returned HTTP {status}")
        validate_close_view(body, request, replayed=False)

    authoritative = snapshot(request)
    row = validate_snapshot(authoritative, request, allow_absent=False)
    require(row is not None, "Merchant settlement did not become authoritative")

    replay_status, replay_body = post(request)
    require(replay_status == 200, f"exact Merchant settlement replay returned HTTP {replay_status}")
    validate_close_view(replay_body, request, replayed=True)
    replay_snapshot = snapshot(request)
    require(replay_snapshot == authoritative, "exact replay changed Merchant settlement state")
    validate_snapshot(replay_snapshot, request, allow_absent=False)

    bad_digest = copy.deepcopy(request)
    bad_digest["statement_digest"] = conflict_digest(request["statement_digest"])
    digest_status, digest_body = post(bad_digest)
    validate_conflict(digest_status, digest_body)
    after_digest = snapshot(request)
    require(after_digest == authoritative, "digest conflict changed authoritative Merchant settlement")

    bad_totals = totals_conflict(request)
    totals_status, totals_body = post(bad_totals)
    validate_conflict(totals_status, totals_body)
    after_totals = snapshot(request)
    require(after_totals == authoritative, "totals conflict changed authoritative Merchant settlement")

    final_status, final_body = post(request)
    require(final_status == 200, f"final exact Merchant replay returned HTTP {final_status}")
    validate_close_view(final_body, request, replayed=True)
    final_snapshot = snapshot(request)
    require(final_snapshot == authoritative, "negative controls changed final Merchant settlement state")
    validate_snapshot(final_snapshot, request, allow_absent=False)

    return {
        "settlement_state": "CLOSED",
        "persisted_row": expected_persisted_row(request),
        "business_event_count": 1,
        "exact_replay_http_status": 200,
        "digest_conflict_http_status": 409,
        "totals_conflict_http_status": 409,
        "authoritative_state_unchanged_after_conflicts": True,
    }


def build_verification(
    *,
    campaign_id: str,
    logical_day_sequence: int,
    source_revision: str,
    roster: dict[str, Any],
    roster_verification: dict[str, Any],
    provider_statement: dict[str, Any],
    merchant_close_proof: dict[str, Any],
) -> dict[str, Any]:
    statement.validate_roster(roster)
    statement.validate_statement(provider_statement, roster)
    require(roster["source_revision"] == source_revision, "roster source revision differs from campaign")
    require(
        roster_verification["campaign_id"] == campaign_id
        and roster_verification["logical_day_sequence"] == logical_day_sequence
        and roster_verification["source_revision"] == source_revision,
        "roster verification identity differs from campaign",
    )
    request = close_request(provider_statement)
    require(
        merchant_close_proof["persisted_row"] == expected_persisted_row(request)
        and merchant_close_proof["business_event_count"] == 1
        and merchant_close_proof["exact_replay_http_status"] == 200
        and merchant_close_proof["digest_conflict_http_status"] == 409
        and merchant_close_proof["totals_conflict_http_status"] == 409
        and merchant_close_proof["authoritative_state_unchanged_after_conflicts"] is True,
        "Merchant close proof incomplete",
    )
    result = {
        "schema": SCHEMA,
        "schema_version": 1,
        "decision": DECISION,
        "campaign_id": campaign_id,
        "logical_day_sequence": logical_day_sequence,
        "source_revision": source_revision,
        "roster_sha256": statement.sha256_digest(roster),
        "roster_verification_sha256": artifact_digest(roster_verification),
        "statement": {
            "statement_contract_version": provider_statement["statement_contract_version"],
            "statement_id": provider_statement["statement_id"],
            "statement_digest": provider_statement["statement_digest"],
            "statement_artifact_sha256": artifact_digest(provider_statement),
            "source_roster_digest": provider_statement["source_roster_digest"],
            "provider_account_id": provider_statement["provider_account_id"],
            "settlement_date": provider_statement["settlement_date"],
            "currency": provider_statement["currency"],
            "line_count": provider_statement["line_count"],
            "captured_total_minor": provider_statement["captured_total_minor"],
            "refunded_total_minor": provider_statement["refunded_total_minor"],
            "net_settlement_total_minor": provider_statement["net_settlement_total_minor"],
        },
        "merchant_close": merchant_close_proof,
    }
    validate_verification(result, roster, roster_verification, provider_statement)
    return result


def validate_verification(
    value: Any,
    roster: dict[str, Any],
    roster_verification: dict[str, Any],
    provider_statement: dict[str, Any],
) -> dict[str, Any]:
    require(type(value) is dict, "settlement verification must be an object")
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "roster_sha256",
        "roster_verification_sha256", "statement", "merchant_close",
    }
    require(set(value) == expected_fields, "settlement verification fields mismatch")
    require(value["schema"] == SCHEMA and value["schema_version"] == 1, "settlement verification schema mismatch")
    require(value["decision"] == DECISION, "settlement verification decision mismatch")
    require(
        value["campaign_id"] == roster_verification["campaign_id"]
        and value["logical_day_sequence"] == roster_verification["logical_day_sequence"]
        and value["source_revision"] == roster_verification["source_revision"]
        and value["source_revision"] == roster["source_revision"],
        "settlement verification campaign/day/source identity mismatch",
    )
    require(value["roster_sha256"] == statement.sha256_digest(roster), "settlement roster digest mismatch")
    require(
        value["roster_verification_sha256"] == artifact_digest(roster_verification),
        "settlement roster-verification digest mismatch",
    )

    summary = value["statement"]
    require(type(summary) is dict, "statement summary must be an object")
    expected_summary = {
        "statement_contract_version": provider_statement["statement_contract_version"],
        "statement_id": provider_statement["statement_id"],
        "statement_digest": provider_statement["statement_digest"],
        "statement_artifact_sha256": artifact_digest(provider_statement),
        "source_roster_digest": provider_statement["source_roster_digest"],
        "provider_account_id": provider_statement["provider_account_id"],
        "settlement_date": provider_statement["settlement_date"],
        "currency": provider_statement["currency"],
        "line_count": provider_statement["line_count"],
        "captured_total_minor": provider_statement["captured_total_minor"],
        "refunded_total_minor": provider_statement["refunded_total_minor"],
        "net_settlement_total_minor": provider_statement["net_settlement_total_minor"],
    }
    require(summary == expected_summary, "statement summary does not match immutable provider statement")
    require(
        provider_statement["source_roster_digest"] == value["roster_sha256"],
        "provider statement does not bind the verified roster",
    )

    proof = value["merchant_close"]
    request = close_request(provider_statement)
    expected_proof = {
        "settlement_state": "CLOSED",
        "persisted_row": expected_persisted_row(request),
        "business_event_count": 1,
        "exact_replay_http_status": 200,
        "digest_conflict_http_status": 409,
        "totals_conflict_http_status": 409,
        "authoritative_state_unchanged_after_conflicts": True,
    }
    require(proof == expected_proof, "Merchant close proof mismatch")
    return value


def loopback_base(url: str, expected_port: int, role: str) -> str:
    return roster_builder.loopback_base(url, expected_port, role)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SettlementVerificationError(f"Merchant redirect refused: HTTP {code}")


def merchant_post_json(
    merchant_base: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any] | None]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        merchant_base + "/settlements/close",
        data=statement.canonical_bytes(payload),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=8) as response:
            data = response.read(1_000_001)
            require(len(data) <= 1_000_000, "Merchant close response oversized")
            body = json.loads(data) if data else None
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        data = exc.read(1_000_001)
        require(len(data) <= 1_000_000, "Merchant conflict response oversized")
        try:
            body = json.loads(data) if data else None
        except json.JSONDecodeError as parse_error:
            raise SettlementVerificationError("Merchant error response is not JSON") from parse_error
        return int(exc.code), body


def compose_exec_prefix() -> list[str]:
    compose_file = os.environ.get(
        "PAYMENT_REFERENCE_COMPOSE_FILE",
        os.path.abspath(os.path.join(HERE, "..", "compose.yaml")),
    )
    require(os.path.isfile(compose_file), "payment-reference Compose file missing")
    command = ["docker", "compose"]
    compose_env_file = os.environ.get("PAYMENT_REFERENCE_COMPOSE_ENV_FILE", "")
    if compose_env_file:
        require(os.path.isfile(compose_env_file), "payment-reference Compose env file missing")
        command.extend(["--env-file", compose_env_file])
    command.extend(["-f", compose_file, "exec", "-T", "merchant-db"])
    return command


def sql_literal(value: str) -> str:
    require(isinstance(value, str) and "\x00" not in value, "unsafe SQL literal value")
    return "'" + value.replace("'", "''") + "'"


def merchant_db_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    sid = sql_literal(request["statement_id"])
    account = sql_literal(request["provider_account_id"])
    date = sql_literal(request["settlement_date"])
    currency = sql_literal(request["currency"])
    query = f"""
WITH matched AS (
  SELECT
    statement_id,
    statement_digest,
    provider_account_id,
    settlement_date::text AS settlement_date,
    currency,
    line_count,
    captured_total_minor,
    refunded_total_minor,
    net_settlement_total_minor,
    settlement_state
  FROM payment_reference_merchant.settlement_close
  WHERE statement_id = {sid}
     OR (
       provider_account_id = {account}
       AND settlement_date = {date}::date
       AND currency = {currency}
     )
  ORDER BY statement_id
),
row_set AS (
  SELECT COALESCE(
    json_agg(json_build_object(
      'statement_id', statement_id,
      'statement_digest', statement_digest,
      'provider_account_id', provider_account_id,
      'settlement_date', settlement_date,
      'currency', currency,
      'line_count', line_count,
      'captured_total_minor', captured_total_minor,
      'refunded_total_minor', refunded_total_minor,
      'net_settlement_total_minor', net_settlement_total_minor,
      'settlement_state', settlement_state
    )),
    '[]'::json
  ) AS rows
  FROM matched
),
event_set AS (
  SELECT COUNT(*)::BIGINT AS event_count
  FROM payment_reference_merchant.business_event
  WHERE aggregate_type = 'SETTLEMENT'
    AND aggregate_id = {sid}
    AND event_type = 'SETTLEMENT_CLOSED'
)
SELECT json_build_object(
  'rows', row_set.rows,
  'event_count', event_set.event_count
)::text
FROM row_set CROSS JOIN event_set;
"""
    result = subprocess.run(
        [
            *compose_exec_prefix(),
            "psql", "-X", "-v", "ON_ERROR_STOP=1",
            "-U", "merchant", "-d", "merchant",
            "-Atq", "-c", query,
        ],
        capture_output=True,
        timeout=12,
        check=False,
    )
    require(result.returncode == 0, "Merchant settlement DB snapshot failed")
    require(len(result.stdout) <= 2_000_000, "Merchant settlement DB snapshot oversized")
    lines = [line for line in result.stdout.decode("utf-8").splitlines() if line.strip()]
    require(len(lines) == 1, "Merchant settlement DB snapshot returned unexpected row count")
    try:
        snapshot = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise SettlementVerificationError("Merchant settlement DB snapshot is not JSON") from exc
    require(type(snapshot) is dict, "Merchant settlement DB snapshot must be an object")
    return snapshot


def require_distinct_paths(*paths: Path) -> None:
    normalized = [os.path.abspath(path) for path in paths]
    require(len(normalized) == len(set(normalized)), "settlement artifact paths must be distinct")


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 independent provider statement + Merchant close verification")
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--roster-verification", type=Path, required=True)
    parser.add_argument("--statement-output", type=Path, required=True)
    parser.add_argument("--verification-output", type=Path, required=True)
    parser.add_argument("--provider-url", default=None)
    parser.add_argument("--merchant-url", default=None)
    args = parser.parse_args()

    try:
        require(type(args.day) is int and args.day > 0, "day must be positive")
        require_distinct_paths(
            args.roster,
            args.roster_verification,
            args.statement_output,
            args.verification_output,
        )
        campaign_id, source_revision = roster_builder.load_campaign(args.campaign_dir)
        plan = roster_builder.traffic.build(campaign_id, args.day)
        roster = statement.load_roster(args.roster)
        roster_verification = load_json_artifact(args.roster_verification, "roster verification")
        roster_builder.validate_verification(roster_verification, roster, plan)
        require(roster["source_revision"] == source_revision, "roster source revision differs from campaign")

        provider_port = int(os.environ.get("PAYMENT_SANDBOX_DIRECT_PORT", "18084"))
        merchant_port = int(os.environ.get("MERCHANT_PORT", "18083"))
        provider_base = loopback_base(
            args.provider_url or f"http://127.0.0.1:{provider_port}",
            provider_port,
            "provider",
        )
        merchant_base = loopback_base(
            args.merchant_url or f"http://127.0.0.1:{merchant_port}",
            merchant_port,
            "Merchant",
        )

        provider_statement = statement.produce_statement(roster, provider_base, 5.0)
        statement.validate_statement(provider_statement, roster)
        statement_retention = statement.retain_immutable_statement(
            args.statement_output,
            provider_statement,
            roster,
        )

        proof = prove_merchant_close(
            provider_statement,
            post=lambda payload: merchant_post_json(merchant_base, payload),
            snapshot=merchant_db_snapshot,
        )
        verification = build_verification(
            campaign_id=campaign_id,
            logical_day_sequence=args.day,
            source_revision=source_revision,
            roster=roster,
            roster_verification=roster_verification,
            provider_statement=provider_statement,
            merchant_close_proof=proof,
        )
        verification_retention = roster_builder.retain_immutable(
            args.verification_output,
            verification,
        )
        print(json.dumps({
            "decision": DECISION,
            "statement_id": provider_statement["statement_id"],
            "statement_digest": provider_statement["statement_digest"],
            "statement_retention": statement_retention,
            "verification_retention": verification_retention,
            "verification_sha256": artifact_digest(verification),
        }, sort_keys=True))
        return 0
    except (
        SettlementVerificationError,
        roster_builder.RosterError,
        statement.SettlementStatementError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
