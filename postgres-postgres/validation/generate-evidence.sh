#!/usr/bin/env bash
# Reproduces the campaign #63 PR 4 retained resource-observation evidence in
# this directory: a materially larger-than-CI dataset (see ROWS below, vs.
# ci/validate's 2,000-row smoke dataset), run once in cursor mode and once in
# paging mode, each followed by an independent `verify`, with real
# process-level peak-RSS observation for every one of those four processes.
#
# Requires a running PostgreSQL reachable at $DATABASE_URL (default: the
# docker-compose service on localhost:5434) and produces a release build of
# the postgres-postgres binary. Linux-only: peak RSS is read from
# /proc/<pid>/status's VmHWM (see measure_peak_rss_kib below), which has no
# portable equivalent on non-Linux hosts.
#
# Usage: DATABASE_URL=... ./validation/generate-evidence.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DATABASE_URL="${DATABASE_URL:-postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5434/postgres_postgres_workload}"
export DATABASE_URL
BIN=./target/release/postgres-postgres
OUT="$(pwd)/validation"

# Deterministic dataset parameters. 200,000 rows is 100x ci/validate's
# 2,000-row smoke dataset -- materially larger, not merely 3,000-5,000 -- and
# CHUNK_SIZE/FETCH_SIZE/PAGE_SIZE are all chosen well below ROWS so cursor
# fetches, paging pages, chunks, and writer batches are all exercised
# repeatedly (hundreds of times), not in one or a handful of round trips.
ROWS=200000
SEED=20260904
ID_OFFSET=0
CHUNK_SIZE=1000
FETCH_SIZE=500
PAGE_SIZE=750

psql_scalar() {
  psql "$DATABASE_URL" -t -A -c "$1"
}

now_seconds() {
  date +%s.%N
}

elapsed_since() {
  python3 -c "print(round($(now_seconds) - $1, 3))"
}

# Runs "$@" in the background, polls /proc/<pid>/status's VmHWM (the kernel's
# own monotonically increasing peak-resident-set-size counter, in KiB) every
# 20ms until the process exits, redirecting its stdout/stderr to the two
# file paths given as $1/$2. Sets PEAK_RSS_KIB (the maximum VmHWM observed)
# and PEAK_RSS_EXIT_CODE (the child's real exit code) as globals.
#
# This is a real external, process-level measurement, not a self-reported
# figure from inside the workload binary: the kernel, not this workload,
# computes and maintains VmHWM.
measure_peak_rss_kib() {
  local stdout_file="$1" stderr_file="$2"
  shift 2
  "$@" >"$stdout_file" 2>"$stderr_file" &
  local pid=$!
  local peak=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ -r "/proc/$pid/status" ]; then
      local hwm
      hwm=$(awk '/^VmHWM:/{print $2}' "/proc/$pid/status" 2>/dev/null || true)
      if [ -n "$hwm" ] && [ "$hwm" -gt "$peak" ] 2>/dev/null; then
        peak=$hwm
      fi
    fi
    sleep 0.02
  done
  set +e
  wait "$pid"
  PEAK_RSS_EXIT_CODE=$?
  set -e
  PEAK_RSS_KIB=$peak
}

cargo build --locked --release --quiet

"$BIN" migrate
"$BIN" reset
psql "$DATABASE_URL" -q -c "TRUNCATE oxide_batch.ob_job_execution CASCADE;" \
                      -c "TRUNCATE oxide_batch.ob_job_instance CASCADE;"

"$BIN" seed --rows "$ROWS" --seed "$SEED" --id-offset "$ID_OFFSET"

DATABASE_VERSION=$(psql_scalar "SHOW server_version;")
RUSTC_VERSION=$(rustc --version)
CARGO_VERSION=$(cargo --version)
OS_KERNEL=$(uname -srm)
WRITER_COLUMNS_PER_ROW=7
# These are the pinned oxide-batch 0.6.0 PostgresBatchMode::MultiRowValues
# defaults and the exact shape supplied by src/writer.rs. The released writer
# splits one write() chunk into parameter-bounded sub-batches; chunk_size is
# not itself the size of one INSERT statement.
WRITER_MAX_PARAMETERS_PER_STATEMENT=2000
WRITER_ROWS_PER_STATEMENT=$((WRITER_MAX_PARAMETERS_PER_STATEMENT / WRITER_COLUMNS_PER_ROW))
WRITER_MAX_BOUND_PARAMS_PER_STATEMENT=$((WRITER_ROWS_PER_STATEMENT * WRITER_COLUMNS_PER_ROW))
WRITER_MAX_SUB_BATCHES_PER_CHUNK=$(((CHUNK_SIZE + WRITER_ROWS_PER_STATEMENT - 1) / WRITER_ROWS_PER_STATEMENT))

