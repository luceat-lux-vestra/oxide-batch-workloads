#!/usr/bin/env python3
"""#318 independent per-day payment-recovery proof.

This verifier does not infer provider side-effect cardinality from the recovery
workload. It replays the exact #271 recovery classification/digest semantics
over secret-safe workload state and independently re-reads payment-sandbox
provider timelines for every terminal recovery candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

import campaign_core as core
import campaign_day_cardinality as day_cardinality

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
RECOVERY_VERIFIER = REPO_ROOT / "payment-recovery-postgres" / "evidence" / "verify-retained-evidence.py"
PROVIDER_VERIFIER = HERE / "campaign-provider-cardinality.py"
RECOVERY_COMPOSE = REPO_ROOT / "payment-recovery-postgres" / "docker-compose.yml"

SCHEMA = "oxide-batch-workloads.payment-reference-day-recovery-verification"
SCHEMA_VERSION = 1
DECISION = "RECOVERY_VERIFIED_NOT_DAY_ACCEPTED"
CRASH_SCHEMA = "oxide-batch-workloads.payment-reference-recovery-crash-execution"
RUN_CONTRACT = "payment-recovery-v1"
POLICY_VERSION = "payment-recovery-policy-v1"
OXIDE_BATCH_VERSION = "0.7.0"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
FORBIDDEN_KEYS = {"idempotency_key", "payment_method_token"}
TERMINAL = {"RESOLVED_SUCCEEDED", "RESOLVED_FAILED"}


class RecoveryEvidenceError(core.EvidenceError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RecoveryEvidenceError(message)


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


qualification = _module("payment_recovery_retained_verifier", RECOVERY_VERIFIER)
provider = _module("payment_recovery_provider_cardinality", PROVIDER_VERIFIER)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest_parts(values: list[str]) -> str:
    outer = hashlib.sha256()
    for value in values:
        require(isinstance(value, str), "digest part must be string")
        outer.update(hashlib.sha256(value.encode("utf-8")).digest())
    return outer.hexdigest()


def secret_digest(value: str) -> str:
    return digest_parts([value])


def expected_run_id(campaign_id: str, sequence: int) -> str:
    value = f"{campaign_id}:day-{sequence:06}:recovery"
    require(SAFE_RUN.fullmatch(value) is not None, "derived recovery run id is unsafe")
    return value


def candidate_identity(candidate: dict[str, Any]) -> str:
    return "candidate-" + digest_parts([
        candidate["merchant_payment_id"],
        candidate["operation_id"],
        candidate["operation_kind"],
        candidate["idempotency_key_digest"],
        str(candidate["amount_minor"]),
        candidate["currency"],
    ])


def candidate_set_digest(candidates: list[dict[str, Any]]) -> str:
    require(bool(candidates), "candidate set must not be empty")
    ordered = sorted(candidates, key=lambda x: (x["merchant_payment_id"], x["operation_id"]))
    outer = hashlib.sha256()
    for candidate in ordered:
        values = [
            candidate["candidate_id"],
            candidate["merchant_id"],
            candidate["merchant_order_id"],
            candidate["merchant_payment_id"],
            candidate["operation_id"],
            candidate["operation_kind"],
            candidate["payment_state"],
            candidate["operation_state"],
            str(candidate["amount_minor"]),
            candidate["currency"],
            candidate["idempotency_key_digest"],
            candidate["payment_method_token_digest"],
            candidate.get("provider_payment_id") or "",
            candidate.get("gateway_operation_id") or "",
            candidate["source_updated_at"],
        ]
        for value in values:
            outer.update(hashlib.sha256(str(value).encode("utf-8")).digest())
    return outer.hexdigest()


def scan_secrets(value: Any, path: str = "$") -> list[str]:
    violations: list[str] = []
    if type(value) is dict:
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                violations.append(f"{path}: forbidden raw replay field {key}")
            violations.extend(scan_secrets(child, f"{path}.{key}"))
    elif type(value) is list:
        for index, child in enumerate(value):
            violations.extend(scan_secrets(child, f"{path}[{index}]"))
    elif isinstance(value, str) and (value.startswith("idem-") or value.startswith("tok-")):
        violations.append(f"{path}: raw replay secret leaked")
    return violations


def scheduled_hard_crash(plan: dict[str, Any]) -> str | None:
    sequence = plan.get("logical_day_sequence")
    require(type(sequence) is int and sequence > 0, "recovery plan logical day sequence invalid")
    day = core.plan_day(sequence)
    require(day.get("logical_day_sequence") == sequence, "recovery core day identity mismatch")
    return day.get("scheduled_hard_crash")


def _plan_slots(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    slots = plan.get("slots")
    require(type(slots) is list and len(slots) == 100, "recovery proof requires a complete 100-slot day plan")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(slots, 1):
        item = object_(item, f"plan slot {index}")
        require(item.get("slot") == index, "day plan slot order mismatch")
        payment_id = item.get("merchant_payment_id")
        require(isinstance(payment_id, str) and payment_id not in result, "day plan payment identity invalid/duplicate")
        result[payment_id] = item
    return result


def _validate_candidate_against_plan(candidate: dict[str, Any], item: dict[str, Any], merchant_id: str) -> None:
    required = {
        "candidate_id", "merchant_id", "merchant_order_id", "merchant_payment_id",
        "operation_id", "operation_kind", "payment_state", "operation_state",
        "amount_minor", "currency", "idempotency_key_digest",
        "payment_method_token_digest", "provider_payment_id",
        "gateway_operation_id", "source_updated_at",
    }
    require(set(candidate) == required, f"{candidate.get('candidate_id', '?')}: candidate fields mismatch")
    require(item["kind"] == "fault_overlay", "recovery candidate must belong to a fault-overlay slot")
    mutation = item["mutations"]
    require(type(mutation) is list and len(mutation) == 1, "fault-overlay recovery candidate must have one planned mutation")
    mutation = object_(mutation[0], "fault-overlay mutation")
    prepare = object_(item["prepare_payment"], "fault-overlay prepare payment")
    require(candidate["merchant_id"] == merchant_id, "candidate merchant identity differs from day plan")
    require(candidate["merchant_payment_id"] == item["merchant_payment_id"], "candidate merchant payment differs from plan")
    require(candidate["merchant_order_id"] == item["merchant_order_id"], "candidate order differs from plan")
    require(candidate["operation_kind"] == "AUTHORIZE", "campaign recovery currently supports fault-overlay AUTHORIZE only")
    require(candidate["operation_id"] == mutation["body"]["operationId"], "candidate operation identity differs from plan")
    require(candidate["amount_minor"] == mutation["body"]["amountMinor"] == 1000, "candidate amount differs from plan")
    require(candidate["currency"] == "USD", "candidate currency differs from plan")
    require(candidate["payment_state"] in {"PAYMENT_UNKNOWN", "PAYMENT_PENDING"}, "candidate payment state is not recovery-eligible")
    require(candidate["operation_state"] in {"UNKNOWN", "RETRY_PENDING"}, "candidate operation state is not recovery-eligible")
    require(
        candidate["idempotency_key_digest"] == secret_digest(mutation["body"]["idempotencyKey"]),
        "candidate idempotency digest differs from exact planned replay identity",
    )
    require(
        candidate["payment_method_token_digest"] == secret_digest(prepare["body"]["paymentMethodToken"]),
        "candidate token digest differs from exact planned payment identity",
    )
    require(HEX64.fullmatch(candidate["idempotency_key_digest"]) is not None, "candidate idempotency digest invalid")
    require(HEX64.fullmatch(candidate["payment_method_token_digest"]) is not None, "candidate token digest invalid")
    require(candidate["candidate_id"] == candidate_identity(candidate), "candidate identity digest mismatch")
    require(isinstance(candidate["merchant_id"], str) and candidate["merchant_id"].strip(), "candidate merchant id missing")
    require(isinstance(candidate["provider_payment_id"], str) and candidate["provider_payment_id"].strip(), "candidate provider mapping missing")
    require(candidate["gateway_operation_id"] in (None, candidate["operation_id"]), "candidate Gateway operation mapping mismatch")
    require(isinstance(candidate["source_updated_at"], str) and candidate["source_updated_at"].strip(), "candidate source timestamp missing")


def _validate_attempts(
    *,
    candidate: dict[str, Any],
    attempts: list[dict[str, Any]],
    result: dict[str, Any],
    run_generation: int,
) -> dict[str, Any]:
    require(bool(attempts), f"{candidate['candidate_id']}: no committed recovery attempts")
    expected_fields = {
        "candidate_id", "attempt_no", "processing_generation", "attempt_state",
        "merchant_http_status", "canonical_payment_state", "canonical_operation_state",
        "canonical_provider_payment_id", "canonical_gateway_operation_id",
        "provider_visibility", "provider_payment_id", "provider_currency",
        "provider_status", "provider_authorized_amount_minor",
        "provider_captured_amount_minor", "provider_refunded_amount_minor",
        "outcome_kind",
    }
    previous_generation = 0
    for ordinal, attempt in enumerate(attempts, 1):
        require(set(attempt) == expected_fields, f"{candidate['candidate_id']}: attempt fields mismatch")
        require(attempt["candidate_id"] == candidate["candidate_id"], "attempt candidate identity mismatch")
        require(type(attempt["attempt_no"]) is int and attempt["attempt_no"] == ordinal, "attempt numbers must be contiguous from 1")
        generation = attempt["processing_generation"]
        require(type(generation) is int and 1 <= generation <= run_generation, "attempt processing generation invalid")
        require(generation >= previous_generation, "attempt processing generations moved backward")
        previous_generation = generation

    latest = attempts[-1]
    expected_result_fields = {
        "candidate_id", "classification", "duplicate_provider_mutation_count",
        "final_canonical_payment_state", "final_canonical_operation_state",
        "final_provider_status", "result_digest", "verified", "oracle_violations",
    }
    require(set(result) == expected_result_fields, f"{candidate['candidate_id']}: result fields mismatch")
    require(result["candidate_id"] == candidate["candidate_id"], "result candidate identity mismatch")
    require(result["classification"] in TERMINAL, "recovery candidate did not reach an allowed terminal classification")
    require(result["duplicate_provider_mutation_count"] == 0, "workload recorded duplicate provider mutation")
    require(result["verified"] is False, "workload-owned verified flag must remain false before independent proof")
    require(result["oracle_violations"] == [], "terminal recovery result retains oracle violations")

    recomputed = qualification.classify(candidate, latest, result["duplicate_provider_mutation_count"])
    require(result["classification"] == recomputed, "recovery classification does not independently recompute")
    require(result["result_digest"] == qualification.digest(candidate, latest, result), "recovery result digest mismatch")
    require(result["final_canonical_payment_state"] == latest["canonical_payment_state"], "result canonical payment state differs from latest attempt")
    require(result["final_canonical_operation_state"] == latest["canonical_operation_state"], "result canonical operation state differs from latest attempt")
    require(result["final_provider_status"] == latest["provider_status"], "result provider status differs from latest attempt")
    return latest


def _provider_proof(
    candidate: dict[str, Any],
    latest: dict[str, Any],
    result: dict[str, Any],
    timeline: dict[str, Any],
) -> dict[str, Any]:
    provider_id = candidate["provider_payment_id"]
    require(latest.get("provider_visibility") == "IMMEDIATE", "terminal recovery requires immediate authoritative provider visibility")
    require(latest.get("provider_payment_id") == provider_id, "latest recovery attempt provider identity mismatch")
    require(latest.get("canonical_provider_payment_id") == provider_id, "latest canonical provider identity mismatch")
    require(latest.get("canonical_gateway_operation_id") == candidate["operation_id"], "latest canonical Gateway operation mismatch")
    require(latest.get("provider_currency") == candidate["currency"], "latest provider currency mismatch")
    timeline = object_(timeline, "recovery provider timeline")
    payment = object_(timeline.get("payment"), "recovery provider timeline payment")
    require(payment.get("id") == provider_id, "recovery provider timeline identity mismatch")
    require(payment.get("currency") == candidate["currency"], "recovery provider timeline currency mismatch")
    require(payment.get("amount") == candidate["amount_minor"], "recovery provider timeline amount mismatch")

    if result["classification"] == "RESOLVED_SUCCEEDED":
        expected_status = "authorized" if candidate["operation_kind"] == "AUTHORIZE" else "captured"
        expected_mutations = 1 if candidate["operation_kind"] == "AUTHORIZE" else 2
        expected_authorized = candidate["amount_minor"]
        expected_captured = candidate["amount_minor"] if candidate["operation_kind"] == "AUTH_CAPTURE" else 0
        expected_refunded = 0
    else:
        expected_status = "failed"
        expected_mutations = 1
        expected_authorized = 0
        expected_captured = 0
        expected_refunded = 0

    try:
        proof = provider.verify_cardinality(
            provider_payment_id=provider_id,
            timeline=timeline,
            expected_effective_mutations=expected_mutations,
            expected_status=expected_status,
            expected_authorized=expected_authorized,
            expected_captured=expected_captured,
            expected_refunded=expected_refunded,
        )
    except provider.Block as exc:
        raise RecoveryEvidenceError(f"independent recovery provider proof failed: {exc}") from exc
    require(latest.get("provider_status") == proof["payment_status"], "latest recovery provider status differs from independent timeline")
    require(latest.get("provider_authorized_amount_minor") == proof["authorized_amount_minor"], "latest recovery authorized amount differs from independent timeline")
    require(latest.get("provider_captured_amount_minor") == proof["captured_amount_minor"], "latest recovery captured amount differs from independent timeline")
    require(latest.get("provider_refunded_amount_minor") == proof["refunded_amount_minor"], "latest recovery refunded amount differs from independent timeline")
    return proof

def _validate_crash(
    crash: Any,
    *,
    plan: dict[str, Any],
    run_id: str,
    entries: list[dict[str, Any]],
    run_generation: int,
) -> dict[str, Any] | None:
    scheduled = scheduled_hard_crash(plan)
    recovery_crash = scheduled == "recovery-after-external-convergence-before-local-commit"
    if not recovery_crash:
        require(crash is None, "recovery crash evidence supplied on a day without a scheduled recovery crash")
        return None

    require(crash is not None, "scheduled recovery crash evidence required")
    crash = object_(crash, "recovery crash evidence")
    expected_fields = {
        "schema", "schema_version", "campaign_id", "logical_day_sequence", "run_id",
        "target_candidate_id", "boundary", "semantic_marker_observed", "signal",
        "killed_pid", "process_gone_after_kill", "pre_recovery_run_state",
        "pre_recovery_target_attempt_count", "recover_process_pid",
        "recover_launched", "post_recovery_processing_generation",
    }
    require(set(crash) == expected_fields, "recovery crash evidence fields mismatch")
    require(crash["schema"] == CRASH_SCHEMA and crash["schema_version"] == 1, "recovery crash evidence schema mismatch")
    require(
        crash["campaign_id"] == plan["campaign_id"]
        and crash["logical_day_sequence"] == plan["logical_day_sequence"]
        and crash["run_id"] == run_id,
        "recovery crash campaign/day/run identity mismatch",
    )
    require(crash["boundary"] == "after-external-convergence-before-local-commit", "wrong scheduled recovery crash boundary")
    require(crash["semantic_marker_observed"] is True and crash["signal"] == 9, "recovery crash marker/SIGKILL proof missing")
    require(type(crash["killed_pid"]) is int and crash["killed_pid"] > 1, "invalid killed process pid")
    require(crash["process_gone_after_kill"] is True, "killed recovery process was not proven gone")
    require(crash["pre_recovery_run_state"] == "PROCESSING", "pre-recovery workload state was not PROCESSING")
    require(type(crash["recover_process_pid"]) is int and crash["recover_process_pid"] > 1, "invalid recovery process pid")
    require(crash["recover_process_pid"] != crash["killed_pid"], "recovery was not observed in a distinct process")
    require(crash["recover_launched"] is True, "published recovery path was not launched")
    require(crash["post_recovery_processing_generation"] == run_generation, "recovery generation differs from terminal run state")

    target = next((entry for entry in entries if entry["candidate"]["candidate_id"] == crash["target_candidate_id"]), None)
    require(target is not None, "scheduled recovery crash target is not a frozen candidate")
    before = crash["pre_recovery_target_attempt_count"]
    require(type(before) is int and before >= 0, "pre-recovery target attempt count invalid")
    require(before == len(target["attempts"]) - 1, "scheduled crash did not leave exactly one uncommitted target attempt")
    return {
        "target_candidate_id": crash["target_candidate_id"],
        "boundary": crash["boundary"],
        "signal": 9,
        "new_process_recovery": True,
    }


def validate(
    artifact: Any,
    *,
    plan: dict[str, Any],
    source_revision: str,
    workload_binary_sha256: str,
) -> dict[str, Any]:
    artifact = object_(artifact, "day recovery verification")
    expected_fields = {
        "schema", "schema_version", "decision", "campaign_id",
        "logical_day_sequence", "source_revision", "day_plan_sha256",
        "workload_binary_sha256", "workload_semantics_sha256",
        "provider_authority", "provider_source_revision", "run", "candidate_count",
        "resolved_succeeded_count", "resolved_failed_count", "candidates",
        "scheduled_hard_crash", "crash_evidence",
    }
    require(set(artifact) == expected_fields, "day recovery verification fields mismatch")
    require(
        artifact["schema"] == SCHEMA
        and artifact["schema_version"] == SCHEMA_VERSION
        and artifact["decision"] == DECISION,
        "day recovery verification schema/decision mismatch",
    )
    require(
        artifact["campaign_id"] == plan["campaign_id"]
        and artifact["logical_day_sequence"] == plan["logical_day_sequence"]
        and artifact["source_revision"] == source_revision,
        "day recovery campaign/day/source mismatch",
    )
    require(artifact["day_plan_sha256"] == json_sha256(plan), "day recovery plan digest mismatch")
    require(HEX64.fullmatch(workload_binary_sha256) is not None, "campaign recovery binary SHA invalid")
    require(artifact["workload_binary_sha256"] == workload_binary_sha256, "day recovery binary identity mismatch")
    require(
        artifact["workload_semantics_sha256"] == file_sha256(RECOVERY_VERIFIER),
        "day recovery verifier semantics source mismatch",
    )
    require(
        artifact["provider_authority"] == provider.AUTHORITY
        and artifact["provider_source_revision"] == provider.PAYMENT_SANDBOX_SOURCE_REVISION,
        "day recovery provider authority/source mismatch",
    )
    violations = scan_secrets(artifact)
    require(not violations, violations[0] if violations else "raw recovery secret leaked")

    run = object_(artifact["run"], "recovery run")
    run_fields = {
        "run_id", "contract_version", "oxide_batch_version", "business_date",
        "eligibility_cutoff", "scenario_set_id", "policy_version",
        "candidate_set_digest", "processing_generation", "run_state",
    }
    require(set(run) == run_fields, "recovery run fields mismatch")
    run_id = expected_run_id(plan["campaign_id"], plan["logical_day_sequence"])
    require(run["run_id"] == run_id and run["scenario_set_id"] == run_id, "recovery run/scenario identity is not day-deterministic")
    require(run["contract_version"] == RUN_CONTRACT, "recovery contract version mismatch")
    require(run["oxide_batch_version"] == OXIDE_BATCH_VERSION, "recovery OxideBatch version mismatch")
    require(run["policy_version"] == POLICY_VERSION, "recovery policy version mismatch")
    require(isinstance(run["business_date"], str) and ISO_DATE.fullmatch(run["business_date"]) is not None, "recovery workload business date invalid")
    require(isinstance(run["eligibility_cutoff"], str) and run["eligibility_cutoff"].strip(), "recovery eligibility cutoff missing")
    require(isinstance(run["candidate_set_digest"], str) and HEX64.fullmatch(run["candidate_set_digest"]) is not None, "recovery candidate-set digest invalid")
    require(type(run["processing_generation"]) is int and run["processing_generation"] > 0, "recovery processing generation invalid")
    require(run["run_state"] == "PROCESSED", "recovery workload is not terminal PROCESSED")

    plan_slots = _plan_slots(plan)
    setup = array(plan.get("merchant_setup"), "day merchant setup")
    require(len(setup) == 2, "day merchant setup shape mismatch")
    merchant_id = object_(setup[0], "day merchant setup[0]").get("body", {}).get("merchantId")
    require(isinstance(merchant_id, str) and merchant_id.strip(), "day merchant identity missing")
    entries = array(artifact["candidates"], "recovery candidates")
    require(1 <= len(entries) <= 10, "day recovery candidate count must be 1..10 fault-overlay candidates")
    require(artifact["candidate_count"] == len(entries), "day recovery candidate count mismatch")

    candidate_values: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    seen_payments: set[str] = set()
    succeeded = 0
    failed = 0
    for ordinal, raw in enumerate(entries, 1):
        entry = object_(raw, f"recovery candidate[{ordinal}]")
        require(set(entry) == {"candidate", "attempts", "result", "provider_timeline", "provider_verification"}, "recovery candidate entry fields mismatch")
        candidate = object_(entry["candidate"], f"recovery candidate[{ordinal}].candidate")
        payment_id = candidate.get("merchant_payment_id")
        require(isinstance(payment_id, str) and payment_id in plan_slots, "recovery candidate does not belong to this day plan")
        require(candidate.get("candidate_id") not in seen_candidates, "duplicate recovery candidate identity")
        require(payment_id not in seen_payments, "duplicate recovery Merchant payment")
        seen_candidates.add(candidate["candidate_id"])
        seen_payments.add(payment_id)
        _validate_candidate_against_plan(candidate, plan_slots[payment_id], merchant_id)

        attempts = [object_(x, "recovery attempt") for x in array(entry["attempts"], "recovery attempts")]
        result = object_(entry["result"], "recovery result")
        latest = _validate_attempts(
            candidate=candidate,
            attempts=attempts,
            result=result,
            run_generation=run["processing_generation"],
        )
        provider_proof = _provider_proof(candidate, latest, result, entry["provider_timeline"])
        require(entry["provider_verification"] == provider_proof, "retained recovery provider proof mismatch")
        candidate_values.append(candidate)
        if result["classification"] == "RESOLVED_SUCCEEDED":
            succeeded += 1
        else:
            failed += 1

    require(run["candidate_set_digest"] == candidate_set_digest(candidate_values), "recovery candidate-set digest does not independently recompute")
    require(artifact["resolved_succeeded_count"] == succeeded, "resolved-success count mismatch")
    require(artifact["resolved_failed_count"] == failed, "resolved-failed count mismatch")
    require(succeeded + failed == len(entries), "recovery contains unresolved candidates")
    expected_crash = scheduled_hard_crash(plan)
    require(artifact["scheduled_hard_crash"] == expected_crash, "recovery scheduled-crash identity mismatch")
    crash_subject = _validate_crash(
        artifact["crash_evidence"],
        plan=plan,
        run_id=run_id,
        entries=entries,
        run_generation=run["processing_generation"],
    )

    return {
        "run_id": run_id,
        "candidate_set_digest": run["candidate_set_digest"],
        "candidate_count": len(entries),
        "resolved_succeeded_count": succeeded,
        "resolved_failed_count": failed,
        "processing_generation": run["processing_generation"],
        "workload_binary_sha256": workload_binary_sha256,
        "provider_authority": provider.AUTHORITY,
        "provider_source_revision": provider.PAYMENT_SANDBOX_SOURCE_REVISION,
        "provider_proofs_sha256": json_sha256([entry["provider_verification"] for entry in entries]),
        "scheduled_hard_crash": expected_crash,
        "crash_subject": crash_subject,
    }


def collect(
    *,
    plan: dict[str, Any],
    source_revision: str,
    workload_binary_sha256: str,
    snapshot: dict[str, Any],
    crash_evidence: dict[str, Any] | None,
    provider_read: Callable[[str], tuple[int, dict[str, Any] | None]],
) -> dict[str, Any]:
    run = object_(snapshot.get("run"), "recovery snapshot run")
    candidates = [object_(x, "recovery snapshot candidate") for x in array(snapshot.get("candidates"), "recovery snapshot candidates")]
    attempts = [object_(x, "recovery snapshot attempt") for x in array(snapshot.get("attempts"), "recovery snapshot attempts")]
    results = [object_(x, "recovery snapshot result") for x in array(snapshot.get("results"), "recovery snapshot results")]

    by_attempt: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        by_attempt.setdefault(attempt.get("candidate_id"), []).append(attempt)
    by_result: dict[str, dict[str, Any]] = {}
    for result in results:
        candidate_id = result.get("candidate_id")
        require(isinstance(candidate_id, str) and candidate_id not in by_result, "duplicate recovery result row")
        by_result[candidate_id] = result

    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        require(isinstance(candidate_id, str) and candidate_id in by_result, "recovery candidate missing result")
        payment_id = candidate.get("merchant_payment_id")
        require(isinstance(payment_id, str), "recovery candidate Merchant payment identity missing")
        candidate_attempts = sorted(by_attempt.get(candidate_id, []), key=lambda x: x.get("attempt_no", -1))
        result = by_result[candidate_id]
        latest = _validate_attempts(
            candidate=candidate,
            attempts=candidate_attempts,
            result=result,
            run_generation=run.get("processing_generation"),
        )
        provider_id = candidate.get("provider_payment_id")
        require(isinstance(provider_id, str) and provider_id.strip(), "recovery candidate provider identity missing")
        status, timeline = provider_read(provider_id)
        require(type(status) is int and status == 200 and type(timeline) is dict,
                "terminal recovery provider timeline must be authenticated HTTP 200")
        provider_proof = _provider_proof(candidate, latest, result, timeline)
        entries.append({
            "candidate": candidate,
            "attempts": candidate_attempts,
            "result": result,
            "provider_timeline": timeline,
            "provider_verification": provider_proof,
        })

    require(set(by_attempt) <= {c["candidate_id"] for c in candidates}, "attempt exists for unknown recovery candidate")
    require(set(by_result) == {c["candidate_id"] for c in candidates}, "result set differs from recovery candidate set")
    artifact = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "decision": DECISION,
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_revision": source_revision,
        "day_plan_sha256": json_sha256(plan),
        "workload_binary_sha256": workload_binary_sha256,
        "workload_semantics_sha256": file_sha256(RECOVERY_VERIFIER),
        "provider_authority": provider.AUTHORITY,
        "provider_source_revision": provider.PAYMENT_SANDBOX_SOURCE_REVISION,
        "run": run,
        "candidate_count": len(entries),
        "resolved_succeeded_count": sum(e["result"]["classification"] == "RESOLVED_SUCCEEDED" for e in entries),
        "resolved_failed_count": sum(e["result"]["classification"] == "RESOLVED_FAILED" for e in entries),
        "candidates": entries,
        "scheduled_hard_crash": scheduled_hard_crash(plan),
        "crash_evidence": crash_evidence,
    }
    validate(
        artifact,
        plan=plan,
        source_revision=source_revision,
        workload_binary_sha256=workload_binary_sha256,
    )
    return artifact


def compose_psql(sql: str) -> str:
    compose_file = Path(os.environ.get("PAYMENT_RECOVERY_COMPOSE_FILE", str(RECOVERY_COMPOSE)))
    require(compose_file.is_file(), "payment recovery Compose file missing")
    command = [
        "docker", "compose", "-f", str(compose_file), "exec", "-T", "postgres",
        "psql", "-X", "-v", "ON_ERROR_STOP=1",
        "-U", "oxide_batch_workload", "-d", "payment_recovery_workload",
        "-Atq", "-c", sql,
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    require(completed.returncode == 0, "read-only recovery PostgreSQL query failed")
    return completed.stdout.strip()


def _query_json(sql: str, name: str) -> Any:
    raw = compose_psql(sql)
    require(bool(raw), f"{name}: PostgreSQL returned no JSON")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecoveryEvidenceError(f"{name}: invalid PostgreSQL JSON") from exc


def database_snapshot(run_id: str) -> dict[str, Any]:
    require(SAFE_RUN.fullmatch(run_id) is not None, "unsafe recovery run id")
    literal = run_id.replace("'", "''")
    run = _query_json(f"""
