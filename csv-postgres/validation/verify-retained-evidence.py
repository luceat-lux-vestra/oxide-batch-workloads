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


def integer(value, name: str, violations: list[str]) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        violations.append(f"{name} must be an integer")
        return None
    return value


def field(record: dict, *parts: str):
    value = record
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def verify(manifest_path: Path) -> list[str]:
    violations: list[str] = []
    manifest = load_json(manifest_path)
    root = manifest_path.parent.parent

    records = manifest.get("records")
    if not isinstance(records, list):
        return ["manifest records must be an array"]
    record_by_scenario = {
        item.get("scenario"): item
        for item in records
        if isinstance(item, dict) and isinstance(item.get("scenario"), str)
    }
    required = {"clean_run", "crash_run", "restart_run"}
    if set(record_by_scenario) != required:
        return [
            "csv-postgres canonical evidence must contain exactly "
            "clean_run, crash_run, and restart_run records"
        ]

    loaded: dict[str, dict] = {}
    for scenario, record in record_by_scenario.items():
        artifact = record.get("artifact")
        path_value = artifact.get("path") if isinstance(artifact, dict) else None
        if not isinstance(path_value, str):
            violations.append(f"{scenario}: artifact path is missing")
            continue
        try:
            loaded[scenario] = load_json(root / path_value)
        except ValueError as exc:
            violations.append(f"{scenario}: {exc}")

    if set(loaded) != required:
        return violations

    clean_record = record_by_scenario["clean_run"]
    crash_record = record_by_scenario["crash_run"]
    restart_record = record_by_scenario["restart_run"]
    clean = loaded["clean_run"]
    crash = loaded["crash_run"]
    restart = loaded["restart_run"]

    if clean.get("scenario") != "clean_run":
        violations.append("clean_run artifact scenario mismatch")
    if crash.get("scenario") != "crash_run":
        violations.append("crash_run artifact scenario mismatch")
    if restart.get("scenario") != "restart_run":
        violations.append("restart_run artifact scenario mismatch")

    clean_identity = field(clean_record, "input", "identity")
    crash_identity = field(crash_record, "input", "identity")
    restart_identity = field(restart_record, "input", "identity")
    if not (isinstance(clean_identity, dict) and clean_identity == crash_identity == restart_identity):
        violations.append("all three scenarios must bind the same exact input identity")

    input_sha = clean_identity.get("sha256") if isinstance(clean_identity, dict) else None
    input_size = clean_identity.get("size_bytes") if isinstance(clean_identity, dict) else None
    reproduction = field(clean_record, "input", "reproduction")
    rows = reproduction.get("rows") if isinstance(reproduction, dict) else None
    seed = reproduction.get("seed") if isinstance(reproduction, dict) else None
    id_offset = reproduction.get("id_offset") if isinstance(reproduction, dict) else None

    clean_dataset = clean.get("dataset")
    crash_dataset = crash.get("dataset")
    if not isinstance(clean_dataset, dict) or not isinstance(crash_dataset, dict):
        violations.append("clean/crash artifacts must contain dataset objects")
    else:
        expected_pairs = {
            "rows": rows,
            "seed": seed,
            "id_offset": id_offset,
            "sha256": input_sha,
        }
        for key, expected in expected_pairs.items():
            if clean_dataset.get(key) != expected:
                violations.append(f"clean_run dataset.{key} does not match manifest input identity")
            if crash_dataset.get(key) != expected:
                violations.append(f"crash_run dataset.{key} does not match manifest input identity")
        if clean_dataset.get("file_size_bytes") != input_size:
            violations.append("clean_run dataset.file_size_bytes does not match manifest input identity")

    clean_params = clean_record.get("parameters")
    crash_params = crash_record.get("parameters")
    restart_params = restart_record.get("parameters")
    chunk_size = clean_params.get("chunk_size") if isinstance(clean_params, dict) else None
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        violations.append("manifest clean_run chunk_size must be a positive integer")
        chunk_size = None
    else:
        if not isinstance(crash_params, dict) or crash_params.get("chunk_size") != chunk_size:
            violations.append("crash_run chunk_size must match clean_run")
        if not isinstance(restart_params, dict) or restart_params.get("chunk_size") != chunk_size:
            violations.append("restart_run chunk_size must match clean_run")
        if clean.get("chunk_size") != chunk_size:
            violations.append("clean_run artifact chunk_size does not match manifest")
        if crash.get("chunk_size") != chunk_size:
            violations.append("crash_run artifact chunk_size does not match manifest")

    rows_value = integer(rows, "manifest input rows", violations)
    source_rows = integer(clean.get("source_rows"), "clean_run source_rows", violations)
    db_rows = integer(clean.get("db_row_count"), "clean_run db_row_count", violations)
    chunks_committed = integer(clean.get("chunks_committed"), "clean_run chunks_committed", violations)

    if clean.get("job_execution_status") != "COMPLETED":
        violations.append("clean_run job_execution_status must be COMPLETED")
    if rows_value is not None:
        if source_rows != rows_value:
            violations.append("clean_run source_rows must equal input rows")
        if db_rows != rows_value:
            violations.append("clean_run db_row_count must equal input rows")
        if chunk_size and rows_value % chunk_size == 0 and chunks_committed != rows_value // chunk_size:
            violations.append("clean_run chunks_committed must equal rows/chunk_size")

    clean_digest = clean.get("final_state_digest_sha256")
    if not isinstance(clean_digest, str) or len(clean_digest) != 64:
        violations.append("clean_run final_state_digest_sha256 must be present")

    failure = crash_record.get("failure_point")
    if not isinstance(failure, dict):
        violations.append("crash_run manifest failure_point must be an object")
        failure = {}
    artifact_failure = crash.get("failure_injection")
    if not isinstance(artifact_failure, dict):
        violations.append("crash_run artifact failure_injection must be an object")
        artifact_failure = {}
    for key in ("fail_at", "failure_mode", "hard_crash"):
        if artifact_failure.get(key) != failure.get(key):
            violations.append(f"crash_run failure_injection.{key} does not match manifest")

    fail_at = failure.get("fail_at")
    fail_chunk = None
    if isinstance(fail_at, str) and fail_at.startswith("chunk:"):
        try:
            fail_chunk = int(fail_at.split(":", 1)[1])
        except ValueError:
            pass
    if fail_chunk is None or fail_chunk <= 0:
        violations.append("crash_run fail_at must be a positive chunk:N point")
    if failure.get("hard_crash") is not True:
        violations.append("crash_run must identify a hard crash")
    if crash.get("process_exit_code") in (None, 0):
        violations.append("crash_run process_exit_code must be nonzero")
    if crash.get("job_execution_status_after_crash") != "STARTED":
        violations.append("crash_run durable status after hard crash must remain STARTED")

    crash_rows = integer(
        crash.get("business_db_rows_after_crash"),
        "crash_run business_db_rows_after_crash",
        violations,
    )
    total_chunks = integer(crash.get("total_chunks"), "crash_run total_chunks", violations)
    if rows_value is not None and chunk_size:
        expected_total_chunks = rows_value // chunk_size
        if rows_value % chunk_size:
            expected_total_chunks += 1
        if total_chunks != expected_total_chunks:
            violations.append("crash_run total_chunks does not match input/chunk parameters")
        if fail_chunk is not None:
            expected_crash_rows = fail_chunk * chunk_size
            if crash_rows != expected_crash_rows:
                violations.append("crash_run durable business rows do not match deterministic failure point")
            named_key = f"expected_rows_from_{fail_chunk}_committed_chunks"
            if crash.get(named_key) != expected_crash_rows:
                violations.append(f"crash_run {named_key} is inconsistent")

    if restart.get("restart_job_execution_status") != "COMPLETED":
        violations.append("restart_run restart_job_execution_status must be COMPLETED")
    final_rows = integer(
        restart.get("final_business_db_rows"),
        "restart_run final_business_db_rows",
        violations,
    )
    expected_final = integer(
        restart.get("expected_final_rows"),
        "restart_run expected_final_rows",
        violations,
    )
    if rows_value is not None and (final_rows != rows_value or expected_final != rows_value):
        violations.append("restart_run final row counts must equal the exact input row count")

    if isinstance(crash_params, dict) and isinstance(restart_params, dict):
        crash_import = crash_params.get("import_name")
        if restart_params.get("import_name") != crash_import:
            violations.append("restart_run import_name must resume the crash_run instance")
        if restart_params.get("recovery_of") != "crash_run":
            violations.append("restart_run must identify crash_run as its recovery source")
        if crash.get("import_name") != crash_import or restart.get("import_name") != crash_import:
            violations.append("crash/restart artifact import_name must match manifest lineage")

    this_attempt = restart.get("this_attempt_only")
    cumulative = restart.get("cumulative_after_restart")
    if not isinstance(this_attempt, dict) or not isinstance(cumulative, dict):
        violations.append("restart_run must contain this_attempt_only and cumulative_after_restart")
    elif rows_value is not None and crash_rows is not None:
        remaining = rows_value - crash_rows
        if this_attempt.get("committed_read") != remaining:
            violations.append("restart_run committed_read must equal rows remaining after crash")
        if this_attempt.get("committed_written") != remaining:
            violations.append("restart_run committed_written must equal rows remaining after crash")
        if cumulative.get("read_count") != rows_value:
            violations.append("restart_run cumulative read_count must equal input rows")
        if total_chunks is not None and cumulative.get("commit_count") != total_chunks:
            violations.append("restart_run cumulative commit_count must equal total chunks")

    recovered_digest = restart.get("recovered_run_full_content_digest_sha256")
    recorded_clean_digest = restart.get("clean_run_full_content_digest_sha256")
    if not (
        isinstance(clean_digest, str)
        and clean_digest == recorded_clean_digest == recovered_digest
    ):
        violations.append("clean and recovered full-content digests must match exactly")

    # Deliberately do not trust restart["full_content_digests_match"] or any
    # producer-authored passed/verdict/summary field. The relationships above
    # are recomputed from the retained observations.
    return violations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        violations = verify(args.manifest)
    except ValueError as exc:
        violations = [str(exc)]
    result = {
        "schema_version": 1,
        "violations": violations,
        "display_verdict": "pass" if not violations else "fail",
    }
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if not violations else 1)


if __name__ == "__main__":
    main()