# $1 reader_mode ("cursor"|"paging")
# $2 import_name
# $3 size_key ("fetch_size"|"page_size" -- the JSON field name; the CLI flag
#    is derived from it by substituting '-' for '_')
# $4 size_value
# $5 out_file
run_scenario() {
  local reader_mode="$1" import_name="$2" size_key="$3" size_value="$4" out_file="$5"
  local size_flag="--${size_key//_/-}"

  local run_stdout run_stderr
  run_stdout="$(mktemp)"
  run_stderr="$(mktemp)"
  local run_start
  run_start=$(now_seconds)
  measure_peak_rss_kib "$run_stdout" "$run_stderr" \
    "$BIN" run --import-name "$import_name" --chunk-size "$CHUNK_SIZE" --reader "$reader_mode" \
    "$size_flag" "$size_value"
  local run_exit=$PEAK_RSS_EXIT_CODE
  local run_peak_rss=$PEAK_RSS_KIB
  local run_runtime
  run_runtime=$(elapsed_since "$run_start")
  if [ "$run_exit" -ne 0 ]; then
    echo "run ($reader_mode) failed:" >&2
    cat "$run_stderr" >&2
    exit 1
  fi

  local job_status commit_count committed_read committed_written
  job_status=$(psql_scalar "SELECT e.status FROM oxide_batch.ob_job_execution e JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$import_name' ORDER BY e.attempt DESC LIMIT 1;")
  commit_count=$(psql_scalar "SELECT s.commit_count FROM oxide_batch.ob_step_execution s JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id WHERE i.job_name = '$import_name' ORDER BY e.attempt DESC LIMIT 1;")
  committed_read=$(grep -o 'committed_read=[0-9]*' "$run_stderr" | tail -1 | cut -d= -f2)
  committed_written=$(grep -o 'committed_written=[0-9]*' "$run_stderr" | tail -1 | cut -d= -f2)

  local verify_stdout verify_stderr
  verify_stdout="$(mktemp)"
  verify_stderr="$(mktemp)"
  local verify_start
  verify_start=$(now_seconds)
  measure_peak_rss_kib "$verify_stdout" "$verify_stderr" \
    "$BIN" verify --import-name "$import_name"
  local verify_exit=$PEAK_RSS_EXIT_CODE
  local verify_peak_rss=$PEAK_RSS_KIB
  local verify_runtime
  verify_runtime=$(elapsed_since "$verify_start")
  if [ "$verify_exit" -ne 0 ]; then
    echo "verify ($reader_mode) failed:" >&2
    cat "$verify_stderr" >&2
    cat "$verify_stdout" >&2
    exit 1
  fi

  REC_SCENARIO="${reader_mode}_bounded_resource_run" \
  REC_READER_MODE="$reader_mode" \
  REC_ROWS="$ROWS" \
  REC_SEED="$SEED" \
  REC_ID_OFFSET="$ID_OFFSET" \
  REC_CHUNK_SIZE="$CHUNK_SIZE" \
  REC_SIZE_KEY="$size_key" \
  REC_SIZE_VALUE="$size_value" \
  REC_IMPORT_NAME="$import_name" \
  REC_COLUMNS_PER_ROW="$WRITER_COLUMNS_PER_ROW" \
  REC_MAX_PARAMETERS="$WRITER_MAX_PARAMETERS_PER_STATEMENT" \
  REC_ROWS_PER_STATEMENT="$WRITER_ROWS_PER_STATEMENT" \
  REC_MAX_BOUND_PARAMS="$WRITER_MAX_BOUND_PARAMS_PER_STATEMENT" \
  REC_MAX_SUB_BATCHES="$WRITER_MAX_SUB_BATCHES_PER_CHUNK" \
  REC_JOB_STATUS="$job_status" \
  REC_COMMIT_COUNT="$commit_count" \
  REC_COMMITTED_READ="$committed_read" \
  REC_COMMITTED_WRITTEN="$committed_written" \
  REC_RUN_PEAK_RSS="$run_peak_rss" \
  REC_RUN_RUNTIME="$run_runtime" \
  REC_VERIFY_PEAK_RSS="$verify_peak_rss" \
  REC_VERIFY_RUNTIME="$verify_runtime" \
  REC_OS_KERNEL="$OS_KERNEL" \
  REC_VERIFY_STDOUT_PATH="$verify_stdout" \
  REC_OUT_FILE="$out_file" \
  python3 <<'PYEOF'
import json
import os

env = os.environ
verify_report = json.load(open(env["REC_VERIFY_STDOUT_PATH"]))

record = {
    "scenario": env["REC_SCENARIO"],
    "reader_mode": env["REC_READER_MODE"],
    "dataset": {
        "rows": int(env["REC_ROWS"]),
        "seed": int(env["REC_SEED"]),
        "id_offset": int(env["REC_ID_OFFSET"]),
        # verify's own JSON report already recomputes and prints this
        # (src/verify.rs's VerifyReport.source_digest), via the same
        # streaming src/source_digest.rs::compute both `run` and `verify`
        # use -- no separate probe pass is needed to obtain it.
        "source_digest_sha256": verify_report["source_digest"],
    },
    "chunk_size": int(env["REC_CHUNK_SIZE"]),
    env["REC_SIZE_KEY"]: int(env["REC_SIZE_VALUE"]),
    "import_name": env["REC_IMPORT_NAME"],
    "writer_config": {
        "mode": "PostgresBatchMode::MultiRowValues",
        "columns_per_row": int(env["REC_COLUMNS_PER_ROW"]),
        "max_parameters_per_statement": int(env["REC_MAX_PARAMETERS"]),
        "rows_per_statement": int(env["REC_ROWS_PER_STATEMENT"]),
        "max_bound_params_per_statement": int(env["REC_MAX_BOUND_PARAMS"]),
        "max_sub_batches_per_chunk": int(env["REC_MAX_SUB_BATCHES"]),
        "note": (
            "oxide-batch 0.6.0's multi_row_values() configures a maximum "
            "of max_parameters_per_statement bound values per INSERT; the "
            "writer derives rows_per_statement by integer division and "
            "sub-batches each write() chunk accordingly"
        ),
    },
    "run": {
        "job_execution_status": env["REC_JOB_STATUS"],
        "chunks_committed": int(env["REC_COMMIT_COUNT"]),
        "committed_read": int(env["REC_COMMITTED_READ"]),
        "committed_written": int(env["REC_COMMITTED_WRITTEN"]),
        "peak_rss_kib": int(env["REC_RUN_PEAK_RSS"]),
        "runtime_seconds": float(env["REC_RUN_RUNTIME"]),
    },
    "verify": {
        "process_exit_code": 0,
        "source_rows": verify_report["source_rows"],
        "destination_rows": verify_report["destination_rows"],
        "row_counts_match": verify_report["row_counts_match"],
        "expected_digest_sha256": verify_report["expected_digest_sha256"],
        "actual_digest_sha256": verify_report["actual_digest_sha256"],
        "digests_match": verify_report["digests_match"],
        "total_mismatches": verify_report["total_mismatches"],
        "mismatches_truncated": verify_report["mismatches_truncated"],
        "peak_rss_kib": int(env["REC_VERIFY_PEAK_RSS"]),
        "runtime_seconds": float(env["REC_VERIFY_RUNTIME"]),
    },
    "resource_measurement": {
        "tool": (
            "generate-evidence.sh's own measure_peak_rss_kib: polls "
            "/proc/<pid>/status VmHWM every 20ms until the process exits"
        ),
        "metric": "VmHWM (kernel-reported peak resident set size across the process's whole lifetime)",
        "units": "KiB",
        "platform": env["REC_OS_KERNEL"],
        "note": (
            "run's own peak RSS includes this run's source_digest "
            "computation (job::run computes it before launching the "
            "reader, in the same process); verify's own peak RSS "
            "likewise includes verify's own independent source_digest "
            "recomputation. Neither figure isolates source-digest "
            "computation into a separate measured process -- see "
            "postgres-postgres/README.md's resource-observation section."
        ),
    },
}

with open(env["REC_OUT_FILE"], "w") as f:
    json.dump(record, f, indent=2, sort_keys=True)
    f.write("\n")
PYEOF

  rm -f "$run_stdout" "$run_stderr" "$verify_stdout" "$verify_stderr"
  echo "wrote $out_file (run peak_rss_kib=$run_peak_rss verify peak_rss_kib=$verify_peak_rss)"
}

run_scenario cursor evidence_cursor_run fetch_size "$FETCH_SIZE" "$OUT/cursor-run.json"
run_scenario paging evidence_paging_run page_size "$PAGE_SIZE" "$OUT/paging-run.json"

cat <<EOF

Producer environment observed during this run:
  database server_version: $DATABASE_VERSION
  rustc:                   $RUSTC_VERSION
  cargo:                   $CARGO_VERSION
  os/kernel:               $OS_KERNEL
  build profile:           release
EOF
