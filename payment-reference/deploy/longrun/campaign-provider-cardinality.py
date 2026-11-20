#!/usr/bin/env python3
"""#318 read-only provider effective-side-effect cardinality verifier.

Authority:
  payment-sandbox v1.5.4 GET /admin/payments/{id}/timeline

The immutable provider business-event timeline is the effective-side-effect
ledger. Request attempts remain a separate execution-evidence concern.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any

SCHEMA = "oxide-batch-workloads.payment-reference-provider-side-effect-cardinality"
LEDGER_SCHEMA = "oxide-batch-workloads.payment-reference-provider-operation-ledger"
AUTHORITY = "payment-sandbox-v1.5.4-admin-payment-timeline"
PAYMENT_SANDBOX_SOURCE_REVISION = "3b6d6e1864ea5a6d017fcb005f547cf26c37352a"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
I64_MAX = 9_223_372_036_854_775_807

PAYMENT_KEYS = {
    "id", "amount", "currency", "status",
    "authorized_amount", "captured_amount", "refunded_amount", "version",
}
EVENT_KEYS = {
    "id", "aggregate_id", "type", "occurred_at", "aggregate_version",
    "correlation_id", "causation_id", "runtime_sequence",
}
EVENT_TYPES = {
    "payment.created",
    "payment.authorized",
    "payment.failed",
    "payment.cancelled",
    "payment.captured",
    "payment.refunded",
}
FINAL_EVENT_BY_STATUS = {
    "pending": "payment.created",
    "authorized": "payment.authorized",
    "failed": "payment.failed",
    "cancelled": "payment.cancelled",
    "partially_captured": "payment.captured",
    "captured": "payment.captured",
    "partially_refunded": "payment.refunded",
    "refunded": "payment.refunded",
}


class Block(ValueError):
    pass


def demand(ok: bool, reason: str) -> None:
    if not ok:
        raise Block(reason)


def object_(value: Any, name: str) -> dict[str, Any]:
    demand(type(value) is dict, f"{name}: expected object")
    return value


def nonnegative_i64(value: Any, name: str) -> int:
    demand(type(value) is int and 0 <= value <= I64_MAX, f"{name}: expected nonnegative i64")
    return value


def positive_i64(value: Any, name: str) -> int:
    value = nonnegative_i64(value, name)
    demand(value > 0, f"{name}: expected positive i64")
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_payment_state(
    status: str,
    amount: int,
    authorized: int,
    captured: int,
    refunded: int,
) -> None:
    demand(status in FINAL_EVENT_BY_STATUS, "provider payment status unsupported")
    demand(amount > 0, "provider payment amount must be positive")
    demand(0 <= refunded <= captured <= authorized <= amount, "provider financial amount invariant violation")
    if status in {"pending", "failed"}:
        demand((authorized, captured, refunded) == (0, 0, 0), "provider pending/failed amount invariant violation")
    elif status == "authorized":
        demand((authorized, captured, refunded) == (amount, 0, 0), "provider authorized amount invariant violation")
    elif status == "partially_captured":
        demand(authorized == amount and 0 < captured < authorized and refunded == 0, "provider partial-capture invariant violation")
    elif status == "captured":
        demand(authorized == amount and captured == authorized and refunded == 0, "provider captured invariant violation")
    elif status == "partially_refunded":
        demand(authorized == amount and captured == authorized and 0 < refunded < captured, "provider partial-refund invariant violation")
    elif status == "refunded":
        demand(authorized == amount and captured == authorized and refunded == captured, "provider refunded invariant violation")
    elif status == "cancelled":
        demand(authorized == amount and captured == 0 and refunded == 0, "provider cancelled invariant violation")


def provider_ledger(provider_payment_id: str, timeline: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the pinned provider's immutable timeline without I/O."""
    demand(isinstance(provider_payment_id, str) and SAFE_ID.fullmatch(provider_payment_id) is not None, "unsafe provider payment identity")
    timeline = object_(timeline, "provider timeline")
    demand(set(timeline) == {"payment", "events"}, "provider timeline fields mismatch")
    payment = object_(timeline.get("payment"), "provider timeline payment")
    demand(set(payment) == PAYMENT_KEYS, "provider payment fields mismatch")
    events = timeline.get("events")
    demand(isinstance(events, list), "provider timeline events must be an array")
    demand(payment.get("id") == provider_payment_id, "provider timeline payment identity conflict")

    version = positive_i64(payment.get("version"), "provider payment version")
    demand(len(events) == version, "provider event count must equal aggregate version")

    normalized: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    aggregate_versions: list[int] = []
    runtime_sequences: list[int] = []
    for index, raw in enumerate(events, 1):
        event = object_(raw, f"provider event[{index}]")
        demand(set(event) == EVENT_KEYS, f"provider event[{index}] fields mismatch")
        event_id = event.get("id")
        event_type = event.get("type")
        demand(isinstance(event_id, str) and event_id.strip(), f"provider event[{index}] missing id")
        demand(event_id not in event_ids, f"duplicate provider event id: {event_id}")
        event_ids.add(event_id)
        demand(event.get("aggregate_id") == provider_payment_id, f"provider event[{index}] aggregate identity conflict")
        demand(isinstance(event_type, str) and event_type in EVENT_TYPES, f"provider event[{index}] has unsupported type")

        aggregate_version = positive_i64(event.get("aggregate_version"), f"provider event[{index}] aggregate_version")
        runtime_sequence = positive_i64(event.get("runtime_sequence"), f"provider event[{index}] runtime_sequence")
        aggregate_versions.append(aggregate_version)
        runtime_sequences.append(runtime_sequence)

        expected_event_id = f"{provider_payment_id}:{event_type}:{aggregate_version}"
        demand(event_id == expected_event_id, f"provider event[{index}] identity/version/type mismatch")

        correlation_id = event.get("correlation_id")
        causation_id = event.get("causation_id")
        demand(isinstance(correlation_id, str) and correlation_id.strip(), f"provider event[{index}] correlation_id must be non-blank string")
        demand(isinstance(causation_id, str), f"provider event[{index}] causation_id must be string")
        if index == 1:
            demand(event_type == "payment.created", "provider version 1 must be payment.created")
            demand(causation_id == "", "provider creation event must not have causation")
        else:
            demand(event_type != "payment.created", "provider creation event must occur exactly once")
            demand(causation_id == normalized[-1]["event_id"], f"provider event[{index}] causation chain mismatch")

        occurred_at = event.get("occurred_at")
        demand(isinstance(occurred_at, str) and occurred_at.strip(), f"provider event[{index}] missing occurred_at")
        normalized.append({
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_version": aggregate_version,
            "runtime_sequence": runtime_sequence,
            "occurred_at": occurred_at,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
        })

    demand(aggregate_versions == list(range(1, version + 1)), "provider aggregate versions must be contiguous from 1")
    demand(
        runtime_sequences == sorted(runtime_sequences)
        and len(runtime_sequences) == len(set(runtime_sequences)),
        "provider runtime sequences must be strictly increasing",
    )

    amount = positive_i64(payment.get("amount"), "provider payment amount")
    authorized = nonnegative_i64(payment.get("authorized_amount"), "provider payment authorized_amount")
    captured = nonnegative_i64(payment.get("captured_amount"), "provider payment captured_amount")
    refunded = nonnegative_i64(payment.get("refunded_amount"), "provider payment refunded_amount")
    currency = payment.get("currency")
    status = payment.get("status")
    demand(isinstance(currency, str) and currency.strip(), "provider payment currency missing")
    demand(isinstance(status, str) and status.strip(), "provider payment status missing")
    validate_payment_state(status, amount, authorized, captured, refunded)
    demand(normalized[-1]["event_type"] == FINAL_EVENT_BY_STATUS[status], "provider final event/status mismatch")

    return {
        "schema": LEDGER_SCHEMA,
        "schema_version": 1,
        "authority": AUTHORITY,
        "authority_source_revision": PAYMENT_SANDBOX_SOURCE_REVISION,
        "provider_payment_id": provider_payment_id,
        "provider_version": version,
        "creation_event_count": 1,
        "effective_mutation_count": version - 1,
        "payment_status": status,
        "currency": currency,
        "amount_minor": amount,
        "authorized_amount_minor": authorized,
        "captured_amount_minor": captured,
        "refunded_amount_minor": refunded,
        "timeline_sha256": sha256(timeline),
        "events": normalized,
    }