SELECT row_to_json(x)
FROM (
  SELECT run_id, contract_version, oxide_batch_version, business_date::text,
         to_char(eligibility_cutoff AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS eligibility_cutoff,
         scenario_set_id, policy_version, candidate_set_digest,
         processing_generation, run_state
  FROM payment_recovery_workload.payment_recovery_run
  WHERE run_id = '{literal}'
) x
""", "recovery run")
    candidates = _query_json(f"""
SELECT COALESCE(json_agg(row_to_json(x) ORDER BY x.merchant_payment_id,x.operation_id),'[]'::json)
FROM (
  SELECT candidate_id, merchant_id, merchant_order_id, merchant_payment_id,
         operation_id, operation_kind, payment_state, operation_state,
         amount_minor, currency, idempotency_key_digest,
         payment_method_token_digest, provider_payment_id, gateway_operation_id,
         to_char(source_updated_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS source_updated_at
  FROM payment_recovery_workload.payment_recovery_candidate
  WHERE run_id = '{literal}'
) x
""", "recovery candidates")
    attempts = _query_json(f"""
SELECT COALESCE(json_agg(row_to_json(x) ORDER BY x.candidate_id,x.attempt_no),'[]'::json)
FROM (
  SELECT candidate_id, attempt_no, processing_generation, attempt_state,
         merchant_http_status, canonical_payment_state, canonical_operation_state,
         canonical_provider_payment_id, canonical_gateway_operation_id,
         provider_visibility, provider_payment_id, provider_currency, provider_status,
         provider_authorized_amount_minor, provider_captured_amount_minor,
         provider_refunded_amount_minor, outcome_kind
  FROM payment_recovery_workload.payment_recovery_attempt
  WHERE run_id = '{literal}'
) x
""", "recovery attempts")
    results = _query_json(f"""
SELECT COALESCE(json_agg(row_to_json(x) ORDER BY x.candidate_id),'[]'::json)
FROM (
  SELECT candidate_id, classification, duplicate_provider_mutation_count,
         final_canonical_payment_state, final_canonical_operation_state,
         final_provider_status, result_digest, verified, oracle_violations
  FROM payment_recovery_workload.payment_recovery_result
  WHERE run_id = '{literal}'
) x
""", "recovery results")
    return {"run": run, "candidates": candidates, "attempts": attempts, "results": results}


def render_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def retain_immutable(path: Path, value: Any) -> str:
    require(not path.is_symlink(), "recovery output path must not be a symlink")
    raw = render_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        require(path.is_file() and not path.is_symlink(), "existing recovery output is unsafe")
        require(path.read_bytes() == raw, "immutable recovery verification conflict")
        return "replay"
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return "created"


def load_campaign(campaign_dir: Path) -> tuple[str, str, str]:
    require(campaign_dir.is_dir() and not campaign_dir.is_symlink(), "unsafe campaign directory")
    record = object_(json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8")), "campaign")
    require(record.get("schema") == "oxide-batch-workloads.payment-reference-campaign" and record.get("schema_version") == 1, "campaign schema mismatch")
    require(record.get("contract") == core.contract()["contract"], "campaign contract identity mismatch")
    require(record.get("contract_sha256") == core.sha256(core.CONTRACT_PATH), "campaign contract digest mismatch")
    identity = object_(record.get("identity"), "campaign identity")
    campaign_id = identity.get("campaign_id")
    source_revision = identity.get("source_revision")
    binaries = object_(identity.get("batch_binaries"), "campaign batch binaries")
    binary = object_(binaries.get("payment-recovery-postgres"), "payment recovery binary identity")
    binary_sha = binary.get("sha256")
    require(campaign_id == campaign_dir.name and core.CAMPAIGN_ID.fullmatch(campaign_id) is not None, "campaign directory identity mismatch")
    require(isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", source_revision), "campaign source revision invalid")
    require(isinstance(binary_sha, str) and HEX64.fullmatch(binary_sha) is not None, "campaign recovery binary SHA invalid")
    return campaign_id, source_revision, binary_sha


def main() -> int:
    parser = argparse.ArgumentParser(description="#318 independent per-day payment-recovery proof")
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--crash-evidence", type=Path)
    args = parser.parse_args()
    try:
        require(type(args.day) is int and args.day > 0, "day must be positive")
        campaign_id, source_revision, binary_sha = load_campaign(args.campaign_dir)
        plan = day_cardinality.plan_for(campaign_id, args.day)
        run_id = expected_run_id(campaign_id, args.day)
        crash = None
        if args.crash_evidence is not None:
            require(args.crash_evidence.is_file() and not args.crash_evidence.is_symlink(), "recovery crash evidence path unsafe/missing")
            crash = object_(json.loads(args.crash_evidence.read_text(encoding="utf-8")), "recovery crash evidence")
        def read_provider(provider_payment_id: str) -> tuple[int, dict[str, Any] | None]:
            status = provider.provider_timeline_status(provider_payment_id)
            if status != 200:
                return status, None
            return 200, provider.provider_timeline(provider_payment_id)

        artifact = collect(
            plan=plan,
            source_revision=source_revision,
            workload_binary_sha256=binary_sha,
            snapshot=database_snapshot(run_id),
            crash_evidence=crash,
            provider_read=read_provider,
        )
        output = args.campaign_dir / "days" / f"day-{args.day:06}.recovery.verification.json"
        retention = retain_immutable(output, artifact)
        print(json.dumps({
            "decision": DECISION,
            "verification_path": str(output),
            "retention": retention,
            "candidate_count": artifact["candidate_count"],
            "resolved_succeeded_count": artifact["resolved_succeeded_count"],
            "resolved_failed_count": artifact["resolved_failed_count"],
        }, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"decision": "BLOCK", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
