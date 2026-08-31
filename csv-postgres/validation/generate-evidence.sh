#!/usr/bin/env bash
# Reproduces the clean-run / crash-run / restart-run evidence in this
# directory. Requires a running PostgreSQL reachable at $DATABASE_URL
# (default: the docker-compose service on localhost:5433) and a release
# build of the csv-postgres binary.
#
# The SAME generated CSV file (identical bytes, identical customer_id
# range) is used for both the clean-run and the crash+restart scenarios:
# the business table is reset between them so the second scenario's rows
# don't collide with the first's, which lets the final comparison be a
# real apples-to-apples full-row-content digest (customer_id, name, email,
# amount, created_at all included) rather than one that has to exclude
# offset-derived columns because two different datasets were used.
#
# Usage: DATABASE_URL=... ./validation/generate-evidence.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DATABASE_URL="${DATABASE_URL:-postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5433/csv_postgres_workload}"
export DATABASE_URL
BIN=./target/release/csv-postgres
OUT="$(pwd)/validation"
DATA="$(mktemp -d)"
CHUNK_SIZE=500
FAIL_AT_CHUNK=50

cargo build --release --quiet

psql_scalar() {
  psql "$DATABASE_URL" -t -A -c "$1"
}

reset_all() {
  "$BIN" reset
  # Fully reproducible from a clean slate: also clear OxideBatch's own job
  # metadata (never its schema/tables, only their rows), so re-running this
  # script twice with the same deterministic seeds doesn't collide with a
  # prior run's already-COMPLETED job instance under the same identity.
  psql "$DATABASE_URL" -q -c "TRUNCATE oxide_batch.ob_job_execution CASCADE;" \
                        -c "TRUNCATE oxide_batch.ob_job_instance CASCADE;"
}

"$BIN" migrate
reset_all

# One dataset, used verbatim by every scenario below.
"$BIN" generate --output "$DATA/dataset.csv" --profile normal --seed 20260831 --id-offset 1
DATASET_SHA256=$(python3 -c "import json;print(json.load(open('$DATA/dataset.manifest.json'))['sha256'])")
DATASET_ROWS=$(python3 -c "import json;print(json.load(open('$DATA/dataset.manifest.json'))['rows'])")
DATASET_SIZE=$(python3 -c "import json;print(json.load(open('$DATA/dataset.manifest.json'))['file_size_bytes'])")
TOTAL_CHUNKS=$((DATASET_ROWS / CHUNK_SIZE))
REMAINING_CHUNKS=$((TOTAL_CHUNKS - FAIL_AT_CHUNK))
ROWS_AT_FAIL_AT_CHUNK=$((FAIL_AT_CHUNK * CHUNK_SIZE))
ROWS_REMAINING=$((DATASET_ROWS - ROWS_AT_FAIL_AT_CHUNK))

# ------------------------------------------------------------------ clean --
CLEAN_IMPORT="evidence_clean_run"

CLEAN_START=$(date +%s.%N)
"$BIN" run --input "$DATA/dataset.csv" --import-name "$CLEAN_IMPORT" --chunk-size "$CHUNK_SIZE"
CLEAN_END=$(date +%s.%N)
CLEAN_RUNTIME=$(python3 -c "print(round($CLEAN_END - $CLEAN_START, 3))")

CLEAN_VERIFY=$("$BIN" verify --input "$DATA/dataset.csv")
CLEAN_STATUS=$(psql_scalar "SELECT e.status FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$CLEAN_IMPORT' ORDER BY e.attempt DESC LIMIT 1;")
CLEAN_COMMIT_COUNT=$(psql_scalar "SELECT s.commit_count FROM oxide_batch.ob_step_execution s JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$CLEAN_IMPORT' ORDER BY e.attempt DESC LIMIT 1;")
# Full-row content digest (customer_id, name, email, amount, created_at):
# saved now, before the business table is reset for the crash scenario, so
# the restart phase below can compare against it directly.
CLEAN_DIGEST=$(python3 -c "import json;print(json.loads('''$CLEAN_VERIFY''')['canonical_digest_sha256'])")
CLEAN_DBROWS=$(python3 -c "import json;print(json.loads('''$CLEAN_VERIFY''')['db_row_count'])")

cat > "$OUT/clean-run.json" <<EOF
{
  "scenario": "clean_run",
  "dataset": {"rows": $DATASET_ROWS, "seed": 20260831, "id_offset": 1, "file_size_bytes": $DATASET_SIZE, "sha256": "$DATASET_SHA256"},
  "chunk_size": $CHUNK_SIZE,
  "import_name": "$CLEAN_IMPORT",
  "job_execution_status": "$CLEAN_STATUS",
  "chunks_committed": $CLEAN_COMMIT_COUNT,
  "db_row_count": $CLEAN_DBROWS,
  "final_state_digest_sha256": "$CLEAN_DIGEST",
  "runtime_seconds": $CLEAN_RUNTIME
}
EOF
echo "wrote $OUT/clean-run.json"

# Reset the business table only (job metadata for a *different* import_name
# below doesn't collide regardless): frees the same customer_id range so
# the crash+restart scenario below writes the exact same rows the clean
# scenario just did, from the exact same input bytes.
"$BIN" reset

# ---------------------------------------------------------------- crash --
CRASH_IMPORT="evidence_crash_run"

set +e
"$BIN" run --input "$DATA/dataset.csv" --import-name "$CRASH_IMPORT" --chunk-size "$CHUNK_SIZE" \
  --fail-at "chunk:$FAIL_AT_CHUNK" --failure-mode after-business-commit --hard-crash
CRASH_EXIT=$?
set -e