def verify_cardinality(
    *,
    provider_payment_id: str,
    timeline: dict[str, Any],
    expected_effective_mutations: int,
    expected_status: str,
    expected_authorized: int,
    expected_captured: int,
    expected_refunded: int,
) -> dict[str, Any]:
    demand(type(expected_effective_mutations) is int and expected_effective_mutations >= 0, "expected mutation count must be nonnegative integer")
    demand(isinstance(expected_status, str) and expected_status.strip(), "expected status must be non-blank")
    for name, value in (
        ("expected_authorized", expected_authorized),
        ("expected_captured", expected_captured),
        ("expected_refunded", expected_refunded),
    ):
        nonnegative_i64(value, name)

    ledger = provider_ledger(provider_payment_id, timeline)
    demand(ledger["effective_mutation_count"] == expected_effective_mutations, "provider effective mutation cardinality mismatch")
    demand(ledger["payment_status"] == expected_status, "provider terminal status mismatch")
    demand(ledger["authorized_amount_minor"] == expected_authorized, "provider authorized amount mismatch")
    demand(ledger["captured_amount_minor"] == expected_captured, "provider captured amount mismatch")
    demand(ledger["refunded_amount_minor"] == expected_refunded, "provider refunded amount mismatch")
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "decision": "PROVIDER_EFFECT_CARDINALITY_VERIFIED_NOT_CAMPAIGN_ACCEPTED",
        "authority": AUTHORITY,
        "authority_source_revision": PAYMENT_SANDBOX_SOURCE_REVISION,
        "provider_payment_id": provider_payment_id,
        "provider_exists": True,
        "provider_version": ledger["provider_version"],
        "creation_event_count": ledger["creation_event_count"],
        "effective_mutation_count": ledger["effective_mutation_count"],
        "timeline_sha256": ledger["timeline_sha256"],
        "payment_status": ledger["payment_status"],
        "authorized_amount_minor": ledger["authorized_amount_minor"],
        "captured_amount_minor": ledger["captured_amount_minor"],
        "refunded_amount_minor": ledger["refunded_amount_minor"],
        "request_attempt_cardinality_authority": "SEPARATE_EXECUTION_EVIDENCE_REQUIRED",
        "ledger": ledger,
    }


