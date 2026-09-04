#!/usr/bin/env python3
"""Canonical retained-evidence verifier for postgres-postgres (campaign #63
PR 4, proof obligation P9).

This never trusts a producer-authored relationship boolean: `row_counts_match`
and `digests_match` are recomputed below from retained primitive values and
then cross-checked against the producer fields. `mismatches_truncated` and
`process_exit_code` are retained observations that are checked as required
facts, never accepted as an overall verdict. Every relationship a violation
can be raised against is recomputed from the retained JSON records' own
primitive fields (row counts, chunk counts, digests, dataset parameters) plus
the evidence manifest's own declared records.

Output contract (`.github/EVIDENCE_CONTRACT.md`'s "Verifier and canonical
verdict" section): a single JSON object printed to stdout,
`{"schema_version": 1, "violations": [...]}`, with a nonzero process exit
whenever `violations` is non-empty.
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1

# The ordinary CI smoke dataset (ci/validate ci's golden-path sequence) uses
# 2,000 rows. Campaign #63 PR 4 / proof obligation P2 requires a materially
# larger retained-evidence dataset -- "not merely 3,000-5,000 rows". This
# floor is deliberately well above that: any retained dataset at or below it
# does not meet the campaign's own stated bar, regardless of what a producer
# script claims about itself.
MIN_MATERIAL_ROWS = 50_000

# PostgreSQL's wire-protocol bind-parameter limit per statement. The count is
# an unsigned 16-bit value. Independent of this workload; a real, external
# constraint the writer-boundedness claim below is checked against.
POSTGRES_MAX_BIND_PARAMS = 65_535

# These are the public oxide-batch 0.6.0 defaults and this workload's fixed
# writer shape. `PostgresBatchMode::multi_row_values()` configures the former;
# `src/writer.rs` supplies the latter.
DEFAULT_MAX_PARAMETERS_PER_STATEMENT = 2_000
WRITER_COLUMNS_PER_ROW = 7
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def field(record: dict, *parts: str):
    value = record
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and SHA256_HEX.fullmatch(value) is not None


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
    required = {"cursor_bounded_resource_run", "paging_bounded_resource_run"}
    if set(record_by_scenario) != required:
        return [
            "postgres-postgres canonical evidence must contain exactly "
            "cursor_bounded_resource_run and paging_bounded_resource_run records"
        ]

    loaded: dict[str, dict] = {}
    for scenario, manifest_record in record_by_scenario.items():
        artifact = manifest_record.get("artifact")
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

    cursor_manifest_record = record_by_scenario["cursor_bounded_resource_run"]
    paging_manifest_record = record_by_scenario["paging_bounded_resource_run"]
    cursor = loaded["cursor_bounded_resource_run"]
    paging = loaded["paging_bounded_resource_run"]

    if cursor.get("scenario") != "cursor_bounded_resource_run":
        violations.append("cursor_bounded_resource_run artifact scenario mismatch")
    if paging.get("scenario") != "paging_bounded_resource_run":
        violations.append("paging_bounded_resource_run artifact scenario mismatch")
    if cursor.get("reader_mode") != "cursor":
        violations.append("cursor_bounded_resource_run artifact reader_mode must be 'cursor'")
    if paging.get("reader_mode") != "paging":
        violations.append("paging_bounded_resource_run artifact reader_mode must be 'paging'")

    # Mode-exclusive configuration (src/job.rs rejects the mode-incompatible
    # flag before any database connection opens -- tests/reader_config.rs):
    # the cursor artifact must carry fetch_size and never page_size, and
    # vice versa. A crossed-over artifact would silently misrepresent which
    # reader component was actually exercised.
    if "fetch_size" not in cursor or "page_size" in cursor:
        violations.append("cursor_bounded_resource_run artifact must have fetch_size and not page_size")
    if "page_size" not in paging or "fetch_size" in paging:
        violations.append("paging_bounded_resource_run artifact must have page_size and not fetch_size")

    # --- Dataset identity: both scenarios must bind the same exact input ---
    cursor_identity = field(cursor_manifest_record, "input", "identity")
    paging_identity = field(paging_manifest_record, "input", "identity")
    if not (isinstance(cursor_identity, dict) and cursor_identity == paging_identity):
        violations.append(
            "cursor and paging scenarios must bind the same exact input identity "
            "(same source content used for both reader modes)"
        )

    input_sha = cursor_identity.get("sha256") if isinstance(cursor_identity, dict) else None
    cursor_reproduction = field(cursor_manifest_record, "input", "reproduction")
    rows = cursor_reproduction.get("rows") if isinstance(cursor_reproduction, dict) else None
    seed = cursor_reproduction.get("seed") if isinstance(cursor_reproduction, dict) else None
    id_offset = cursor_reproduction.get("id_offset") if isinstance(cursor_reproduction, dict) else None

    cursor_dataset = cursor.get("dataset")
    paging_dataset = paging.get("dataset")
    if not isinstance(cursor_dataset, dict) or not isinstance(paging_dataset, dict):
        violations.append("cursor/paging artifacts must contain dataset objects")
    else:
        expected_pairs = {
            "rows": rows,
            "seed": seed,
            "id_offset": id_offset,
            "source_digest_sha256": input_sha,
        }
        for key, expected in expected_pairs.items():
            if cursor_dataset.get(key) != expected:
                violations.append(f"cursor_bounded_resource_run dataset.{key} does not match manifest input identity")
            if paging_dataset.get(key) != expected:
                violations.append(f"paging_bounded_resource_run dataset.{key} does not match manifest input identity")
        # Recomputed, not trusted: the two scenarios' own recorded source
        # digests must actually agree with each other, independent of what
        # the manifest declares.
        if cursor_dataset.get("source_digest_sha256") != paging_dataset.get("source_digest_sha256"):
            violations.append(
                "cursor and paging scenarios must have recomputed the identical source_digest_sha256 "
                "(same underlying source content)"
            )

    # --- P2: materially larger than the ordinary CI smoke dataset ---
    rows_value = rows if isinstance(rows, int) and not isinstance(rows, bool) else None
    if rows_value is None or rows_value < MIN_MATERIAL_ROWS:
        violations.append(
            f"retained dataset rows ({rows_value!r}) must be at least {MIN_MATERIAL_ROWS}, "
            "materially larger than the 2,000-row ordinary CI smoke dataset"
        )

    # --- Chunk size must agree between manifest parameters and artifacts ---
    cursor_params = cursor_manifest_record.get("parameters")
    paging_params = paging_manifest_record.get("parameters")
    chunk_size = cursor_params.get("chunk_size") if isinstance(cursor_params, dict) else None
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        violations.append("manifest cursor_bounded_resource_run chunk_size must be a positive integer")
        chunk_size = None
    else:
        if not isinstance(paging_params, dict) or paging_params.get("chunk_size") != chunk_size:
            violations.append("paging_bounded_resource_run chunk_size must match cursor_bounded_resource_run")
        if cursor.get("chunk_size") != chunk_size:
            violations.append("cursor_bounded_resource_run artifact chunk_size does not match manifest")
        if paging.get("chunk_size") != chunk_size:
            violations.append("paging_bounded_resource_run artifact chunk_size does not match manifest")

    fetch_size = cursor_params.get("fetch_size") if isinstance(cursor_params, dict) else None
    page_size = paging_params.get("page_size") if isinstance(paging_params, dict) else None
    if not isinstance(fetch_size, int) or isinstance(fetch_size, bool) or fetch_size <= 0:
        violations.append("manifest cursor_bounded_resource_run fetch_size must be a positive integer")
    elif cursor.get("fetch_size") != fetch_size:
        violations.append("cursor_bounded_resource_run artifact fetch_size does not match manifest")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
        violations.append("manifest paging_bounded_resource_run page_size must be a positive integer")
    elif paging.get("page_size") != page_size:
        violations.append("paging_bounded_resource_run artifact page_size does not match manifest")

    for name, artifact, params in (
        ("cursor_bounded_resource_run", cursor, cursor_params),
        ("paging_bounded_resource_run", paging, paging_params),
    ):
        import_name = params.get("import_name") if isinstance(params, dict) else None
        if not isinstance(import_name, str) or not import_name:
            violations.append(f"{name} manifest import_name must be a non-empty string")
        elif artifact.get("import_name") != import_name:
            violations.append(f"{name} artifact import_name does not match manifest parameters")

    # --- Recomputed run/verify relationships, per scenario ---
    for name, artifact in (("cursor_bounded_resource_run", cursor), ("paging_bounded_resource_run", paging)):
        run = artifact.get("run")
        verify_section = artifact.get("verify")
        if not isinstance(run, dict) or not isinstance(verify_section, dict):
            violations.append(f"{name} must contain run and verify objects")
            continue

        if run.get("job_execution_status") != "COMPLETED":
            violations.append(f"{name} run.job_execution_status must be COMPLETED")

        committed_read = run.get("committed_read")
        committed_written = run.get("committed_written")
        if rows_value is not None and (committed_read != rows_value or committed_written != rows_value):
            violations.append(f"{name} run.committed_read/committed_written must equal the exact dataset row count")

        chunks_committed = run.get("chunks_committed")
        if rows_value is not None and chunk_size:
            expected_chunks = math.ceil(rows_value / chunk_size)
            if chunks_committed != expected_chunks:
                violations.append(f"{name} run.chunks_committed does not match ceil(rows/chunk_size)")

        # Fail-closed relationships, recomputed from the retained primitive
        # counts/digests -- never read from the producer's own boolean
        # fields directly.
        source_rows = verify_section.get("source_rows")
        destination_rows = verify_section.get("destination_rows")
        total_mismatches = verify_section.get("total_mismatches")
        recomputed_row_counts_match = (
            isinstance(source_rows, int) and not isinstance(source_rows, bool)
            and source_rows == destination_rows
        )
        if verify_section.get("row_counts_match") != recomputed_row_counts_match:
            violations.append(f"{name} verify.row_counts_match does not match recomputed source/destination row counts")
        if not recomputed_row_counts_match:
            violations.append(f"{name} verify.source_rows and verify.destination_rows must be equal")
        if rows_value is not None and source_rows != rows_value:
            violations.append(f"{name} verify.source_rows must equal the exact dataset row count")

        if not isinstance(total_mismatches, int) or isinstance(total_mismatches, bool) or total_mismatches < 0:
            violations.append(f"{name} verify.total_mismatches must be a non-negative integer")
        elif total_mismatches != 0:
            violations.append(f"{name} verify.total_mismatches must be 0 for a clean retained-evidence run")

        if verify_section.get("mismatches_truncated") is not False:
            violations.append(f"{name} verify.mismatches_truncated must be false for a clean retained-evidence run")

        expected_digest = verify_section.get("expected_digest_sha256")
        actual_digest = verify_section.get("actual_digest_sha256")
        expected_digest_valid = is_sha256_hex(expected_digest)
        actual_digest_valid = is_sha256_hex(actual_digest)
        if not expected_digest_valid:
            violations.append(
                f"{name} verify.expected_digest_sha256 must be a lowercase 64-hex SHA-256 string"
            )
        if not actual_digest_valid:
            violations.append(
                f"{name} verify.actual_digest_sha256 must be a lowercase 64-hex SHA-256 string"
            )
        # This is the canonical relationship. The retained producer boolean
        # is checked against it, but can never supply it.
        recomputed_digests_match = (
            expected_digest_valid
            and actual_digest_valid
            and expected_digest == actual_digest
        )
        if verify_section.get("digests_match") != recomputed_digests_match:
            violations.append(
                f"{name} verify.digests_match does not match recomputed expected/actual digest equality"
            )
        if not recomputed_digests_match:
            violations.append(
                f"{name} verify.expected_digest_sha256 and actual_digest_sha256 must be equal "
                "for a clean retained-evidence run"
            )
        if verify_section.get("digests_match") is not True:
            violations.append(f"{name} verify.digests_match must be true for a clean retained-evidence run")

        # Overall pass/fail is recomputed, not trusted: a clean run's verify
        # process must have exited 0, which is only correct exactly when every
        # one of the fail-closed conditions above actually holds.
        recomputed_pass = (
            recomputed_row_counts_match
            and total_mismatches == 0
            and recomputed_digests_match
        )
        if verify_section.get("process_exit_code") != 0:
            violations.append(f"{name} verify.process_exit_code must be 0")
        if verify_section.get("process_exit_code") == 0 and not recomputed_pass:
            violations.append(
                f"{name} verify reported process_exit_code 0 but the recomputed pass/fail relationships "
                "do not actually hold -- a producer-authored exit code is not sufficient evidence on its own"
            )

        # --- P5: writer boundedness, derived from the pinned API, not asserted ---
        writer_config = artifact.get("writer_config")
        if not isinstance(writer_config, dict):
            violations.append(f"{name} must contain a writer_config object")
        else:
            if writer_config.get("mode") != "PostgresBatchMode::MultiRowValues":
                violations.append(f"{name} writer_config.mode must be PostgresBatchMode::MultiRowValues")
            columns_per_row = writer_config.get("columns_per_row")
            max_parameters = writer_config.get("max_parameters_per_statement")
            rows_per_statement = writer_config.get("rows_per_statement")
            max_bound_params = writer_config.get("max_bound_params_per_statement")
            max_sub_batches = writer_config.get("max_sub_batches_per_chunk")

            if not is_positive_int(columns_per_row):
                violations.append(f"{name} writer_config.columns_per_row must be a positive integer")
            elif columns_per_row != WRITER_COLUMNS_PER_ROW:
                violations.append(
                    f"{name} writer_config.columns_per_row must equal the pinned writer shape "
                    f"({WRITER_COLUMNS_PER_ROW})"
                )

            if not is_positive_int(max_parameters):
                violations.append(
                    f"{name} writer_config.max_parameters_per_statement must be a positive integer"
                )
            elif max_parameters != DEFAULT_MAX_PARAMETERS_PER_STATEMENT:
                violations.append(
                    f"{name} writer_config.max_parameters_per_statement must equal the pinned "
                    f"oxide-batch 0.6.0 default ({DEFAULT_MAX_PARAMETERS_PER_STATEMENT})"
                )
            if is_positive_int(max_parameters) and max_parameters > POSTGRES_MAX_BIND_PARAMS:
                violations.append(
                    f"{name} writer_config.max_parameters_per_statement ({max_parameters}) must not exceed "
                    f"PostgreSQL's unsigned 16-bit bind-parameter limit ({POSTGRES_MAX_BIND_PARAMS})"
                )

            if not is_positive_int(rows_per_statement):
                violations.append(f"{name} writer_config.rows_per_statement must be a positive integer")
            elif is_positive_int(columns_per_row) and is_positive_int(max_parameters):
                expected_rows_per_statement = max(max_parameters // columns_per_row, 1)
                if rows_per_statement != expected_rows_per_statement:
                    violations.append(
                        f"{name} writer_config.rows_per_statement must equal "
                        "max(1, max_parameters_per_statement // columns_per_row)"
                    )

            if not is_positive_int(max_bound_params):
                violations.append(
                    f"{name} writer_config.max_bound_params_per_statement must be a positive integer"
                )
            elif is_positive_int(columns_per_row) and is_positive_int(rows_per_statement):
                expected_bound_params = rows_per_statement * columns_per_row
                if max_bound_params != expected_bound_params:
                    violations.append(
                        f"{name} writer_config.max_bound_params_per_statement must equal "
                        "rows_per_statement * columns_per_row (the maximum full sub-batch bind count)"
                    )
                if max_bound_params > POSTGRES_MAX_BIND_PARAMS:
                    violations.append(
                        f"{name} writer_config.max_bound_params_per_statement ({max_bound_params}) must not exceed "
                        f"PostgreSQL's unsigned 16-bit bind-parameter limit ({POSTGRES_MAX_BIND_PARAMS})"
                    )

            if not is_positive_int(max_sub_batches):
                violations.append(f"{name} writer_config.max_sub_batches_per_chunk must be a positive integer")
            elif chunk_size and is_positive_int(rows_per_statement):
                expected_sub_batches = math.ceil(chunk_size / rows_per_statement)
                if max_sub_batches != expected_sub_batches:
                    violations.append(
                        f"{name} writer_config.max_sub_batches_per_chunk must equal "
                        "ceil(chunk_size / rows_per_statement)"
                    )

            if (
                is_positive_int(max_parameters)
                and is_positive_int(columns_per_row)
                and max_parameters < columns_per_row
            ):
                violations.append(
                    f"{name} writer_config.max_parameters_per_statement must be at least "
                    "columns_per_row for the pinned writer configuration"
                )

        # --- Peak RSS observations: presence/shape only. This verifier
        # makes no threshold claim (observational, per campaign #63 PR 4's
        # explicit non-goal against a hosted-CI RSS regression gate) and no
        # cross-host comparability claim.
        for phase in ("run", "verify"):
            section = artifact.get(phase)
            peak = section.get("peak_rss_kib") if isinstance(section, dict) else None
            if not isinstance(peak, int) or isinstance(peak, bool) or peak <= 0:
                violations.append(f"{name} {phase}.peak_rss_kib must be a positive integer (observational, no threshold)")

    return violations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        violations = verify(args.manifest)
    except ValueError as exc:
        violations = [str(exc)]
    result = {"schema_version": SCHEMA_VERSION, "violations": violations}
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if not violations else 1)


if __name__ == "__main__":
    main()
