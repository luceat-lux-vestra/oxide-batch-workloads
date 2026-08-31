#!/usr/bin/env bash
# Reproduces the clean-run / crash-run / restart-run evidence in this
# directory. Requires a running PostgreSQL reachable at $DATABASE_URL
# (default: the docker-compose service on localhost:5433) and a release
# build of the csv-postgres binary.
#
# Usage: DATABASE_URL=... ./validation/generate-evidence.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DATABASE_URL="${DATABASE_URL:-postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5433/csv_postgres_workload}"
export DATABASE_URL
BIN=./target/release/csv-postgres
OUT="$(pwd)/validation"
DATA="$(mktemp -d)"

cargo build --release --quiet
"$BIN" migrate
"$BIN" reset
# Fully reproducible from a clean slate: also clear OxideBatch's own job
# metadata (never its schema/tables, only their rows), so re-running this
# script twice with the same deterministic seeds doesn't collide with a
# prior run's already-COMPLETED job instance under the same identity.
psql "$DATABASE_URL" -q -c "TRUNCATE oxide_batch.ob_job_execution CASCADE;" \
                      -c "TRUNCATE oxide_batch.ob_job_instance CASCADE;"

psql_scalar() {
  psql "$DATABASE_URL" -t -A -c "$1"
}


# ------------------------------------------------------------------ clean --
CLEAN_IMPORT="evidence_clean_run"
"$BIN" generate --output "$DATA/clean.csv" --profile normal --seed 20260831 --id-offset 1
CLEAN_SHA256=$(python3 -c "import json;print(json.load(open('$DATA/clean.manifest.json'))['sha256'])")
CLEAN_ROWS=$(python3 -c "import json;print(json.load(open('$DATA/clean.manifest.json'))['rows'])")
CLEAN_SIZE=$(python3 -c "import json;print(json.load(open('$DATA/clean.manifest.json'))['file_size_bytes'])")

CLEAN_START=$(date +%s.%N)
"$BIN" run --input "$DATA/clean.csv" --import-name "$CLEAN_IMPORT" --chunk-size 500
CLEAN_END=$(date +%s.%N)
CLEAN_RUNTIME=$(python3 -c "print(round($CLEAN_END - $CLEAN_START, 3))")

CLEAN_VERIFY=$("$BIN" verify --input "$DATA/clean.csv")
CLEAN_STATUS=$(psql_scalar "SELECT e.status FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = 'evidence_clean_run' ORDER BY e.attempt DESC LIMIT 1;")
CLEAN_COMMIT_COUNT=$(psql_scalar "SELECT s.commit_count FROM oxide_batch.ob_step_execution s JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = 'evidence_clean_run' ORDER BY e.attempt DESC LIMIT 1;")
CLEAN_DIGEST=$(python3 -c "import json;print(json.loads('''$CLEAN_VERIFY''')['canonical_digest_sha256'])")
CLEAN_DBROWS=$(python3 -c "import json;print(json.loads('''$CLEAN_VERIFY''')['db_row_count'])")

cat > "$OUT/clean-run.json" <<EOF
{
  "scenario": "clean_run",
  "dataset": {"rows": $CLEAN_ROWS, "seed": 20260831, "id_offset": 1, "file_size_bytes": $CLEAN_SIZE, "sha256": "$CLEAN_SHA256"},
  "chunk_size": 500,
  "import_name": "$CLEAN_IMPORT",
  "job_execution_status": "$CLEAN_STATUS",
  "chunks_committed": $CLEAN_COMMIT_COUNT,
  "db_row_count": $CLEAN_DBROWS,
  "final_state_digest_sha256": "$CLEAN_DIGEST",
  "runtime_seconds": $CLEAN_RUNTIME
}
EOF
echo "wrote $OUT/clean-run.json"

# ---------------------------------------------------------------- crash --
CRASH_IMPORT="evidence_crash_run"
"$BIN" generate --output "$DATA/crash.csv" --profile normal --seed 20260831 --id-offset 2000000
CRASH_SHA256=$(python3 -c "import json;print(json.load(open('$DATA/crash.manifest.json'))['sha256'])")
CRASH_ROWS=$(python3 -c "import json;print(json.load(open('$DATA/crash.manifest.json'))['rows'])")

set +e
"$BIN" run --input "$DATA/crash.csv" --import-name "$CRASH_IMPORT" --chunk-size 500 \
  --fail-at chunk:50 --failure-mode after-business-commit --hard-crash
CRASH_EXIT=$?
set -e

CRASH_STATUS=$(psql_scalar "SELECT e.status FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = 'evidence_crash_run' ORDER BY e.attempt DESC LIMIT 1;")
CRASH_EXIT_CODE=$(psql_scalar "SELECT e.exit_code FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = 'evidence_crash_run' ORDER BY e.attempt DESC LIMIT 1;")
CRASH_DBROWS=$(psql_scalar "SELECT COUNT(*) FROM app_business.imported_customer WHERE customer_id > 2000000 AND customer_id <= 2000000 + $CRASH_ROWS;")