def verify_absent(provider_payment_id: str, http_status: int) -> dict[str, Any]:
    """Prove zero provider effects when the provider aggregate does not exist."""
    demand(isinstance(provider_payment_id, str) and SAFE_ID.fullmatch(provider_payment_id) is not None, "unsafe provider payment identity")
    demand(type(http_status) is int and http_status == 404, "provider timeline must be HTTP 404 for absent proof")
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "decision": "PROVIDER_EFFECT_CARDINALITY_VERIFIED_NOT_CAMPAIGN_ACCEPTED",
        "authority": AUTHORITY,
        "authority_source_revision": PAYMENT_SANDBOX_SOURCE_REVISION,
        "provider_payment_id": provider_payment_id,
        "provider_exists": False,
        "provider_version": None,
        "creation_event_count": 0,
        "effective_mutation_count": 0,
        "timeline_sha256": None,
        "payment_status": "ABSENT",
        "authorized_amount_minor": 0,
        "captured_amount_minor": 0,
        "refunded_amount_minor": 0,
        "request_attempt_cardinality_authority": "SEPARATE_EXECUTION_EVIDENCE_REQUIRED",
        "ledger": None,
    }


def compose_exec() -> list[str]:
    compose_file = os.environ.get(
        "PAYMENT_REFERENCE_COMPOSE_FILE",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "compose.yaml")),
    )
    demand(os.path.isfile(compose_file), "payment-reference Compose file missing")
    command = ["docker", "compose"]
    compose_env_file = os.environ.get("PAYMENT_REFERENCE_COMPOSE_ENV_FILE", "")
    if compose_env_file:
        demand(os.path.isfile(compose_env_file), "payment-reference Compose env file missing")
        command.extend(["--env-file", compose_env_file])
    command.extend(["-f", compose_file, "exec", "-T", "payment-sandbox"])
    return command


