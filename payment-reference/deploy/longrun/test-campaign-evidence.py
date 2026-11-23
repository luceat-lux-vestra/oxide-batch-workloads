#!/usr/bin/env python3
"""Deterministic, local-only contract and evidence negative controls for #318."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import campaign_day_cardinality as day_cardinality
import campaign_day_financial_invariants as day_financial
import campaign_day_reconciliation as day_reconciliation
import campaign_day_recovery as day_recovery
import campaign_day_online_verification as day_online
import campaign_day_quiescence_verification as day_quiescence
import campaign_day_online_canonical as canonical_online
import campaign_quiescence as quiescence

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("campaign_evidence", HERE / "campaign-evidence.py")
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
m = module

SHA40 = "a" * 40
SHA64 = "b" * 64
SERVICES = ("merchant", "gateway", "killbill", "payment-sandbox", "payment-sandbox-fault-proxy", "merchant-db", "gateway-db", "killbill-db")
CHECKS = (
    "recovery", "reconciliation", "settlement_roster", "settlement_close",
    "financial_invariants", "side_effect_cardinality", "canonical_online", "quiescence",
)


def fixture(root):
    provenance = root / "source-provenance"
    provenance.mkdir()
    services = {service: f"ghcr.io/oxide/{service}@sha256:{SHA64}" for service in SERVICES}
    documents = {
        "host.json": {"repository_sha": SHA40},
        "runtime.json": {"repository_sha": SHA40, "compose_services": list(SERVICES)},
        "container-images.json": {"repository_sha": SHA40, "images": [{"service": s, "image_ref": ref} for s, ref in services.items()]},
        "toolchains.json": {"repository_sha": SHA40},
    }
    hashes = {}
    for name, doc in documents.items():
        path = provenance / name
        path.write_bytes(m.canonical_bytes(doc))
        hashes[name] = m.sha256(path)
    identity = {
        "campaign_id": "test318",
        "source_revision": SHA40,
        "oxide_batch": {"version": "0.7.0", "registry": "crates.io", "lockfile_sha256": SHA64, "artifact_sha256": SHA64},
        "batch_binaries": {name: {"sha256": SHA64, "size_bytes": 1024} for name in m.BINARIES},
        "images": {"manifest_sha256": SHA64, "compose_sha256": SHA64, "services": services},
        "provenance_sha256": hashes,
    }
    return provenance, identity

def mock_timeline(payment_id, kind, contract):
    status = contract["provider_status"]
    if kind == "auth_capture_purchase":
        types = ("payment.created", "payment.authorized", "payment.captured")
    elif kind == "authorize_capture":
        types = ("payment.created", "payment.authorized", "payment.captured", "payment.captured")
    elif kind == "authorize_cancel":
        types = ("payment.created", "payment.authorized", "payment.cancelled")
    elif kind == "auth_capture_refund":
        types = ("payment.created", "payment.authorized", "payment.captured", "payment.refunded", "payment.refunded")
    else:
        types = ("payment.created", "payment.failed" if status == "failed" else "payment.authorized")
    events = []
    for index, event_type in enumerate(types, 1):
        prior = types[index - 2] if index > 1 else None
        events.append({
            "id": f"{payment_id}:{event_type}:{index}",
            "aggregate_id": payment_id,
            "type": event_type,
            "occurred_at": f"2026-11-21T00:00:{index:02}Z",
            "aggregate_version": index,
            "runtime_sequence": 100 + index,
            "correlation_id": f"corr-{payment_id}-{index}",
            "causation_id": "" if index == 1 else f"{payment_id}:{prior}:{index-1}",
        })
    return {
        "payment": {
            "id": payment_id, "amount": 1000, "currency": "USD",
            "status": status, "authorized_amount": contract["authorized_amount_minor"],
            "captured_amount": contract["captured_amount_minor"],
            "refunded_amount": contract["refunded_amount_minor"], "version": len(types),
        },
        "events": events,
    }


def mocked_day_observations(plan):
    rows, provider_timelines = [], {}
    for item in plan["slots"]:
        payment_id = "provider-" + item["merchant_payment_id"]
        contract = day_cardinality.expected(item)
        status = contract["provider_status"]
        timeline = None if status == "ABSENT" else mock_timeline(payment_id, item["kind"], contract)
        provider_timelines[payment_id] = (404, None) if timeline is None else (200, timeline)
        rows.append({
            "slot": item["slot"], "kind": item["kind"], "fault_class": item["fault_class"],
            "merchant_payment_id": item["merchant_payment_id"],
            "provider_payment_id": payment_id, "provider_status": status,
            "provider_version": None if timeline is None else timeline["payment"]["version"],
            "captured_amount_minor": contract["captured_amount_minor"],
            "refunded_amount_minor": contract["refunded_amount_minor"],
            "settlement_eligible": status in {"captured", "refunded"},
        })
    return rows, provider_timelines


def recovery_artifact_for_day(root, sequence, day_plan, day_card, provider_timelines):
    item = next(row for row in day_plan["slots"] if row["fault_class"] == "post-commit-response-loss")
    card_row = next(row for row in day_card["observations"] if row["merchant_payment_id"] == item["merchant_payment_id"])
    provider_id = card_row["provider_payment_id"]
    mutation = item["mutations"][0]["body"]
    prepare = item["prepare_payment"]["body"]
    candidate = {
        "candidate_id": "",
        "merchant_id": day_plan["merchant_setup"][0]["body"]["merchantId"],
        "merchant_order_id": item["merchant_order_id"],
        "merchant_payment_id": item["merchant_payment_id"],
        "operation_id": mutation["operationId"],
        "operation_kind": "AUTHORIZE",
        "payment_state": "PAYMENT_UNKNOWN",
        "operation_state": "UNKNOWN",
        "amount_minor": mutation["amountMinor"],
        "currency": "USD",
        "idempotency_key_digest": day_recovery.secret_digest(mutation["idempotencyKey"]),
        "payment_method_token_digest": day_recovery.secret_digest(prepare["paymentMethodToken"]),
        "provider_payment_id": provider_id,
        "gateway_operation_id": mutation["operationId"],
        "source_updated_at": "2026-11-21T03:00:00.000000Z",
    }
    candidate["candidate_id"] = day_recovery.candidate_identity(candidate)
    attempt = {
        "candidate_id": candidate["candidate_id"],
        "attempt_no": 1,
        "processing_generation": 1,
        "attempt_state": "OBSERVED",
        "merchant_http_status": 200,
        "canonical_payment_state": "AUTHORIZED",
        "canonical_operation_state": "SUCCEEDED",
        "canonical_provider_payment_id": provider_id,
        "canonical_gateway_operation_id": candidate["operation_id"],
        "provider_visibility": "IMMEDIATE",
        "provider_payment_id": provider_id,
        "provider_currency": "USD",
        "provider_status": "authorized",
        "provider_authorized_amount_minor": 1000,
        "provider_captured_amount_minor": 0,
        "provider_refunded_amount_minor": 0,
        "outcome_kind": "MERCHANT_CANONICAL_RESPONSE",
    }
    result = {
        "candidate_id": candidate["candidate_id"],
        "classification": "RESOLVED_SUCCEEDED",
        "duplicate_provider_mutation_count": 0,
        "final_canonical_payment_state": "AUTHORIZED",
        "final_canonical_operation_state": "SUCCEEDED",
        "final_provider_status": "authorized",
        "result_digest": "",
        "verified": False,
        "oracle_violations": [],
    }
    result["result_digest"] = day_recovery.qualification.digest(candidate, attempt, result)
    run_id = day_recovery.expected_run_id(root.name, sequence)
    snapshot = {
        "run": {
            "run_id": run_id,
            "contract_version": day_recovery.RUN_CONTRACT,
            "oxide_batch_version": day_recovery.OXIDE_BATCH_VERSION,
            "business_date": "2026-11-21",
            "eligibility_cutoff": "2026-11-21T03:01:00.000000Z",
            "scenario_set_id": run_id,
            "policy_version": day_recovery.POLICY_VERSION,
            "candidate_set_digest": day_recovery.candidate_set_digest([candidate]),
            "processing_generation": 1,
            "run_state": "PROCESSED",
        },
        "candidates": [candidate],
        "attempts": [attempt],
        "results": [result],
    }
    crash = None
    if day_recovery.scheduled_hard_crash(day_plan) == "recovery-after-external-convergence-before-local-commit":
        crash = {
            "schema": day_recovery.CRASH_SCHEMA,
            "schema_version": 1,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "run_id": run_id,
            "target_candidate_id": candidate["candidate_id"],
            "boundary": "after-external-convergence-before-local-commit",
            "semantic_marker_observed": True,
            "signal": 9,
            "killed_pid": 1201,
            "process_gone_after_kill": True,
            "pre_recovery_run_state": "PROCESSING",
            "pre_recovery_target_attempt_count": 0,
            "recover_process_pid": 1202,
            "recover_launched": True,
            "post_recovery_processing_generation": 1,
        }
    artifact = day_recovery.collect(
        plan=day_plan,
        source_revision=SHA40,
        workload_binary_sha256=SHA64,
        snapshot=snapshot,
        crash_evidence=crash,
        provider_read=lambda payment_id: provider_timelines[payment_id],
    )
    relative = f"days/day-{sequence:06}.recovery.verification.json"
    path = root / relative
    m.create_only(path, m.canonical_bytes(artifact))
    subject = day_recovery.validate(
        artifact,
        plan=day_plan,
        source_revision=SHA40,
        workload_binary_sha256=SHA64,
    )
    return relative, path, artifact, subject


def financial_artifact_for_day(root, sequence, day_plan, day_card):
    observations = []
    authorized_total = captured_total = refunded_total = settlement_eligible = 0
    for item, card_row in zip(day_plan["slots"], day_card["observations"]):
        provider = copy.deepcopy(card_row["timeline"]["payment"])
        provider_id = card_row["provider_payment_id"]
        kb_id = "kb-" + item["merchant_payment_id"]
        if item["kind"] == "fault_overlay":
            truth = day_financial.financial.fault_catalog.end_of_day_provider_truth(item["fault_class"])
            payment_state = "AUTHORIZED" if truth == "AUTHORIZED" else "PAYMENT_FAILED"
            operation_state = "SUCCEEDED" if truth == "AUTHORIZED" else "FAILED"
            kb_transactions = [{
                "transactionType": "AUTHORIZE",
                "transactionStatus": "SUCCESS" if truth == "AUTHORIZED" else "PAYMENT_FAILURE",
            }]
        else:
            payment_state, _, tx_counts, _, _ = day_financial.financial.EXPECTED[item["kind"]]
            operation_state = "SUCCEEDED"
            kb_transactions = [
                {"transactionType": tx, "transactionStatus": "SUCCESS"}
                for tx, count in tx_counts.items()
                for _ in range(count)
            ]
        merchant = {
            "merchantPaymentId": item["merchant_payment_id"],
            "merchantOrderId": item["merchant_order_id"],
            "amountMinor": 1000,
            "currency": "USD",
            "paymentState": payment_state,
        }
        gateway = {
            "merchantPaymentId": item["merchant_payment_id"],
            "paymentState": payment_state,
            "lastOperationState": operation_state,
            "mapping": {
                "merchantPaymentId": item["merchant_payment_id"],
                "killbillPaymentId": kb_id,
                "providerPaymentId": provider_id,
            },
        }
        killbill = {"paymentId": kb_id, "transactions": kb_transactions}
        if item["kind"] == "fault_overlay":
            proof = day_financial.financial.canonical_fault_consistency(
                payment_id=item["merchant_payment_id"],
                order_id=item["merchant_order_id"],
                fault_class=item["fault_class"],
                merchant=merchant, gateway=gateway, provider=provider, killbill=killbill,
            )
        else:
            proof = day_financial.financial.canonical_consistency(
                payment_id=item["merchant_payment_id"],
                order_id=item["merchant_order_id"],
                case=item["kind"],
                merchant=merchant, gateway=gateway, provider=provider, killbill=killbill,
            )
        expected = day_cardinality.expected(item)
        authorized_total += expected["authorized_amount_minor"]
        captured_total += expected["captured_amount_minor"]
        refunded_total += expected["refunded_amount_minor"]
        if expected["provider_status"] in {"captured", "refunded"}:
            settlement_eligible += 1
        observations.append({
            "slot": item["slot"],
            "kind": item["kind"],
            "fault_class": item["fault_class"],
            "merchant_payment_id": item["merchant_payment_id"],
            "merchant_order_id": item["merchant_order_id"],
            "merchant": merchant,
            "gateway": gateway,
            "provider": provider,
            "killbill": killbill,
            "proof": proof,
        })
    artifact = {
        "schema": day_financial.SCHEMA,
        "schema_version": day_financial.SCHEMA_VERSION,
        "decision": day_financial.DECISION,
        "campaign_id": root.name,
        "logical_day_sequence": sequence,
        "source_revision": SHA40,
        "day_plan_sha256": day_financial.plan_digest(day_plan),
        "financial_semantics_sha256": day_financial.file_sha256(day_financial.FINANCIAL_VERIFIER),
        "observed_slot_count": 100,
        "normal_slot_count": 90,
        "fault_slot_count": 10,
        "authorized_total_minor": authorized_total,
        "captured_total_minor": captured_total,
        "refunded_total_minor": refunded_total,
        "net_settlement_total_minor": captured_total - refunded_total,
        "settlement_eligible_count": settlement_eligible,
        "observations": observations,
    }
    relative = f"days/day-{sequence:06}.financial-invariants.verification.json"
    path = root / relative
    m.create_only(path, m.canonical_bytes(artifact))
    subject = day_financial.validate(artifact, plan=day_plan, source_revision=SHA40)
    return relative, path, artifact, subject


def reconciliation_artifact_for_day(root, sequence, day_plan, day_card):
    internal = []
    provider_rows = []
    status_map = {"authorized": "AUTHORIZED", "captured": "CAPTURED", "refunded": "REFUNDED"}
    for row in day_card["observations"]:
        proof = row["verification"]
        if proof["payment_status"] not in status_map:
            continue
        provider_id = row["provider_payment_id"]
        normalized = status_map[proof["payment_status"]]
        internal.append({
            "source_row_id": "internal:" + row["merchant_payment_id"],
            "merchant_payment_id": row["merchant_payment_id"],
            "provider_payment_id": provider_id,
            "currency": "USD",
            "normalized_status": normalized,
            "authorized_amount_minor": proof["authorized_amount_minor"],
            "captured_amount_minor": proof["captured_amount_minor"],
            "refunded_amount_minor": proof["refunded_amount_minor"],
        })
        provider_rows.append({
            "source_row_id": "provider:" + provider_id,
            "provider_payment_id": provider_id,
            "currency": "USD",
            "normalized_status": normalized,
            "authorized_amount_minor": proof["authorized_amount_minor"],
            "captured_amount_minor": proof["captured_amount_minor"],
            "refunded_amount_minor": proof["refunded_amount_minor"],
            "provider_version": row["timeline"]["payment"]["version"],
        })
    snapshot_id = day_reconciliation.expected_snapshot_id(root.name, sequence)
    metadata = {
        "snapshot_id": snapshot_id,
        "business_date": m.plan_day(sequence)["logical_business_date"],
        "provider_scope": day_plan["merchant_setup"][0]["body"]["merchantId"],
        "scenario_case": "CLEAR",
        "base_snapshot_id": None,
        "internal_snapshot_digest": day_reconciliation.qualification.row_digest(internal),
        "provider_snapshot_digest": day_reconciliation.qualification.row_digest(provider_rows),
        "policy_version": day_reconciliation.POLICY_VERSION,
    }
    snapshot = {
        "metadata": metadata,
        "internal": internal,
        "provider": provider_rows,
        "mutation_recipe": {},
    }
    oracle = day_reconciliation.qualification.oracle(snapshot)
    actual = {
        "run_id": f"{root.name}:day-{sequence:06}:reconciliation-run",
        "processing_state": "PROCESSED",
        "business_state": oracle["business_state"],
        "mismatch_count": oracle["mismatch_count"],
        "aggregate_delta_minor": oracle["aggregate_delta_minor"],
        "result_digest": oracle["result_digest"],
        "details": oracle["details"],
        "published_count": 1,
    }
    report = {
        "schema_version": 1,
        "campaign_issue": 148,
        "classification": day_reconciliation.qualification.CLASSIFICATION,
        "snapshot": snapshot,
        "base_snapshot": None,
        "oracle": oracle,
        "actual": actual,
        "violations": [],
    }
    scheduled = day_reconciliation.scheduled_hard_crash(day_plan)
    crash = None
    if scheduled is not None:
        boundary, recovery_action = day_reconciliation.RECONCILIATION_CRASHES[scheduled]
        crash = {
            "schema": day_reconciliation.CRASH_SCHEMA,
            "schema_version": 1,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "snapshot_id": snapshot_id,
            "boundary": boundary,
            "semantic_marker_observed": True,
            "signal": 9,
            "killed_pid": 2201,
            "process_gone_after_kill": True,
            "recovery_action": recovery_action,
            "new_process_pid": 2202,
            "post_recovery_processing_state": "PROCESSED",
            "final_publication_count": 1,
        }
    artifact = {
        "schema": day_reconciliation.SCHEMA,
        "schema_version": day_reconciliation.SCHEMA_VERSION,
        "decision": day_reconciliation.DECISION,
        "campaign_id": root.name,
        "logical_day_sequence": sequence,
        "source_revision": SHA40,
        "day_plan_sha256": day_reconciliation.plan_digest(day_plan),
        "oxide_batch_version": day_reconciliation.OXIDE_BATCH_VERSION,
        "workload_contract": day_reconciliation.WORKLOAD_CONTRACT,
        "workload_binary_sha256": SHA64,
        "workload_semantics_sha256": day_reconciliation.file_sha256(
            day_reconciliation.RECONCILIATION_VERIFIER
        ),
        "producer_report": report,
        "scheduled_hard_crash": scheduled,
        "crash_evidence": crash,
    }
    relative = f"days/day-{sequence:06}.reconciliation.verification.json"
    path = root / relative
    m.create_only(path, m.canonical_bytes(artifact))
    subject = day_reconciliation.validate(
        artifact,
        plan=day_plan,
        source_revision=SHA40,
        workload_binary_sha256=SHA64,
    )
    return relative, path, artifact, subject



def canonical_frontier_artifacts(root, sequence):
    identity = m.load_json(root / "campaign.json")["identity"]

    online_plan = day_online.plan_for(root.name, sequence)
    journal_identity = canonical_online.journal_identity(
        online_plan, SHA40, identity["provenance_sha256"],
    )
    previous = canonical_online.pilot.sha(canonical_online.pilot.stable(journal_identity))
    slot_chain = []
    completion_hashes = []
    fault_results = []
    for item in online_plan["slots"]:
        intent = canonical_online.expected_intent(item, previous)
        result_sha = f"{item['slot']:064x}"
        completion = canonical_online.expected_completion(
            item,
            canonical_online.pilot.sha(canonical_online.pilot.stable(intent)),
            {"result.json": result_sha},
        )
        previous = canonical_online.pilot.sha(
            canonical_online.pilot.stable(completion)
        )
        completion_hashes.append({"slot": item["slot"], "sha256": previous})
        slot_chain.append({
            "slot": item["slot"], "intent": intent, "completion": completion,
        })
        if item["kind"] == "fault_overlay":
            fault_results.append({"recovery_reconciliation_required": True})
    online_summary = canonical_online.summary(
        plan=online_plan,
        source_sha=SHA40,
        provenance_hashes=identity["provenance_sha256"],
        journal_identity_sha256=canonical_online.pilot.sha(
            canonical_online.pilot.stable(journal_identity)
        ),
        completion_hashes=completion_hashes,
        fault_results=fault_results,
    )
    online_artifact = day_online.artifact_from_components(
        plan=online_plan,
        source_revision=SHA40,
        provenance_hashes=identity["provenance_sha256"],
        journal_identity=journal_identity,
        slot_chain=slot_chain,
        online_summary=online_summary,
    )
    online_rel = f"days/day-{sequence:06}.online.verification.json"
    online_path = root / online_rel
    m.create_only(online_path, m.canonical_bytes(online_artifact))
    online_subject = day_online.validate(
        online_artifact,
        plan=online_plan,
        source_revision=SHA40,
        provenance_hashes=identity["provenance_sha256"],
    )

    q_contract = quiescence.load_contract()
    expected_services = sorted(identity["images"]["services"])
    duration = m.plan_day(sequence)["minimum_quiet_seconds"]
    interval = q_contract["target_sample_interval_seconds"]
    seconds = list(range(0, duration, interval))
    if not seconds or seconds[-1] != duration:
        seconds.append(duration)
    existing_lines = (root / "samples.jsonl").read_text(encoding="utf-8").splitlines()
    next_ordinal = len(existing_lines) + 1
    observation = {
        "host": {
            "memory_total_bytes": 16 * 1024**3,
            "memory_available_bytes": 8 * 1024**3,
            "swap_total_bytes": 0,
            "swap_used_bytes": 0,
            "load_average_1m": 0.1,
            "load_average_5m": 0.1,
            "load_average_15m": 0.1,
        },
        "filesystems": [
            {
                "role": "campaign-evidence", "path": str(root),
                "total_bytes": 1000, "used_bytes": 100, "available_bytes": 900,
            },
            {
                "role": "campaign-data", "path": str(root),
                "total_bytes": 1000, "used_bytes": 100, "available_bytes": 900,
            },
        ],
        "containers": [
            {
                "service": service, "running": True, "oom_killed": False,
                "restart_count": 0,
            }
            for service in expected_services
        ],
        "postgres": [
            {
                "service": service, "database": service,
                "total_connections": 1, "active_connections": 0,
                "database_size_bytes": 1024, "oxide_batch_tables": [],
            }
            for service in ("gateway-db", "killbill-db", "merchant-db")
        ],
    }
    q_records = []
    for index, second in enumerate(seconds):
        kind = (
            "quiescence-start" if index == 0 else
            "quiescence-end" if index == len(seconds) - 1 else
            "quiescence-periodic"
        )
        value = quiescence.sample_record(
            campaign_id=root.name,
            sequence=sequence,
            source_revision=SHA40,
            contract=q_contract,
            contract_sha256=m.sha256(quiescence.CONTRACT_PATH),
            kind=kind,
            window_sample_index=index,
            timestamp_unix_nanos_utc=(
                sequence * 1_000_000_000_000 + second * 1_000_000_000 + 1
            ),
            monotonic_elapsed_nanos=second * 1_000_000_000,
            observation=copy.deepcopy(observation),
        )
        value["ordinal"] = next_ordinal + index
        q_records.append(value)
    with (root / "samples.jsonl").open("a", encoding="utf-8") as handle:
        for value in q_records:
            handle.write(json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n")

    quiescence_artifact = day_quiescence.artifact_from_records(
        plan=m.plan_day(sequence),
        campaign_id=root.name,
        source_revision=SHA40,
        expected_services=expected_services,
        records=q_records,
    )
    quiescence_rel = f"days/day-{sequence:06}.quiescence.verification.json"
    quiescence_path = root / quiescence_rel
    m.create_only(quiescence_path, m.canonical_bytes(quiescence_artifact))
    quiescence_subject = day_quiescence.validate(
        quiescence_artifact,
        campaign_dir=root,
        plan=m.plan_day(sequence),
        source_revision=SHA40,
        expected_services=expected_services,
    )

    return {
        "canonical_online": {
            "verdict": "PASS",
            "artifact_schema": day_online.SCHEMA,
            "artifact_schema_version": day_online.SCHEMA_VERSION,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "source_revision": SHA40,
            "relative_path": online_rel,
            "artifact_sha256": m.sha256(online_path),
            "subject": online_subject,
        },
        "quiescence": {
            "verdict": "PASS",
            "artifact_schema": day_quiescence.SCHEMA,
            "artifact_schema_version": day_quiescence.SCHEMA_VERSION,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "source_revision": SHA40,
            "relative_path": quiescence_rel,
            "artifact_sha256": m.sha256(quiescence_path),
            "subject": quiescence_subject,
        },
    }


def settlement_artifacts(root, sequence):
    plan = m.plan_day(sequence)
    day_plan = day_cardinality.plan_for(root.name, sequence)
    rows, provider_timelines = mocked_day_observations(day_plan)
    roster_digest = "sha256:" + ("1" * 64)
    account = "payment-sandbox-default"
    roster = {
        "schema": m.SETTLEMENT_ROSTER_SCHEMA,
        "schema_version": 1,
        "decision": "SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED",
        "campaign_id": root.name,
        "logical_day_sequence": sequence,
        "source_revision": SHA40,
        "day_plan_sha256": day_cardinality.digest(day_plan).removeprefix("sha256:"),
        "plan_contract_sha256": day_plan["plan_contract_sha256"],
        "fault_catalog_sha256": day_plan["fault_catalog_sha256"],
        "provider_account_id": account,
        "settlement_date": plan["logical_business_date"],
        "currency": "USD",
        "observed_slot_count": 100,
        "eligible_payment_count": 80,
        "roster_sha256": roster_digest,
        "observations": rows,
    }
    roster_rel = f"statements/day-{sequence:06}.roster.verification.json"
    roster_path = root / roster_rel
    m.create_only(roster_path, m.canonical_bytes(roster))
    day_card = day_cardinality.collect(
        plan=day_plan, source_revision=SHA40, roster_verification=roster,
        read=lambda payment_id: provider_timelines[payment_id],
    )
    cardinality_rel = f"statements/day-{sequence:06}.provider-cardinality.verification.json"
    cardinality_path = root / cardinality_rel
    m.create_only(cardinality_path, m.canonical_bytes(day_card))
    cardinality_subject = day_cardinality.validate(
        day_card, plan=day_plan, source_revision=SHA40, roster_verification=roster,
    )
    recovery_rel, recovery_path, recovery_artifact, recovery_subject = recovery_artifact_for_day(
        root, sequence, day_plan, day_card, provider_timelines,
    )
    reconciliation_rel, reconciliation_path, reconciliation_artifact, reconciliation_subject = (
        reconciliation_artifact_for_day(root, sequence, day_plan, day_card)
    )
    financial_rel, financial_path, financial_artifact, financial_subject = (
        financial_artifact_for_day(root, sequence, day_plan, day_card)
    )

    statement_id = "statement-" + ("5" * 64)
    statement_digest = "sha256:" + ("6" * 64)
    settlement = {
        "schema": m.SETTLEMENT_VERIFICATION_SCHEMA,
        "schema_version": 1,
        "decision": "SETTLEMENT_VERIFIED_NOT_DAY_ACCEPTED",
        "campaign_id": root.name,
        "logical_day_sequence": sequence,
        "source_revision": SHA40,
        "roster_sha256": roster_digest,
        "roster_verification_sha256": m.compact_json_sha256(roster),
        "statement": {
            "statement_contract_version": "provider-settlement-statement-v1",
            "statement_id": statement_id,
            "statement_digest": statement_digest,
            "statement_artifact_sha256": "sha256:" + ("7" * 64),
            "source_roster_digest": roster_digest,
            "provider_account_id": account,
            "settlement_date": plan["logical_business_date"],
            "currency": "USD",
            "line_count": 80,
            "captured_total_minor": 80_000,
            "refunded_total_minor": 15_000,
            "net_settlement_total_minor": 65_000,
        },
        "merchant_close": {
            "settlement_state": "CLOSED",
            "persisted_row": {
                "statement_id": statement_id,
                "statement_digest": statement_digest,
                "provider_account_id": account,
                "settlement_date": plan["logical_business_date"],
                "currency": "USD",
                "line_count": 80,
                "captured_total_minor": 80_000,
                "refunded_total_minor": 15_000,
                "net_settlement_total_minor": 65_000,
                "settlement_state": "CLOSED",
            },
            "business_event_count": 1,
            "exact_replay_http_status": 200,
            "digest_conflict_http_status": 409,
            "totals_conflict_http_status": 409,
            "authoritative_state_unchanged_after_conflicts": True,
        },
    }
    settlement_rel = f"statements/day-{sequence:06}.settlement.verification.json"
    settlement_path = root / settlement_rel
    m.create_only(settlement_path, m.canonical_bytes(settlement))

    roster_subject = {
        "roster_sha256": roster_digest,
        "provider_account_id": account,
        "settlement_date": plan["logical_business_date"],
        "currency": "USD",
        "eligible_payment_count": 80,
    }
    settlement_subject = {
        "roster_sha256": roster_digest,
        "statement_id": statement_id,
        "statement_digest": statement_digest,
        "provider_account_id": account,
        "settlement_date": plan["logical_business_date"],
        "currency": "USD",
        "line_count": 80,
        "captured_total_minor": 80_000,
        "refunded_total_minor": 15_000,
        "net_settlement_total_minor": 65_000,
    }
    return {
        **canonical_frontier_artifacts(root, sequence),
        "financial_invariants": {
            "verdict": "PASS",
            "artifact_schema": day_financial.SCHEMA,
            "artifact_schema_version": day_financial.SCHEMA_VERSION,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "source_revision": SHA40,
            "relative_path": financial_rel,
            "artifact_sha256": m.sha256(financial_path),
            "subject": financial_subject,
        },
        "reconciliation": {
            "verdict": "PASS",
            "artifact_schema": day_reconciliation.SCHEMA,
            "artifact_schema_version": day_reconciliation.SCHEMA_VERSION,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "source_revision": SHA40,
            "relative_path": reconciliation_rel,
            "artifact_sha256": m.sha256(reconciliation_path),
            "subject": reconciliation_subject,
        },
        "recovery": {
            "verdict": "PASS",
            "artifact_schema": day_recovery.SCHEMA,
            "artifact_schema_version": day_recovery.SCHEMA_VERSION,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "source_revision": SHA40,
            "relative_path": recovery_rel,
            "artifact_sha256": m.sha256(recovery_path),
            "subject": recovery_subject,
        },
        "side_effect_cardinality": {
            "verdict": "PASS",
            "artifact_schema": day_cardinality.SCHEMA,
            "artifact_schema_version": 1,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "source_revision": SHA40,
            "relative_path": cardinality_rel,
            "artifact_sha256": m.sha256(cardinality_path),
            "subject": cardinality_subject,
        },
        "settlement_roster": {
            "verdict": "PASS",
            "artifact_schema": m.SETTLEMENT_ROSTER_SCHEMA,
            "artifact_schema_version": 1,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "source_revision": SHA40,
            "relative_path": roster_rel,
            "artifact_sha256": m.sha256(roster_path),
            "subject": roster_subject,
        },
        "settlement_close": {
            "verdict": "PASS",
            "artifact_schema": m.SETTLEMENT_VERIFICATION_SCHEMA,
            "artifact_schema_version": 1,
            "campaign_id": root.name,
            "logical_day_sequence": sequence,
            "source_revision": SHA40,
            "relative_path": settlement_rel,
            "artifact_sha256": m.sha256(settlement_path),
            "subject": settlement_subject,
        },
    }


def write_terminal(root, sequence, *, started=True):
    if started:
        m.append_event(root, sequence, "day-started", {})
    checks = settlement_artifacts(root, sequence)
    verification = {
        "schema": m.DAY_VERIFICATION_SCHEMA,
        "schema_version": m.DAY_VERIFICATION_SCHEMA_VERSION,
        "campaign_id": root.name,
        "logical_day_sequence": sequence,
        "source_revision": SHA40,
        "verdict": "PASS",
        "checks": checks,
    }
    path = root / "days" / f"{sequence:06}.verification.json"
    m.create_only(path, m.canonical_bytes(verification))
    day = {
        "schema": m.DAY_SCHEMA,
        "schema_version": 1, "campaign_id": root.name,
        "logical_day_sequence": sequence, "plan": m.plan_day(sequence),
        "verdict": "PASS", "independent_verification_sha256": m.sha256(path),
    }
    day_path = root / "days" / f"{sequence:06}.json"
    m.create_only(day_path, m.canonical_bytes(day))
    m.append_event(root, sequence, "day-terminal-success", {"day_sha256": m.sha256(day_path)})


class CampaignEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.provenance, self.identity = fixture(self.base)

    def init(self):
        return m.initialize(self.base / "evidence", self.identity, self.provenance)

    def test_plan_geometry_seed_calendar_crash_and_quiet_schedule(self):
        for seq in range(1, 31):
            plan = m.plan_day(seq)
            self.assertEqual(sum(plan["traffic"].values()), 100)
            self.assertEqual(plan["primary_lifecycles"], 100)
            self.assertEqual(plan["logical_day_sequence"], seq)
            self.assertEqual(plan, m.plan_day(seq))
            self.assertEqual(plan["minimum_quiet_seconds"], 600 if seq % 10 == 0 else 60)
        self.assertEqual(len({m.plan_day(s)["day_seed"] for s in range(1, 31)}), 30)
        self.assertEqual(m.plan_day(1)["logical_business_date"], "2026-11-18")
        self.assertEqual(m.plan_day(2)["logical_business_date"], "2026-11-19")
        self.assertIsNone(m.plan_day(1)["scheduled_hard_crash"])
        self.assertEqual(m.plan_day(3)["scheduled_hard_crash"], "reconciliation-during-write")
        self.assertEqual(m.plan_day(6)["scheduled_hard_crash"], "reconciliation-after-business-commit")
        self.assertEqual(m.plan_day(8)["scheduled_hard_crash"], "recovery-after-external-convergence-before-local-commit")
        self.assertEqual(m.plan_day(10)["scheduled_hard_crash"], "reconciliation-finalize-after-commit")
        self.assertEqual(m.plan_day(13)["scheduled_hard_crash"], m.plan_day(3)["scheduled_hard_crash"])
        for bad in (0, -1, 100001, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(m.EvidenceError):
                m.plan_day(bad)

    def test_initialize_is_create_only_and_provenance_is_immutable(self):
        root = self.init()
        self.assertEqual(m.inspect(root)["decision"], "READY")
        self.assertEqual(m.inspect(root)["next_day"], m.plan_day(1))
        for name in m.ARTIFACTS:
            self.assertEqual(m.sha256(root / "provenance" / name), self.identity["provenance_sha256"][name])
        self.assertEqual((root / "events.jsonl").read_text().count("\n"), 1)
        with self.assertRaises(FileExistsError):
            self.init()
        (root / "provenance" / "host.json").write_text("corruption")
        with self.assertRaises(m.EvidenceError):
            m.inspect(root)

    def test_invalid_identity_and_cross_source_provenance_fail_before_creation(self):
        cases = [
            ("bad campaign ID", ("campaign_id", "../../escape")),
            ("wrong release", ("oxide_batch", {**self.identity["oxide_batch"], "version": "0.6.0"})),
            ("wrong registry", ("oxide_batch", {**self.identity["oxide_batch"], "registry": "git"})),
            ("mutable image", ("images", {**self.identity["images"], "services": {**self.identity["images"]["services"], "gateway": "gateway:latest"}})),
            ("missing binary", ("batch_binaries", {})),
            ("wrong provenance hash", ("provenance_sha256", {**self.identity["provenance_sha256"], "host.json": SHA64})),
        ]
        for label, (key, val) in cases:
            with self.subTest(label=label):
                modified = copy.deepcopy(self.identity)
                modified[key] = val
                with self.assertRaises((m.EvidenceError, ValueError)):
                    m.initialize(self.base / label.replace(" ", "-"), modified, self.provenance)
        corrupted = self.provenance / "runtime.json"
        d = json.loads(corrupted.read_text())
        d["repository_sha"] = "c" * 40
        corrupted.write_bytes(m.canonical_bytes(d))
        changed = copy.deepcopy(self.identity)
        changed["provenance_sha256"]["runtime.json"] = m.sha256(corrupted)
        with self.assertRaisesRegex(m.EvidenceError, "repository SHA"):
            m.initialize(self.base / "changed-revision", changed, self.provenance)

    def test_one_then_two_completed_days_advance_frontier(self):
        root = self.init()
        write_terminal(root, 1)
        self.assertEqual(m.inspect(root)["frontier_sequence"], 1)
        self.assertEqual(m.inspect(root)["next_day"], m.plan_day(2))
        write_terminal(root, 2)
        self.assertEqual(m.inspect(root)["frontier_sequence"], 2)
        self.assertEqual(m.inspect(root)["next_day"], m.plan_day(3))
        with self.assertRaises(FileExistsError):
            write_terminal(root, 2, started=False)

    def test_started_but_not_terminal_is_blocked_across_new_process(self):
        root = self.init()
        m.append_event(root, 1, "day-started", {})
        self.assertEqual(m.inspect(root)["decision"], "BLOCK")
        out = subprocess.run([sys.executable, str(HERE / "campaign-evidence.py"), "status", "--campaign-dir", str(root)], text=True, capture_output=True, check=False)
        self.assertEqual(out.returncode, 75)
        self.assertEqual(json.loads(out.stdout)["frontier_sequence"], 0)

    def test_missing_terminal_event_or_orphan_verification_blocks(self):
        root = self.init()
        m.create_only(root / "days" / "000001.json", b"{}\n")
        with self.assertRaisesRegex(m.EvidenceError, "orphan"):
            m.inspect(root)

    def test_day_namespace_accepts_only_exact_recovery_verification_filename(self):
        root = self.init()
        write_terminal(root, 1)
        self.assertEqual(m.inspect(root)["frontier_sequence"], 1)

        unexpected = root / "days" / "day-000001.recovery.extra.json"
        unexpected.write_text("{}\n")
        with self.assertRaisesRegex(m.EvidenceError, "unexpected day evidence path"):
            m.inspect(root)

    def test_plan_corruption_is_detected_even_if_terminal_hash_recomputed(self):
        root = self.init()
        write_terminal(root, 1)
        day_path = root / "days/000001.json"
        value = m.load_json(day_path)
        value["plan"]["primary_lifecycles"] = 99
        day_path.write_bytes(m.canonical_bytes(value))
        events = m.read_events(root / "events.jsonl", root.name)
        events[-1]["detail"]["day_sha256"] = m.sha256(day_path)
        (root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        with self.assertRaisesRegex(m.EvidenceError, "plan identity mismatch"):
            m.inspect(root)

    def test_verification_corruption_and_missing_checks_block(self):
        root = self.init()
        write_terminal(root, 1)
        verification_path = root / "days/000001.verification.json"
        record = m.load_json(verification_path)
        record["checks"].pop("settlement_close")
        verification_path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "missing verification coverage"):
            m.inspect(root)

    def test_generic_pass_sha_without_bound_descriptors_cannot_advance_frontier(self):
        root = self.init()
        m.append_event(root, 1, "day-started", {})
        verification = {
            "schema": m.DAY_VERIFICATION_SCHEMA,
            "schema_version": 1,
            "campaign_id": root.name,
            "logical_day_sequence": 1,
            "verdict": "PASS",
            "checks": {
                c: {"verdict": "PASS", "artifact_sha256": SHA64}
                for c in CHECKS
            },
        }
        path = root / "days/000001.verification.json"
        m.create_only(path, m.canonical_bytes(verification))
        day = {
            "schema": m.DAY_SCHEMA,
            "schema_version": 1,
            "campaign_id": root.name,
            "logical_day_sequence": 1,
            "plan": m.plan_day(1),
            "verdict": "PASS",
            "independent_verification_sha256": m.sha256(path),
        }
        day_path = root / "days/000001.json"
        m.create_only(day_path, m.canonical_bytes(day))
        m.append_event(root, 1, "day-terminal-success", {"day_sha256": m.sha256(day_path)})
        with self.assertRaisesRegex(m.EvidenceError, "independent verification fields mismatch"):
            m.inspect(root)


    def test_canonical_contract_requires_canonical_online_check(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "days/000001.verification.json"
        record = m.load_json(path)
        record["checks"].pop("canonical_online")
        path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "missing verification coverage"):
            m.inspect(root)

    def test_canonical_contract_requires_quiescence_check(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "days/000001.verification.json"
        record = m.load_json(path)
        record["checks"].pop("quiescence")
        path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "missing verification coverage"):
            m.inspect(root)

    def test_canonical_contract_replays_canonical_online_chain_not_controller_boolean(self):
        root = self.init()
        write_terminal(root, 1)
        online_path = root / "days/day-000001.online.verification.json"
        artifact = m.load_json(online_path)
        artifact["slot_chain"][0]["intent"]["merchant_payment_id"] = "forged-payment"
        online_path.write_bytes(m.canonical_bytes(artifact))
        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["canonical_online"]["artifact_sha256"] = m.sha256(online_path)
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "exact plan/chain"):
            m.inspect(root)

    def test_canonical_contract_replays_quiescence_raw_window_not_descriptor_only(self):
        root = self.init()
        write_terminal(root, 1)
        lines = (root / "samples.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["monotonic_elapsed_nanos"] = 1
        lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        (root / "samples.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            (m.EvidenceError, quiescence.QuiescenceError),
            "digest mismatch|start elapsed time",
        ):
            m.inspect(root)

    def test_settlement_descriptor_must_bind_real_safe_artifact_file(self):
        root = self.init()
        write_terminal(root, 1)
        verification_path = root / "days/000001.verification.json"
        record = m.load_json(verification_path)
        record["checks"]["settlement_roster"]["relative_path"] = "../outside.json"
        verification_path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "unsafe artifact relative path"):
            m.inspect(root)

    def test_settlement_artifact_identity_and_file_sha_are_authoritative(self):
        root = self.init()
        write_terminal(root, 1)
        roster_path = root / "statements/day-000001.roster.verification.json"
        roster = m.load_json(roster_path)
        roster["campaign_id"] = "other-campaign"
        roster_path.write_bytes(m.canonical_bytes(roster))
        with self.assertRaisesRegex(m.EvidenceError, "artifact file SHA mismatch"):
            m.inspect(root)

    def test_settlement_close_must_bind_exact_roster_verification_artifact(self):
        root = self.init()
        write_terminal(root, 1)
        settlement_path = root / "statements/day-000001.settlement.verification.json"
        settlement = m.load_json(settlement_path)
        settlement["roster_verification_sha256"] = "sha256:" + ("f" * 64)
        settlement_path.write_bytes(m.canonical_bytes(settlement))

        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["settlement_close"]["artifact_sha256"] = m.sha256(settlement_path)
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "does not bind exact settlement_roster"):
            m.inspect(root)

    def test_settlement_close_artifact_must_contain_exact_merchant_proof(self):
        root = self.init()
        write_terminal(root, 1)
        settlement_path = root / "statements/day-000001.settlement.verification.json"
        settlement = m.load_json(settlement_path)
        settlement["merchant_close"]["business_event_count"] = 2
        settlement_path.write_bytes(m.canonical_bytes(settlement))

        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["settlement_close"]["artifact_sha256"] = m.sha256(settlement_path)
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "merchant close proof mismatch"):
            m.inspect(root)

    def test_settlement_subjects_must_cross_bind_roster_identity_and_count(self):
        root = self.init()
        write_terminal(root, 1)
        settlement_path = root / "statements/day-000001.settlement.verification.json"
        settlement = m.load_json(settlement_path)
        settlement["statement"]["line_count"] = 79
        settlement["merchant_close"]["persisted_row"]["line_count"] = 79
        settlement_path.write_bytes(m.canonical_bytes(settlement))

        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["settlement_close"]["artifact_sha256"] = m.sha256(settlement_path)
        verification["checks"]["settlement_close"]["subject"]["line_count"] = 79
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "line count differs"):
            m.inspect(root)

    def test_financial_invariants_generic_pass_sha_no_longer_advances_frontier(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "days/000001.verification.json"
        record = m.load_json(path)
        record["checks"]["financial_invariants"] = {"verdict": "PASS", "artifact_sha256": SHA64}
        path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "financial_invariants: bound descriptor fields mismatch"):
            m.inspect(root)

    def test_financial_invariants_replay_raw_cross_system_state(self):
        root = self.init()
        write_terminal(root, 1)
        financial_path = root / "days/day-000001.financial-invariants.verification.json"
        artifact = m.load_json(financial_path)
        row = next(x for x in artifact["observations"] if x["kind"] == "auth_capture_purchase")
        row["merchant"]["paymentState"] = "AUTHORIZED"
        financial_path.write_bytes(m.canonical_bytes(artifact))
        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["financial_invariants"]["artifact_sha256"] = m.sha256(financial_path)
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "Merchant terminal state conflict"):
            m.inspect(root)

    def test_financial_provider_version_crossbinds_day_cardinality(self):
        root = self.init()
        write_terminal(root, 1)
        financial_path = root / "days/day-000001.financial-invariants.verification.json"
        artifact = m.load_json(financial_path)
        artifact["observations"][0]["provider"]["version"] += 1
        financial_path.write_bytes(m.canonical_bytes(artifact))
        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["financial_invariants"]["artifact_sha256"] = m.sha256(financial_path)
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "provider version differs"):
            m.inspect(root)

    def test_financial_merchant_provider_pairs_crossbind_reconciliation(self):
        root = self.init()
        write_terminal(root, 1)
        reconciliation_path = root / "days/day-000001.reconciliation.verification.json"
        artifact = m.load_json(reconciliation_path)
        snapshot = artifact["producer_report"]["snapshot"]
        snapshot["internal"][0]["merchant_payment_id"] = "tampered-payment"
        snapshot["metadata"]["internal_snapshot_digest"] = (
            day_reconciliation.qualification.row_digest(snapshot["internal"])
        )
        artifact["producer_report"]["oracle"] = day_reconciliation.qualification.oracle(snapshot)
        reconciliation_path.write_bytes(m.canonical_bytes(artifact))

        subject = day_reconciliation.validate(
            artifact,
            plan=day_cardinality.plan_for(root.name, 1),
            source_revision=SHA40,
            workload_binary_sha256=SHA64,
        )
        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["reconciliation"]["artifact_sha256"] = m.sha256(reconciliation_path)
        verification["checks"]["reconciliation"]["subject"] = subject
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "Merchant/provider identity pairs differ"):
            m.inspect(root)

    def test_reconciliation_generic_pass_sha_no_longer_advances_frontier(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "days/000001.verification.json"
        record = m.load_json(path)
        record["checks"]["reconciliation"] = {"verdict": "PASS", "artifact_sha256": SHA64}
        path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "reconciliation: bound descriptor fields mismatch"):
            m.inspect(root)

    def test_reconciliation_result_tamper_blocks_even_when_descriptor_sha_is_repaired(self):
        root = self.init()
        write_terminal(root, 1)
        reconciliation_path = root / "days/day-000001.reconciliation.verification.json"
        artifact = m.load_json(reconciliation_path)
        artifact["producer_report"]["actual"]["result_digest"] = "f" * 64
        reconciliation_path.write_bytes(m.canonical_bytes(artifact))
        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["reconciliation"]["artifact_sha256"] = m.sha256(reconciliation_path)
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "reconciliation result digest mismatch"):
            m.inspect(root)

    def test_reconciliation_provider_snapshot_crossbinds_day_cardinality(self):
        root = self.init()
        write_terminal(root, 1)
        reconciliation_path = root / "days/day-000001.reconciliation.verification.json"
        artifact = m.load_json(reconciliation_path)
        provider_rows = artifact["producer_report"]["snapshot"]["provider"]
        provider_rows[0]["provider_version"] += 1
        metadata = artifact["producer_report"]["snapshot"]["metadata"]
        metadata["provider_snapshot_digest"] = day_reconciliation.qualification.row_digest(provider_rows)
        reconciliation_path.write_bytes(m.canonical_bytes(artifact))

        day_plan = day_cardinality.plan_for(root.name, 1)
        subject = day_reconciliation.validate(
            artifact,
            plan=day_plan,
            source_revision=SHA40,
            workload_binary_sha256=SHA64,
        )
        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["reconciliation"]["artifact_sha256"] = m.sha256(reconciliation_path)
        verification["checks"]["reconciliation"]["subject"] = subject
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "provider version differs"):
            m.inspect(root)

    def test_scheduled_reconciliation_crash_requires_execution_evidence(self):
        root = self.init()
        settlement_artifacts(root, 3)
        reconciliation_path = root / "days/day-000003.reconciliation.verification.json"
        artifact = m.load_json(reconciliation_path)
        artifact["crash_evidence"] = None
        with self.assertRaisesRegex(
            day_reconciliation.ReconciliationEvidenceError,
            "scheduled reconciliation crash",
        ):
            day_reconciliation.validate(
                artifact,
                plan=day_cardinality.plan_for(root.name, 3),
                source_revision=SHA40,
                workload_binary_sha256=SHA64,
            )

    def test_recovery_generic_pass_sha_no_longer_advances_frontier(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "days/000001.verification.json"
        record = m.load_json(path)
        record["checks"]["recovery"] = {"verdict": "PASS", "artifact_sha256": SHA64}
        path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "recovery: bound descriptor fields mismatch"):
            m.inspect(root)

    def test_recovery_artifact_result_tamper_blocks_even_when_descriptor_sha_is_repaired(self):
        root = self.init()
        write_terminal(root, 1)
        recovery_path = root / "days/day-000001.recovery.verification.json"
        artifact = m.load_json(recovery_path)
        artifact["candidates"][0]["result"]["result_digest"] = "f" * 64
        recovery_path.write_bytes(m.canonical_bytes(artifact))
        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["recovery"]["artifact_sha256"] = m.sha256(recovery_path)
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "result digest"):
            m.inspect(root)

    def test_recovery_artifact_must_cross_bind_exact_day_provider_cardinality(self):
        root = self.init()
        write_terminal(root, 1)
        cardinality_path = root / "statements/day-000001.provider-cardinality.verification.json"
        cardinality = m.load_json(cardinality_path)
        recovery_path = root / "days/day-000001.recovery.verification.json"
        recovery_artifact = m.load_json(recovery_path)
        payment_id = recovery_artifact["candidates"][0]["candidate"]["merchant_payment_id"]
        row = next(x for x in cardinality["observations"] if x["merchant_payment_id"] == payment_id)
        row["timeline"]["events"][0]["occurred_at"] = "2026-11-21T00:10:01Z"
        day_plan = day_cardinality.plan_for(root.name, 1)
        item = next(x for x in day_plan["slots"] if x["merchant_payment_id"] == payment_id)
        expected = day_cardinality.expected(item)
        row["verification"] = day_cardinality.provider.verify_cardinality(
            provider_payment_id=row["provider_payment_id"],
            timeline=row["timeline"],
            expected_effective_mutations=expected["effective_mutation_count"],
            expected_status=expected["provider_status"],
            expected_authorized=expected["authorized_amount_minor"],
            expected_captured=expected["captured_amount_minor"],
            expected_refunded=expected["refunded_amount_minor"],
        )
        cardinality_path.write_bytes(m.canonical_bytes(cardinality))

        verification_path = root / "days/000001.verification.json"
        verification = m.load_json(verification_path)
        verification["checks"]["side_effect_cardinality"]["artifact_sha256"] = m.sha256(cardinality_path)
        verification_path.write_bytes(m.canonical_bytes(verification))
        with self.assertRaisesRegex(m.EvidenceError, "timeline_sha256 differs"):
            m.inspect(root)

    def test_cardinality_generic_pass_sha_no_longer_advances_frontier(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "days/000001.verification.json"
        record = m.load_json(path)
        record["checks"]["side_effect_cardinality"] = {"verdict": "PASS", "artifact_sha256": SHA64}
        path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "bound descriptor fields mismatch"):
            m.inspect(root)

    def test_cardinality_rejects_missing_slot_even_if_file_sha_recomputed(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "statements/day-000001.provider-cardinality.verification.json"
        value = m.load_json(path)
        value["observations"].pop()
        path.write_bytes(m.canonical_bytes(value))
        verification_path = root / "days/000001.verification.json"
        record = m.load_json(verification_path)
        record["checks"]["side_effect_cardinality"]["artifact_sha256"] = m.sha256(path)
        verification_path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "requires exactly 100 slots"):
            m.inspect(root)

    def test_cardinality_rejects_rehashed_duplicated_provider_ledger(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "statements/day-000001.provider-cardinality.verification.json"
        value = m.load_json(path)
        # Duplicate identity across two slots even when the descriptor SHA is repaired.
        value["observations"][1]["provider_payment_id"] = value["observations"][0]["provider_payment_id"]
        path.write_bytes(m.canonical_bytes(value))
        verification_path = root / "days/000001.verification.json"
        record = m.load_json(verification_path)
        record["checks"]["side_effect_cardinality"]["artifact_sha256"] = m.sha256(path)
        verification_path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "provider mapping"):
            m.inspect(root)

    def test_cardinality_artifact_crossbinds_exact_roster_verification_digest(self):
        root = self.init()
        write_terminal(root, 1)
        path = root / "statements/day-000001.provider-cardinality.verification.json"
        value = m.load_json(path)
        value["roster_verification_sha256"] = "sha256:" + "f" * 64
        path.write_bytes(m.canonical_bytes(value))
        verification_path = root / "days/000001.verification.json"
        record = m.load_json(verification_path)
        record["checks"]["side_effect_cardinality"]["artifact_sha256"] = m.sha256(path)
        verification_path.write_bytes(m.canonical_bytes(record))
        with self.assertRaisesRegex(m.EvidenceError, "plan/roster digest mismatch"):
            m.inspect(root)

    def test_out_of_order_gap_duplicate_terminal_and_truncated_jsonl_block(self):
        root = self.init()
        m.append_event(root, 2, "day-started", {})
        with self.assertRaisesRegex(m.EvidenceError, "gap"):
            m.inspect(root)
        other = self.base / "independent"
        changed_identity = {**self.identity, "campaign_id": "test319"}
        root2 = m.initialize(other, changed_identity, self.provenance)
        m.append_event(root2, 1, "day-started", {})
        m.append_event(root2, 1, "day-started", {})
        with self.assertRaisesRegex(m.EvidenceError, "gap/duplicate"):
            m.inspect(root2)
        (root2 / "events.jsonl").write_bytes((root2 / "events.jsonl").read_bytes() + b'{"truncated":')
        with self.assertRaisesRegex(m.EvidenceError, "torn"):
            list(m.iter_events(root2 / "events.jsonl", root2.name))

    def test_lifecycle_phases_and_controller_restart_boundary(self):
        root = self.init()
        m.append_event(root, 0, "controller-started", {})
        m.append_event(root, 1, "day-started", {})
        for phase in ("traffic", "fault", "batch", "crash", "recovery", "reconciliation", "statement", "close", "quiescence"):
            m.append_event(root, 1, phase, {})
        self.assertEqual(m.inspect(root)["decision"], "BLOCK")
        write_terminal(root, 1, started=False)
        m.append_event(root, 0, "controller-stopped", {})
        m.append_event(root, 0, "controller-started", {})
        self.assertEqual(m.inspect(root)["frontier_sequence"], 1)
        with self.assertRaisesRegex(m.EvidenceError, "unrecognized"):
            m.append_event(root, 2, "skip-verification", {})

    def test_failure_artifact_and_terminal_failure_fail_closed(self):
        root = self.init()
        m.append_event(root, 1, "day-started", {})
        m.append_event(root, 1, "day-terminal-failure", {})
        self.assertEqual(m.inspect(root)["decision"], "BLOCK")
        other = self.base / "other"
        root2 = m.initialize(other, {**self.identity, "campaign_id": "fail318"}, self.provenance)
        (root2 / "failures" / "000001.json").write_text("{}\n")
        self.assertEqual(m.inspect(root2)["decision"], "BLOCK")

    def test_large_journal_streaming_and_terminal_append_guard(self):
        root = self.init()
        for _ in range(300):
            m.append_event(root, 0, "controller-started", {"detail": "safe"})
        self.assertEqual(len(list(m.iter_events(root / "events.jsonl", root.name))), 301)
        self.assertEqual(m.inspect(root)["frontier_sequence"], 0)
        events = root / "events.jsonl"
        with events.open("ab") as out:
            out.write(b"broken")
        with self.assertRaisesRegex(m.EvidenceError, "truncated"):
            m.append_event(root, 0, "controller-stopped", {})
        with self.assertRaisesRegex(m.EvidenceError, "torn"):
            list(m.iter_events(events, root.name))

    def test_cli_plan_output_is_not_runtime_execution(self):
        out = subprocess.run([sys.executable, str(HERE / "campaign-evidence.py"), "plan", "--day", "10"], text=True, capture_output=True, check=False)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout), m.plan_day(10))
        self.assertEqual(list(self.base.iterdir()), [self.provenance])


if __name__ == "__main__":
    unittest.main(verbosity=2)
