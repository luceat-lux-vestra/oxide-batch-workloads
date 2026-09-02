#!/usr/bin/env python3

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def nested(record: dict, *parts: str):
    value = record
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def integer(value, name: str, violations: list[str]) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        violations.append(f"{name} must be an integer")
        return None
    return value


def verify(manifest_path: Path) -> list[str]:
    violations: list[str] = []
    manifest = load_json(manifest_path)
    root = manifest_path.parent.parent
    records = manifest.get("records")
    if not isinstance(records, list):
        return ["manifest records must be an array"]
    by_scenario = {
        item.get("scenario"): item
        for item in records
        if isinstance(item, dict) and isinstance(item.get("scenario"), str)
    }
    required = {"clean_run", "crash_run", "restart_run"}
    if set(by_scenario) != required:
        return ["csv-postgres evidence must contain exactly clean_run, crash_run, and restart_run"]

    artifacts: dict[str, dict] = {}
    for scenario, record in by_scenario.items():
        path_value = nested(record, "artifact", "path")
        if not isinstance(path_value, str):
            violations.append(f"{scenario}: artifact path is missing")
            continue
        try:
            artifacts[scenario] = load_json(root / path_value)
        except ValueError as exc:
            violations.append(f"{scenario}: {exc}")
    if set(artifacts) != required:
        return violations

    clean_record, crash_record, restart_record = (
        by_scenario["clean_run"], by_scenario["crash_run"], by_scenario["restart_run"]
    )
    clean, crash, restart = artifacts["clean_run"], artifacts["crash_run"], artifacts["restart_run"]
    for expected, artifact in (("clean_run", clean), ("crash_run", crash), ("restart_run", restart)):
        if artifact.get("scenario") != expected:
            violations.append(f"{expected} artifact scenario mismatch")

    identities = [nested(record, "input", "identity") for record in (clean_record, crash_record, restart_record)]
    if not isinstance(identities[0], dict) or not identities[0] == identities[1] == identities[2]:
        violations.append("all scenarios must bind the same exact input identity")
    identity = identities[0] if isinstance(identities[0], dict) else {}
    reproduction = nested(clean_record, "input", "reproduction")
    reproduction = reproduction if isinstance(reproduction, dict) else {}
    expected_dataset = {
        "rows": reproduction.get("rows"),
        "seed": reproduction.get("seed"),
        "id_offset": reproduction.get("id_offset"),
        "sha256": identity.get("sha256"),
    }
    for label, artifact in (("clean_run", clean), ("crash_run", crash)):
        dataset = artifact.get("dataset")
        if not isinstance(dataset, dict):
            violations.append(f"{label} artifact must contain dataset")
            continue
        for key, expected in expected_dataset.items():
            if dataset.get(key) != expected:
                violations.append(f"{label} dataset.{key} does not match manifest input identity")
    if isinstance(clean.get("dataset"), dict) and clean["dataset"].get("file_size_bytes") != identity.get("size_bytes"):
        violations.append("clean_run dataset.file_size_bytes does not match manifest input identity")

    params = [record.get("parameters") for record in (clean_record, crash_record, restart_record)]
    chunk_size = params[0].get("chunk_size") if isinstance(params[0], dict) else None
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        violations.append("manifest chunk_size must be a positive integer")
        chunk_size = None
    else:
        if any(not isinstance(item, dict) or item.get("chunk_size") != chunk_size for item in params[1:]):
            violations.append("all scenarios must use the same chunk_size")
        if clean.get("chunk_size") != chunk_size or crash.get("chunk_size") != chunk_size:
            violations.append("artifact chunk_size does not match manifest")

    rows = integer(reproduction.get("rows"), "manifest input rows", violations)
    clean_rows = integer(clean.get("source_rows"), "clean source_rows", violations)
    db_rows = integer(clean.get("db_row_count"), "clean db_row_count", violations)
    committed = integer(clean.get("chunks_committed"), "clean chunks_committed", violations)
    if clean.get("job_execution_status") != "COMPLETED":
        violations.append("clean_run status must be COMPLETED")
    if rows is not None:
        if clean_rows != rows or db_rows != rows:
            violations.append("clean_run source/database rows must equal input rows")
        if chunk_size and rows % chunk_size == 0 and committed != rows // chunk_size:
            violations.append("clean_run chunks_committed must equal rows/chunk_size")

    failure = crash_record.get("failure_point")
    failure = failure if isinstance(failure, dict) else {}
    artifact_failure = crash.get("failure_injection")
    artifact_failure = artifact_failure if isinstance(artifact_failure, dict) else {}
    for key in ("fail_at", "failure_mode", "hard_crash"):
        if artifact_failure.get(key) != failure.get(key):
            violations.append(f"crash failure_injection.{key} does not match manifest")
    fail_at = failure.get("fail_at")
    fail_chunk = None
    if isinstance(fail_at, str) and fail_at.startswith("chunk:"):
        try:
            fail_chunk = int(fail_at.split(":", 1)[1])
        except ValueError:
            pass
    if fail_chunk is None or fail_chunk <= 0:
        violations.append("crash fail_at must be positive chunk:N")
    if failure.get("hard_crash") is not True:
        violations.append("crash scenario must identify a hard crash")
    if crash.get("process_exit_code") in (None, 0):
        violations.append("crash process_exit_code must be nonzero")
    if crash.get("job_execution_status_after_crash") != "STARTED":
        violations.append("hard crash durable status must remain STARTED")

    crash_rows = integer(crash.get("business_db_rows_after_crash"), "crash business rows", violations)
    total_chunks = integer(crash.get("total_chunks"), "crash total_chunks", violations)
    if rows is not None and chunk_size:
        expected_chunks = (rows + chunk_size - 1) // chunk_size
        if total_chunks != expected_chunks:
            violations.append("crash total_chunks does not match input/chunk parameters")
        if fail_chunk is not None:
            expected_crash_rows = fail_chunk * chunk_size
            if crash_rows != expected_crash_rows:
                violations.append("crash durable rows do not match deterministic failure point")
            if crash.get(f"expected_rows_from_{fail_chunk}_committed_chunks") != expected_crash_rows:
                violations.append("crash expected committed-row observation is inconsistent")

    if restart.get("restart_job_execution_status") != "COMPLETED":
        violations.append("restart status must be COMPLETED")
    final_rows = integer(restart.get("final_business_db_rows"), "restart final rows", violations)
    expected_final = integer(restart.get("expected_final_rows"), "restart expected rows", violations)
    if rows is not None and (final_rows != rows or expected_final != rows):
        violations.append("restart final row counts must equal input rows")

    crash_import = params[1].get("import_name") if isinstance(params[1], dict) else None
    if not isinstance(params[2], dict) or params[2].get("import_name") != crash_import:
        violations.append("restart import_name must resume crash instance")
    if not isinstance(params[2], dict) or params[2].get("recovery_of") != "crash_run":
        violations.append("restart must identify crash_run as recovery source")
    if crash.get("import_name") != crash_import or restart.get("import_name") != crash_import:
        violations.append("crash/restart artifact lineage must match manifest")

    attempt = restart.get("this_attempt_only")
    cumulative = restart.get("cumulative_after_restart")
    if not isinstance(attempt, dict) or not isinstance(cumulative, dict):
        violations.append("restart must contain attempt and cumulative observations")
    elif rows is not None and crash_rows is not None:
        remaining = rows - crash_rows
        if attempt.get("committed_read") != remaining or attempt.get("committed_written") != remaining:
            violations.append("restart attempt counts must equal rows remaining after crash")
        if cumulative.get("read_count") != rows:
            violations.append("restart cumulative read_count must equal input rows")
        if total_chunks is not None and cumulative.get("commit_count") != total_chunks:
            violations.append("restart cumulative commit_count must equal total chunks")

    clean_digest = clean.get("final_state_digest_sha256")
    if not isinstance(clean_digest, str) or len(clean_digest) != 64:
        violations.append("clean final-state digest must be present")
    if not (
        isinstance(clean_digest, str)
        and clean_digest == restart.get("clean_run_full_content_digest_sha256")
        and clean_digest == restart.get("recovered_run_full_content_digest_sha256")
    ):
        violations.append("clean and recovered full-content digests must match exactly")

    # Deliberately do not trust producer-authored booleans such as
    # full_content_digests_match or any passed/verdict/summary field.
    return violations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        violations = verify(args.manifest)
    except ValueError as exc:
        violations = [str(exc)]
    print(json.dumps({
        "schema_version": 1,
        "violations": violations,
        "display_verdict": "pass" if not violations else "fail",
    }, sort_keys=True))
    raise SystemExit(0 if not violations else 1)


if __name__ == "__main__":
    main()