def provider_timeline_status(provider_payment_id: str) -> int:
    demand(isinstance(provider_payment_id, str) and SAFE_ID.fullmatch(provider_payment_id) is not None, "unsafe provider payment identity")
    shell = r"""
: "${PAYMENT_SANDBOX_ADMIN_TOKEN:?PAYMENT_SANDBOX_ADMIN_TOKEN is required}"
exec curl --silent --show-error --max-time 8 \
  --output /dev/null \
  --write-out "%{http_code}" \
  --header "Accept: application/json" \
  --header "Authorization: Bearer ${PAYMENT_SANDBOX_ADMIN_TOKEN}" \
  "http://127.0.0.1:8080/admin/payments/$1/timeline"
"""
    result = subprocess.run(
        [*compose_exec(), "sh", "-eu", "-c", shell, "provider-cardinality-status", provider_payment_id],
        capture_output=True,
        timeout=12,
        check=False,
    )
    demand(result.returncode == 0, "provider timeline status GET failed")
    raw = result.stdout.decode("ascii", errors="strict").strip()
    demand(re.fullmatch(r"[1-5][0-9]{2}", raw) is not None, "provider timeline returned invalid HTTP status")
    return int(raw)


def provider_timeline(provider_payment_id: str) -> dict[str, Any]:
    demand(isinstance(provider_payment_id, str) and SAFE_ID.fullmatch(provider_payment_id) is not None, "unsafe provider payment identity")
    shell = r"""
: "${PAYMENT_SANDBOX_ADMIN_TOKEN:?PAYMENT_SANDBOX_ADMIN_TOKEN is required}"
exec curl --fail --silent --show-error --max-time 8 \
  --header "Accept: application/json" \
  --header "Authorization: Bearer ${PAYMENT_SANDBOX_ADMIN_TOKEN}" \
  "http://127.0.0.1:8080/admin/payments/$1/timeline"
"""
    result = subprocess.run(
        [*compose_exec(), "sh", "-eu", "-c", shell, "provider-cardinality", provider_payment_id],
        capture_output=True,
        timeout=12,
        check=False,
    )
    demand(result.returncode == 0, "provider timeline GET failed")
    demand(len(result.stdout) <= 2_000_000, "provider timeline response oversized")
    return object_(json.loads(result.stdout), "provider timeline GET")


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 read-only provider side-effect cardinality verifier")
    parser.add_argument("--provider-payment-id", required=True)
    parser.add_argument("--expect-absent", action="store_true")
    parser.add_argument("--expected-effective-mutations", type=int)
    parser.add_argument("--expected-status")
    parser.add_argument("--expected-authorized", type=int)
    parser.add_argument("--expected-captured", type=int)
    parser.add_argument("--expected-refunded", type=int)
    args = parser.parse_args()
    try:
        expected = (
            args.expected_effective_mutations,
            args.expected_status,
            args.expected_authorized,
            args.expected_captured,
            args.expected_refunded,
        )
        if args.expect_absent:
            demand(all(value is None for value in expected), "--expect-absent cannot be combined with present-payment expectations")
            result = verify_absent(
                args.provider_payment_id,
                provider_timeline_status(args.provider_payment_id),
            )
        else:
            demand(all(value is not None for value in expected), "present-payment verification requires all expected fields")
            timeline = provider_timeline(args.provider_payment_id)
            result = verify_cardinality(
                provider_payment_id=args.provider_payment_id,
                timeline=timeline,
                expected_effective_mutations=args.expected_effective_mutations,
                expected_status=args.expected_status,
                expected_authorized=args.expected_authorized,
                expected_captured=args.expected_captured,
                expected_refunded=args.expected_refunded,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Block, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"decision": "BLOCK", "reason": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