cat > "$OUT/crash-run.json" <<EOF
{
  "scenario": "crash_run",
  "dataset": {"rows": $CRASH_ROWS, "seed": 20260831, "id_offset": 2000000, "sha256": "$CRASH_SHA256"},
  "chunk_size": 500,
  "import_name": "$CRASH_IMPORT",
  "failure_injection": {"fail_at": "chunk:50", "failure_mode": "after-business-commit", "hard_crash": true},
  "process_exit_code": $CRASH_EXIT,
  "process_terminated_by_signal": $([ "$CRASH_EXIT" -gt 128 ] && echo "$((CRASH_EXIT - 128))" || echo null),
  "job_execution_status_after_crash": "$CRASH_STATUS",
  "job_execution_exit_code_after_crash": "$CRASH_EXIT_CODE",
  "business_db_rows_after_crash": $CRASH_DBROWS,
  "expected_rows_from_50_committed_chunks": 25000
}
EOF
echo "wrote $OUT/crash-run.json"

# -------------------------------------------------------------- restart --
"$BIN" recover --import-name "$CRASH_IMPORT" --input "$DATA/crash.csv"

RESTART_START=$(date +%s.%N)
RESTART_STDERR="$DATA/restart.stderr"
"$BIN" run --input "$DATA/crash.csv" --import-name "$CRASH_IMPORT" --chunk-size 500 2>"$RESTART_STDERR"
cat "$RESTART_STDERR" >&2
RESTART_END=$(date +%s.%N)
RESTART_RUNTIME=$(python3 -c "print(round($RESTART_END - $RESTART_START, 3))")

# Two different, deliberately distinguished counts (spec ss24, Claim A vs
# Claim B): the DB's ob_step_execution row is the *cumulative* total across
# the whole job instance's lineage (inherited progress + this attempt), while
# this restart attempt's own tracing output ("chunk evidence") reports only
# what *this attempt itself* newly read/committed -- i.e. attempts vs.
# final state are not conflated here.
RESTART_STATUS=$(psql_scalar "SELECT e.status FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = 'evidence_crash_run' ORDER BY e.attempt DESC LIMIT 1;")
CUMULATIVE_COMMIT_COUNT=$(psql_scalar "SELECT s.commit_count FROM oxide_batch.ob_step_execution s JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = 'evidence_crash_run' ORDER BY e.attempt DESC LIMIT 1;")
CUMULATIVE_READ_COUNT=$(psql_scalar "SELECT s.read_count FROM oxide_batch.ob_step_execution s JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = 'evidence_crash_run' ORDER BY e.attempt DESC LIMIT 1;")
THIS_ATTEMPT_READ=$(grep -o 'committed_read=[0-9]*' "$RESTART_STDERR" | tail -1 | cut -d= -f2)
THIS_ATTEMPT_WRITTEN=$(grep -o 'committed_written=[0-9]*' "$RESTART_STDERR" | tail -1 | cut -d= -f2)
RESTART_DBROWS=$(psql_scalar "SELECT COUNT(*) FROM app_business.imported_customer WHERE customer_id > 2000000 AND customer_id <= 2000000 + $CRASH_ROWS;")

# Content-only (offset/email-independent) digest for clean-vs-recovered
# comparison: recompute the clean run's own content digest at its offset
# and this run's, both excluding customer_id/email (see tests/support).
CLEAN_CONTENT_DIGEST=$(psql "$DATABASE_URL" -t -A -c "
  SELECT md5(string_agg(name || amount::text || created_at::text, '' ORDER BY customer_id))
  FROM app_business.imported_customer WHERE customer_id > 1 AND customer_id <= 1 + $CLEAN_ROWS;")
RESTART_CONTENT_DIGEST=$(psql "$DATABASE_URL" -t -A -c "
  SELECT md5(string_agg(name || amount::text || created_at::text, '' ORDER BY customer_id))
  FROM app_business.imported_customer WHERE customer_id > 2000000 AND customer_id <= 2000000 + $CRASH_ROWS;")

cat > "$OUT/restart-run.json" <<EOF
{
  "scenario": "restart_run",
  "import_name": "$CRASH_IMPORT",
  "recovery": "RecoveryRequest::mark_failed via 'recover' subcommand",
  "restart_job_execution_status": "$RESTART_STATUS",
  "this_attempt_only": {
    "committed_read": $THIS_ATTEMPT_READ,
    "committed_written": $THIS_ATTEMPT_WRITTEN,
    "note": "rows this restart attempt itself newly processed (50 remaining chunks), not the instance total"
  },
  "cumulative_after_restart": {
    "read_count": $CUMULATIVE_READ_COUNT,
    "commit_count": $CUMULATIVE_COMMIT_COUNT,
    "note": "ob_step_execution's persisted total across the whole job instance's lineage (inherited + this attempt)"
  },
  "final_business_db_rows": $RESTART_DBROWS,
  "expected_final_rows": $CRASH_ROWS,
  "runtime_seconds": $RESTART_RUNTIME,
  "clean_run_content_digest_md5": "$CLEAN_CONTENT_DIGEST",
  "recovered_run_content_digest_md5": "$RESTART_CONTENT_DIGEST",
  "content_digests_match": $([ "$CLEAN_CONTENT_DIGEST" = "$RESTART_CONTENT_DIGEST" ] && echo true || echo false)
}
EOF
echo "wrote $OUT/restart-run.json"

rm -rf "$DATA"
