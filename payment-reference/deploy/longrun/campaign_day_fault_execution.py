#!/usr/bin/env python3
"""#318 day-wide deterministic fault-overlay executor.

This owns only the online fault lane.  It requires the normal lane to be
immutably complete first, arms the canonical fault plan before the Merchant
mutation, binds provider identity from the real provider request, and retains a
write-ahead journal.  Recovery, reconciliation, hard-crash continuation,
settlement, day acceptance and frontier advancement remain separate authorities.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

import campaign_fault_catalog as fault_catalog
import campaign_hard_crash as hard_crash
import campaign_day_online_traffic as normal_day

HERE = Path(__file__).resolve().parent

SCHEMA = "oxide-batch-workloads.payment-reference-day-online-fault-execution"
SCHEMA_VERSION = 1
SLOT_SCHEMA = "oxide-batch-workloads.payment-reference-fault-slot-execution"
CONTROL_INTENT_SCHEMA = "oxide-batch-workloads.payment-reference-fault-control-intent"
CONTROL_RESPONSE_SCHEMA = "oxide-batch-workloads.payment-reference-fault-control-response"
MUTATION_INTENT_SCHEMA = "oxide-batch-workloads.payment-reference-fault-mutation-intent"
MUTATION_RESPONSE_SCHEMA = "oxide-batch-workloads.payment-reference-fault-mutation-response"
PROVIDER_CONTROL_SCHEMA = "oxide-batch-workloads.payment-reference-provider-failure-control"
DECISION = "FAULT_TRAFFIC_COMPLETE_RECOVERY_AND_CRASH_PHASE_PENDING"
pilot = normal_day.pilot


def require(condition: bool, message: str) -> None:
    if not condition:
        raise pilot.Block(message)


def fault_slots(plan: dict[str, Any]) -> list[dict[str, Any]]:
    _, faults = normal_day.partition(plan)
    expected = plan.get("fault_counts")
    require(isinstance(expected, dict), "fault count contract missing")
    require(set(expected) == set(fault_catalog.classes()), "fault count class set differs from catalog")
    observed = {
        fault_class: sum(row["fault_class"] == fault_class for row in faults)
        for fault_class in fault_catalog.classes()
    }
    require(observed == expected, "fault slot distribution differs from day plan")
    require(sum(observed.values()) == len(faults), "fault count total mismatch")
    return faults


def logical_business_time(plan: dict[str, Any]) -> str:
    value = plan.get("logical_business_date")
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None,
            "logical business date invalid")
    return value + "T00:00:00Z"


def validate_proxy_base(value: str) -> str:
    require(isinstance(value, str) and value, "fault proxy base URL missing")
    parsed = urllib.parse.urlsplit(value)
    require(
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment,
        "fault proxy must be an explicit 127.0.0.1 HTTP endpoint with port",
    )
    return f"http://127.0.0.1:{parsed.port}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        raise pilot.Block("redirect is not a qualified fault-control outcome")


def proxy_http(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    base = validate_proxy_base(base)
    require(method in {"GET", "POST"}, "fault-control method unsupported")
    require(path.startswith("/__fault/") and "?" not in path and ".." not in path,
            "unsafe fault-control path")
    payload = None if body is None else pilot.stable(body)
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=payload, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(2_000_001)
        require(len(raw) <= 2_000_000, "fault-control response exceeds size limit")
        return int(response.status), raw


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    value = json.loads(raw)
    require(type(value) is dict, f"{label} must be a JSON object")
    return value


def _control_files(folder: Path, ordinal: int) -> tuple[Path, Path]:
    return (
        folder / f"control-{ordinal:02d}-intent.json",
        folder / f"control-{ordinal:02d}-response.json",
    )


def record_control(
    *,
    folder: Path,
    ordinal: int,
    base: str,
    path: str,
    body: dict[str, Any],
    expected_status: int,
    timeout_seconds: float,
    call: Callable[[str, str, str, dict[str, Any] | None, float], tuple[int, bytes]],
) -> dict[str, Any]:
    intent_path, response_path = _control_files(folder, ordinal)
    require(not intent_path.exists() and not response_path.exists(), "previous fault-control attempt detected")
    pilot.exclusive_write(intent_path, {
        "schema": CONTROL_INTENT_SCHEMA,
        "ordinal": ordinal,
        "method": "POST",
        "path": path,
        "body_sha256": pilot.sha(pilot.stable(body)),
        "expected_status": expected_status,
    })
    try:
        status, raw = call(base, "POST", path, body, timeout_seconds)
    except Exception as exc:
        raise pilot.Block("fault-control transport outcome unknown; retained intent blocks replay") from exc
    parsed = _decode_object(raw, "fault-control response")
    pilot.exclusive_write(response_path, {
        "schema": CONTROL_RESPONSE_SCHEMA,
        "ordinal": ordinal,
        "status": status,
        "body_sha256": pilot.sha(raw),
        "json_body": parsed,
    })
    require(status == expected_status, f"fault-control HTTP {status}, expected {expected_status}")
    return parsed


def read_proxy_stats(
    *,
    base: str,
    timeout_seconds: float,
    call: Callable[[str, str, str, dict[str, Any] | None, float], tuple[int, bytes]],
) -> dict[str, Any]:
    status, raw = call(base, "GET", "/__fault/stats", None, timeout_seconds)
    require(status == 200, "fault proxy stats read failed")
    return _decode_object(raw, "fault proxy stats")


def record_fault_mutation(
    *,
    folder: Path,
    ordinal: int,
    request: dict[str, Any],
    post: Callable[[str, str, dict[str, Any]], tuple[int, bytes]],
) -> tuple[int, dict[str, Any] | None]:
    intent_path, response_path = pilot.step_files(folder, ordinal)
    require(not intent_path.exists() and not response_path.exists(), "previous fault mutation attempt detected")
    require(request.get("method") == "POST" and type(request.get("body")) is dict, "fault mutation request invalid")
    pilot.exclusive_write(intent_path, {
        "schema": MUTATION_INTENT_SCHEMA,
        "ordinal": ordinal,
        "method": "POST",
        "path": request["path"],
        "body_sha256": pilot.sha(pilot.stable(request["body"])),
        "response_semantics_authority": "RECOVERY_RECONCILIATION_AND_PROVIDER_TRUTH",
    })
    try:
        status, raw = post(pilot.BASE, request["path"], request["body"])
    except Exception as exc:
        raise pilot.Block("fault mutation transport outcome unknown; retained intent blocks replay") from exc
    parsed: dict[str, Any] | None
    try:
        value = json.loads(raw) if raw else None
        parsed = value if type(value) is dict else None
    except ValueError:
        parsed = None
    pilot.exclusive_write(response_path, {
        "schema": MUTATION_RESPONSE_SCHEMA,
        "ordinal": ordinal,
        "status": status,
        "body_sha256": pilot.sha(raw),
        "json_body": parsed,
    })
    require(200 <= status < 600, "fault mutation returned invalid HTTP status")
    return status, parsed


def _compose_exec() -> list[str]:
    compose_file = os.environ.get(
        "PAYMENT_REFERENCE_COMPOSE_FILE",
        str(HERE.parent / "compose.yaml"),
    )
    require(Path(compose_file).is_file(), "payment-reference Compose file missing")
    command = ["docker", "compose"]
    env_file = os.environ.get("PAYMENT_REFERENCE_COMPOSE_ENV_FILE")
    if env_file:
        require(Path(env_file).is_file(), "payment-reference Compose env file missing")
        command.extend(["--env-file", env_file])
    command.extend(["-f", compose_file, "exec", "-T", "payment-sandbox"])
    return command


def fail_provider_payment(
    provider_payment_id: str,
    correlation_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    require(fault_catalog.PAYMENT_ID.fullmatch(provider_payment_id) is not None,
            "provider failure control received unsafe payment id")
    require(isinstance(correlation_id, str) and correlation_id, "provider failure correlation missing")
    result = subprocess.run(
        [
            *_compose_exec(),
            "payment-sandbox-campaign-control",
            "fail",
            "--payment-id", provider_payment_id,
            "--correlation-id", correlation_id,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    require(result.returncode == 0, "provider failure control command failed")
    value = json.loads(result.stdout)
    require(
        type(value) is dict
        and value.get("payment_id") == provider_payment_id
        and value.get("status") == "failed"
        and type(value.get("version")) is int
        and value["version"] > 1,
        "provider failure control result invalid",
    )
    return value


def record_provider_failure(
    *,
    folder: Path,
    provider_payment_id: str,
    correlation_id: str,
    timeout_seconds: float,
    fail: Callable[[str, str, float], dict[str, Any]],
) -> dict[str, Any]:
    intent = folder / "provider-failure-control.intent.json"
    response = folder / "provider-failure-control.response.json"
    require(not intent.exists() and not response.exists(), "provider failure control already attempted")
    pilot.exclusive_write(intent, {
        "schema": PROVIDER_CONTROL_SCHEMA,
        "phase": "intent",
        "provider_payment_id": provider_payment_id,
        "correlation_id": correlation_id,
    })
    try:
        value = fail(provider_payment_id, correlation_id, timeout_seconds)
    except Exception as exc:
        raise pilot.Block("provider failure control outcome unknown; manual provider reconciliation required") from exc
    pilot.exclusive_write(response, {
        "schema": PROVIDER_CONTROL_SCHEMA,
        "phase": "response",
        "provider_payment_id": provider_payment_id,
        "result": value,
    })
    return value


def _normal_summary_required(
    *,
    plan: dict[str, Any],
    day: Path,
    source_sha: str,
    provenance_hashes: dict[str, str],
) -> None:
    normal, faults = normal_day.partition(plan)
    expected = normal_day._summary(
        plan=plan,
        source_sha=source_sha,
        provenance_hashes=provenance_hashes,
        normal=normal,
        faults=faults,
    )
    require(
        pilot.read_object(day / "normal-traffic.summary.json") == expected,
        "normal traffic summary missing or differs from exact day/source/provenance",
    )


def _runtime_plan(plan: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    value = fault_catalog.render_runtime_plan(
        item["fault_class"],
        logical_business_time(plan),
    )
    request_steps = [
        step for step in value["steps"]
        if step["point"] in {"REQUEST_BEFORE_PROVIDER_SIDE_EFFECT", "REQUEST_AFTER_PROVIDER_COMMIT"}
    ]
    require(all(step["offset_ms"] == 0 for step in request_steps),
            "online fault request steps must be armed at logical offset zero")
    return value


def _webhook_event_ids(runtime_plan: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for step in runtime_plan["steps"]:
        if step["point"] != "WEBHOOK_DELIVERY":
            continue
        event_id = step["action"]["event_id"]
        if event_id not in result:
            result.append(event_id)
    return result


def _provider_binding(
    *,
    runtime_plan: dict[str, Any],
    stats: dict[str, Any],
    merchant_view: dict[str, Any] | None,
) -> tuple[str, str]:
    plan_snapshot = stats.get("plan")
    require(type(plan_snapshot) is dict, "fault proxy plan snapshot missing")
    bound = plan_snapshot.get("provider_payment_id")
    requires_binding = fault_catalog.PLACEHOLDER in json.dumps(runtime_plan, sort_keys=True)
    if requires_binding:
        require(
            isinstance(bound, str)
            and fault_catalog.PAYMENT_ID.fullmatch(bound) is not None,
            "fault proxy did not bind a safe provider payment identity",
        )
        if merchant_view is not None and merchant_view.get("providerPaymentId") is not None:
            require(merchant_view["providerPaymentId"] == bound, "Merchant/proxy provider identity conflict")
        return bound, "fault-proxy-runtime-binding"

    require(type(merchant_view) is dict, "provider identity requires Merchant payment view")
    provider_id = merchant_view.get("providerPaymentId")
    require(
        isinstance(provider_id, str)
        and fault_catalog.PAYMENT_ID.fullmatch(provider_id) is not None,
        "Merchant payment view did not expose a safe provider identity",
    )
    return provider_id, "merchant-payment-view"


def _digest_files(folder: Path, exclude: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(folder.iterdir(), key=lambda p: p.name):
        if path.name in exclude:
            continue
        require(path.is_file() and not path.is_symlink(), "fault slot contains unsafe/non-file evidence")
        result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def validate_completed_fault_slot(
    *,
    plan: dict[str, Any],
    item: dict[str, Any],
    evidence_root: Path,
    source_sha: str,
    provenance_hashes: dict[str, str],
) -> dict[str, Any]:
    day = pilot.day_path(evidence_root.resolve(), plan)
    require(
        pilot.read_object(day / "identity.json") == pilot.day_identity(plan, source_sha, provenance_hashes),
        "fault slot day identity drift",
    )
    _normal_summary_required(
        plan=plan, day=day, source_sha=source_sha, provenance_hashes=provenance_hashes,
    )
    folder = day / f"slot-{item['slot']:03}"
    require(folder.is_dir() and not folder.is_symlink(), "fault slot directory missing/unsafe")
    result_path = folder / "result.json"
    result = pilot.read_object(result_path)
    require(
        result.get("schema") == SLOT_SCHEMA
        and result.get("schema_version") == 1
        and result.get("campaign_id") == plan["campaign_id"]
        and result.get("logical_day_sequence") == plan["logical_day_sequence"]
        and result.get("slot") == item["slot"]
        and result.get("fault_class") == item["fault_class"]
        and result.get("source_revision") == source_sha
        and result.get("day_plan_sha256") == pilot.sha(pilot.stable(plan))
        and result.get("fault_catalog_sha256") == plan["fault_catalog_sha256"]
        and result.get("provenance_sha256") == provenance_hashes
        and result.get("day_completion_authorized") is False,
        "fault slot result identity/authority mismatch",
    )
    expected_plan = _runtime_plan(plan, item)
    require(pilot.read_object(folder / "fault-plan.json") == expected_plan, "retained runtime fault plan drift")
    hashes = _digest_files(folder, {"result.json"})
    require(result.get("evidence_sha256") == hashes, "fault slot retained evidence digest mismatch")
    require(set(folder.iterdir()) == {folder / "result.json", *[folder / name for name in hashes]},
            "fault slot unexpected evidence files")
    return result


def execute_fault_slot(
    *,
    plan: dict[str, Any],
    item: dict[str, Any],
    evidence_root: Path,
    source_sha: str,
    provenance_hashes: dict[str, str],
    fault_proxy_base: str,
    fault_control_timeout_seconds: float,
    provider_control_timeout_seconds: float,
    post: Callable[[str, str, dict[str, Any]], tuple[int, bytes]] = pilot.http_post,
    get: Callable[[str, str], dict[str, Any]] = pilot.http_get,
    control_call: Callable[[str, str, str, dict[str, Any] | None, float], tuple[int, bytes]] = proxy_http,
    fail_provider: Callable[[str, str, float], dict[str, Any]] = fail_provider_payment,
) -> dict[str, Any]:
    require(item.get("kind") == "fault_overlay", "fault executor received non-fault slot")
    require(item.get("fault_class") in fault_catalog.classes(), "fault slot class not in canonical catalog")
    root = evidence_root.resolve()
    day = pilot.day_path(root, plan)
    require(day.is_dir() and not day.is_symlink(), "normal day evidence must exist before fault execution")
    require(pilot.read_object(day / "identity.json") == pilot.day_identity(plan, source_sha, provenance_hashes),
            "fault day source/provenance/plan drift")
    _normal_summary_required(plan=plan, day=day, source_sha=source_sha, provenance_hashes=provenance_hashes)
    require(pilot.validate_setup(day / "setup", plan["merchant_setup"]), "day setup not validated")

    folder = day / f"slot-{item['slot']:03}"
    folder.mkdir(mode=0o700, exist_ok=False)
    pilot.fsync_dir(day)

    runtime_plan = _runtime_plan(plan, item)
    pilot.exclusive_write(folder / "fault-plan.json", runtime_plan)

    control_ordinal = 1
    record_control(
        folder=folder, ordinal=control_ordinal, base=fault_proxy_base,
        path="/__fault/reset", body={}, expected_status=200,
        timeout_seconds=fault_control_timeout_seconds, call=control_call,
    )
    control_ordinal += 1
    record_control(
        folder=folder, ordinal=control_ordinal, base=fault_proxy_base,
        path="/__fault/load-plan", body=runtime_plan, expected_status=200,
        timeout_seconds=fault_control_timeout_seconds, call=control_call,
    )
    control_ordinal += 1
    record_control(
        folder=folder, ordinal=control_ordinal, base=fault_proxy_base,
        path="/__fault/advance", body={"offset_ms": 0}, expected_status=200,
        timeout_seconds=fault_control_timeout_seconds, call=control_call,
    )
    control_ordinal += 1

    for event_id in _webhook_event_ids(runtime_plan):
        record_control(
            folder=folder, ordinal=control_ordinal, base=fault_proxy_base,
            path="/__fault/webhook-events",
            body={"event_id": event_id, "payload": {"event_id": event_id}},
            expected_status=201,
            timeout_seconds=fault_control_timeout_seconds, call=control_call,
        )
        control_ordinal += 1

    pilot.record_post(folder, 1, item["create_order"], 201, None, post)
    prepared = pilot.record_post(folder, 2, item["prepare_payment"], 201, None, post)
    require(
        type(prepared) is dict
        and prepared.get("merchantPaymentId") == item["merchant_payment_id"]
        and prepared.get("merchantOrderId") == item["merchant_order_id"],
        "fault slot prepared payment identity mismatch",
    )

    _, mutation_view = record_fault_mutation(
        folder=folder, ordinal=3, request=item["mutations"][0], post=post,
    )
    after_authorize = read_proxy_stats(
        base=fault_proxy_base,
        timeout_seconds=fault_control_timeout_seconds,
        call=control_call,
    )
    pilot.exclusive_write(folder / "proxy-after-authorize.json", after_authorize)

    request_steps = [
        step for step in runtime_plan["steps"]
        if step["point"] in {"REQUEST_BEFORE_PROVIDER_SIDE_EFFECT", "REQUEST_AFTER_PROVIDER_COMMIT"}
    ]
    consumed = after_authorize.get("plan", {}).get("consumed_request_steps")
    require(type(consumed) is list and len(consumed) == len(request_steps),
            "planned provider request fault was not consumed exactly once")

    merchant_view: dict[str, Any] | None = mutation_view
    if merchant_view is None or merchant_view.get("providerPaymentId") is None:
        try:
            merchant_view = get(pilot.BASE, f"/payments/{item['merchant_payment_id']}")
        except Exception:
            merchant_view = mutation_view
    provider_id, binding_authority = _provider_binding(
        runtime_plan=runtime_plan,
        stats=after_authorize,
        merchant_view=merchant_view,
    )
    pilot.exclusive_write(folder / "provider-binding.json", {
        "provider_payment_id": provider_id,
        "authority": binding_authority,
    })

    failed_controls = 0
    offsets = sorted({
        step["offset_ms"] for step in runtime_plan["steps"]
        if type(step.get("offset_ms")) is int and step["offset_ms"] > 0
    })
    for offset in offsets:
        response = record_control(
            folder=folder, ordinal=control_ordinal, base=fault_proxy_base,
            path="/__fault/advance", body={"offset_ms": offset}, expected_status=200,
            timeout_seconds=fault_control_timeout_seconds, call=control_call,
        )
        control_ordinal += 1
        executions = response.get("timeline_executions", [])
        require(type(executions) is list, "fault timeline execution record missing")
        for execution in executions:
            if (
                type(execution) is dict
                and execution.get("point") == "PROVIDER_TRANSITION"
                and execution.get("outcome") == "FAILED"
            ):
                require(execution.get("provider_side_effect") is False,
                        "proxy FAILED transition unexpectedly claims provider side effect")
                require(failed_controls == 0, "multiple durable FAILED provider controls requested")
                correlation = (
                    f"fault:{plan['campaign_id']}:d{plan['logical_day_sequence']:06}:"
                    f"s{item['slot']:03}:{item['fault_class']}"
                )
                record_provider_failure(
                    folder=folder,
                    provider_payment_id=provider_id,
                    correlation_id=correlation,
                    timeout_seconds=provider_control_timeout_seconds,
                    fail=fail_provider,
                )
                failed_controls += 1

    expected_failed = sum(
        1
        for step in runtime_plan["steps"]
        if step["point"] == "PROVIDER_TRANSITION"
        and step["action"].get("outcome") == "FAILED"
    )
    require(failed_controls == expected_failed, "durable provider FAILED control count mismatch")

    final_stats = read_proxy_stats(
        base=fault_proxy_base,
        timeout_seconds=fault_control_timeout_seconds,
        call=control_call,
    )
    pilot.exclusive_write(folder / "proxy-final.json", final_stats)
    final_plan = final_stats.get("plan")
    require(type(final_plan) is dict, "final proxy plan snapshot missing")
    require(not final_plan.get("scheduled_request_steps"), "fault request steps remain scheduled")
    require(not final_plan.get("active_request_steps"), "fault request steps remain active")
    require(not final_plan.get("scheduled_timeline_steps"), "fault timeline steps remain scheduled")

    result = {
        "schema": SLOT_SCHEMA,
        "schema_version": 1,
        "decision": "FAULT_SLOT_EXECUTED_NOT_DAY_ACCEPTED",
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "slot": item["slot"],
        "fault_class": item["fault_class"],
        "merchant_payment_id": item["merchant_payment_id"],
        "merchant_order_id": item["merchant_order_id"],
        "provider_payment_id": provider_id,
        "provider_binding_authority": binding_authority,
        "source_revision": source_sha,
        "day_plan_sha256": pilot.sha(pilot.stable(plan)),
        "fault_catalog_sha256": plan["fault_catalog_sha256"],
        "provenance_sha256": provenance_hashes,
        "online_expected_provider_truth": fault_catalog.fault(item["fault_class"])["expected_provider_truth"],
        "end_of_day_provider_truth": fault_catalog.end_of_day_provider_truth(item["fault_class"]),
        "reconciliation": fault_catalog.fault(item["fault_class"])["reconciliation"],
        "independent_provider_verification_required": True,
        "recovery_reconciliation_required": (
            fault_catalog.fault(item["fault_class"])["reconciliation"]["mutation_retry_policy"]
            != "NOT_APPLICABLE"
        ),
        "day_completion_authorized": False,
        "evidence_sha256": _digest_files(folder, {"result.json"}),
    }
    pilot.exclusive_write(folder / "result.json", result)
    return validate_completed_fault_slot(
        plan=plan, item=item, evidence_root=root, source_sha=source_sha,
        provenance_hashes=provenance_hashes,
    )


def _summary(
    *,
    plan: dict[str, Any],
    faults: list[dict[str, Any]],
    results: list[dict[str, Any]],
    source_sha: str,
    provenance_hashes: dict[str, str],
) -> dict[str, Any]:
    by_slot = {result["slot"]: result for result in results}
    require(set(by_slot) == {item["slot"] for item in faults}, "fault summary slot set mismatch")
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_sha,
        "day_plan_sha256": pilot.sha(pilot.stable(plan)),
        "fault_catalog_sha256": plan["fault_catalog_sha256"],
        "provenance_sha256": provenance_hashes,
        "fault_slot_count": len(faults),
        "fault_slots": [
            {
                "slot": item["slot"],
                "fault_class": item["fault_class"],
                "provider_payment_id": by_slot[item["slot"]]["provider_payment_id"],
            }
            for item in faults
        ],
        "scheduled_hard_crash": hard_crash.scheduled_spec(plan),
        "combined_normal_fault_ordering_authorized": False,
        "recovery_reconciliation_required": any(
            by_slot[item["slot"]]["recovery_reconciliation_required"]
            for item in faults
        ),
        "independent_financial_verification_required": True,
        "day_completion_authorized": False,
    }


def execute_fault_day(
    *,
    plan: dict[str, Any],
    evidence_root: Path,
    source_sha: str,
    provenance_hashes: dict[str, str],
    fault_proxy_base: str,
    fault_control_timeout_seconds: float,
    provider_control_timeout_seconds: float,
    run_slot: Callable[..., dict[str, Any]] = execute_fault_slot,
    validate_slot: Callable[..., dict[str, Any]] = validate_completed_fault_slot,
) -> dict[str, Any]:
    faults = fault_slots(plan)
    root = evidence_root.resolve()
    day = pilot.day_path(root, plan)
    require(day.is_dir() and not day.is_symlink(), "normal day evidence missing before fault lane")
    require(pilot.read_object(day / "identity.json") == pilot.day_identity(plan, source_sha, provenance_hashes),
            "fault day identity drift")
    _normal_summary_required(plan=plan, day=day, source_sha=source_sha, provenance_hashes=provenance_hashes)

    results: list[dict[str, Any]] = []
    executed: list[int] = []
    reused: list[int] = []
    with (day / ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for item in faults:
            folder = day / f"slot-{item['slot']:03}"
            if folder.exists():
                results.append(validate_slot(
                    plan=plan, item=item, evidence_root=root, source_sha=source_sha,
                    provenance_hashes=provenance_hashes,
                ))
                reused.append(item["slot"])
                continue
            results.append(run_slot(
                plan=plan, item=item, evidence_root=root, source_sha=source_sha,
                provenance_hashes=provenance_hashes, fault_proxy_base=fault_proxy_base,
                fault_control_timeout_seconds=fault_control_timeout_seconds,
                provider_control_timeout_seconds=provider_control_timeout_seconds,
            ))
            executed.append(item["slot"])

        summary = _summary(
            plan=plan, faults=faults, results=results,
            source_sha=source_sha, provenance_hashes=provenance_hashes,
        )
        retention = normal_day.retain_exact(day / "fault-traffic.summary.json", summary)
    return {
        "decision": DECISION,
        "fault_slot_count": len(faults),
        "executed_this_invocation": executed,
        "reused_completed_slots": reused,
        "summary_retention": retention,
        "scheduled_hard_crash": summary["scheduled_hard_crash"],
        "day_completion_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 day-wide canonical fault-overlay executor")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--execute-fault-day", action="store_true")
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--provenance-dir", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--fault-proxy-base-url")
    parser.add_argument("--fault-control-timeout-seconds", type=float)
    parser.add_argument("--provider-control-timeout-seconds", type=float)
    args = parser.parse_args()
    try:
        plan = pilot.traffic.build(args.campaign_id, args.day)
        faults = fault_slots(plan)
        if not args.execute_fault_day:
            print(json.dumps({
                "decision": "PLAN_ONLY",
                "fault_slots": [
                    {"slot": item["slot"], "fault_class": item["fault_class"]}
                    for item in faults
                ],
                "scheduled_hard_crash": hard_crash.scheduled_spec(plan),
                "no_http_mutations": True,
                "day_completion_authorized": False,
            }, sort_keys=True))
            return 0

        require(
            args.expected_source_sha is not None
            and args.provenance_dir is not None
            and args.evidence_root is not None,
            "fault execution requires exact source, provenance and evidence root",
        )
        require(args.fault_proxy_base_url is not None, "fault execution requires explicit fault proxy base URL")
        require(
            args.fault_control_timeout_seconds is not None
            and args.fault_control_timeout_seconds > 0
            and args.provider_control_timeout_seconds is not None
            and args.provider_control_timeout_seconds > 0,
            "fault execution requires positive explicit control timeouts",
        )
        proxy = validate_proxy_base(args.fault_proxy_base_url)
        hashes = pilot.qualified_source(args.expected_source_sha, args.provenance_dir)
        pilot.attest_live_topology(args.provenance_dir)
        result = execute_fault_day(
            plan=plan,
            evidence_root=args.evidence_root,
            source_sha=args.expected_source_sha,
            provenance_hashes=hashes,
            fault_proxy_base=proxy,
            fault_control_timeout_seconds=args.fault_control_timeout_seconds,
            provider_control_timeout_seconds=args.provider_control_timeout_seconds,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as exc:
        print(json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