CRASH_STATUS=$(psql_scalar "SELECT e.status FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$CRASH_IMPORT' ORDER BY e.attempt DESC LIMIT 1;")
CRASH_EXIT_CODE=$(psql_scalar "SELECT e.exit_code FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$CRASH_IMPORT' ORDER BY e.attempt DESC LIMIT 1;")
CRASH_DBROWS=$(psql_scalar "SELECT COUNT(*) FROM app_business.imported_customer WHERE customer_id > 1 AND customer_id <= 1 + $DATASET_ROWS;")

cat > "$OUT/crash-run.json" <<EOF
{
  "scenario": "crash_run",
  "dataset": {"rows": $DATASET_ROWS, "seed": 20260831, "id_offset": 1, "sha256": "$DATASET_SHA256", "note": "same exact file as clean-run.json's dataset"},
  "chunk_size": $CHUNK_SIZE,
  "import_name": "$CRASH_IMPORT",
  "failure_injection": {"fail_at": "chunk:$FAIL_AT_CHUNK", "failure_mode": "after-business-commit", "hard_crash": true},
  "process_exit_code": $CRASH_EXIT,
  "process_terminated_by_signal": $([ "$CRASH_EXIT" -gt 128 ] && echo "$((CRASH_EXIT - 128))" || echo null),
  "job_execution_status_after_crash": "$CRASH_STATUS",
  "job_execution_exit_code_after_crash": "$CRASH_EXIT_CODE",
  "business_db_rows_after_crash": $CRASH_DBROWS,
  "total_chunks": $TOTAL_CHUNKS,
  "expected_rows_from_${FAIL_AT_CHUNK}_committed_chunks": $ROWS_AT_FAIL_AT_CHUNK
}
EOF
echo "wrote $OUT/crash-run.json"

# -------------------------------------------------------------- restart --
"$BIN" recover --import-name "$CRASH_IMPORT" --input "$DATA/dataset.csv"

RESTART_START=$(date +%s.%N)
RESTART_STDERR="$DATA/restart.stderr"
"$BIN" run --input "$DATA/dataset.csv" --import-name "$CRASH_IMPORT" --chunk-size "$CHUNK_SIZE" 2>"$RESTART_STDERR"
cat "$RESTART_STDERR" >&2
RESTART_END=$(date +%s.%N)
RESTART_RUNTIME=$(python3 -c "print(round($RESTART_END - $RESTART_START, 3))")

# Two different, deliberately distinguished counts (spec ss24, Claim A vs
# Claim B): the DB's ob_step_execution row is the *cumulative* total across
# the whole job instance's lineage (inherited progress + this attempt), while
# this restart attempt's own tracing output ("chunk evidence") reports only
# what *this attempt itself* newly read/committed -- i.e. attempts vs.
# final state are not conflated here.
RESTART_STATUS=$(psql_scalar "SELECT e.status FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$CRASH_IMPORT' ORDER BY e.attempt DESC LIMIT 1;")
CUMULATIVE_COMMIT_COUNT=$(psql_scalar "SELECT s.commit_count FROM oxide_batch.ob_step_execution s JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$CRASH_IMPORT' ORDER BY e.attempt DESC LIMIT 1;")
CUMULATIVE_READ_COUNT=$(psql_scalar "SELECT s.read_count FROM oxide_batch.ob_step_execution s JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$CRASH_IMPORT' ORDER BY e.attempt DESC LIMIT 1;")
THIS_ATTEMPT_READ=$(grep -o 'committed_read=[0-9]*' "$RESTART_STDERR" | tail -1 | cut -d= -f2)
THIS_ATTEMPT_WRITTEN=$(grep -o 'committed_written=[0-9]*' "$RESTART_STDERR" | tail -1 | cut -d= -f2)

RESTART_VERIFY=$("$BIN" verify --input "$DATA/dataset.csv")
RESTART_DIGEST=$(python3 -c "import json;print(json.loads('''$RESTART_VERIFY''')['canonical_digest_sha256'])")
RESTART_DBROWS=$(python3 -c "import json;print(json.loads('''$RESTART_VERIFY''')['db_row_count'])")

cat > "$OUT/restart-run.json" <<EOF
{
  "scenario": "restart_run",
  "import_name": "$CRASH_IMPORT",
  "recovery": "RecoveryRequest::mark_failed via 'recover' subcommand",
  "restart_job_execution_status": "$RESTART_STATUS",
  "this_attempt_only": {
    "committed_read": $THIS_ATTEMPT_READ,
    "committed_written": $THIS_ATTEMPT_WRITTEN,
    "note": "rows this restart attempt itself newly processed ($REMAINING_CHUNKS remaining chunks of $TOTAL_CHUNKS total), not the instance total"
  },
  "cumulative_after_restart": {
    "read_count": $CUMULATIVE_READ_COUNT,
    "commit_count": $CUMULATIVE_COMMIT_COUNT,
    "note": "ob_step_execution's persisted total across the whole job instance's lineage (inherited + this attempt)"
  },
  "final_business_db_rows": $RESTART_DBROWS,
  "expected_final_rows": $DATASET_ROWS,
  "runtime_seconds": $RESTART_RUNTIME,
  "clean_run_full_content_digest_sha256": "$CLEAN_DIGEST",
  "recovered_run_full_content_digest_sha256": "$RESTART_DIGEST",
  "full_content_digests_match": $([ "$CLEAN_DIGEST" = "$RESTART_DIGEST" ] && echo true || echo false),
  "digest_note": "same exact input file, same customer_id range (business table reset between the clean and crash scenarios): this compares customer_id/name/email/amount/created_at in full, not an offset-invariant subset"
}
EOF
echo "wrote $OUT/restart-run.json"

rm -rf "$DATA"
