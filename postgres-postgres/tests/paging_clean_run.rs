//! Clean paging-mode execution, end to end: seed -> run (`--reader paging`,
//! through the real production `JobLauncher`/`ChunkJob` path and the
//! released `postgres_paging_reader`) -> independently verify. The paging
//! counterpart of `tests/clean_run.rs`'s cursor proof. Programmatic,
//! DB-query-based evidence only -- row counts, correct transformed values
//! (via the independent verifier), and job/step completion status. Never
//! asserts on log strings.

mod support;

use support::SeedOptions;

#[tokio::test]
async fn clean_paging_run_transforms_every_row_exactly_once_with_correct_values() {
    support::migrate();
    // run/verify cover the entire app_source table (see
    // tests/support/mod.rs::reset's doc comment); reset first so this
    // test's exact row-count assertions are not disturbed by residual data
    // from other tests sharing this database.
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: 500,
        seed: 43,
    });
    let import_name = support::unique_name("paging_clean_run");

    let run_output = support::run_paging(&import_name, 50);
    let stdout = String::from_utf8_lossy(&run_output.stdout);
    let _ = stdout; // production run intentionally logs to stderr only

    let pool = support::pool().await;
    let (status, exit_code) = support::latest_execution_status(&pool, &import_name)
        .await
        .expect("job execution recorded");
    assert_eq!(status, "COMPLETED");
    assert_eq!(exit_code, "COMPLETED");

    // Real correctness proof, not a spot check: `verify` independently
    // re-derives the expected transformed row from the source table (its
    // own hand-written oracle, not the production processor -- see
    // src/verify.rs) and merge-compares it against every destination row
    // scoped to this import's exact source identity, field by field, plus
    // two independently accumulated content digests. Fails closed
    // (nonzero exit) on any mismatch.
    let verify_output = support::verify(&import_name);
    assert!(
        verify_output.status.success(),
        "verify must succeed against an untouched clean paging run: stdout={}\nstderr={}",
        String::from_utf8_lossy(&verify_output.stdout),
        String::from_utf8_lossy(&verify_output.stderr),
    );

    let report: serde_json::Value = serde_json::from_slice(&verify_output.stdout)
        .expect("verify prints a JSON report to stdout");
    assert_eq!(report["source_rows"].as_u64(), Some(dataset.rows));
    assert_eq!(report["destination_rows"].as_u64(), Some(dataset.rows));
    assert_eq!(report["row_counts_match"].as_bool(), Some(true));
    assert_eq!(report["digests_match"].as_bool(), Some(true));
    assert!(report["mismatches"]
        .as_array()
        .expect("mismatches is an array")
        .is_empty());
}

/// Paging counterpart of `clean_run.rs`'s connection-sanity check: the
/// paging reader never holds a transaction (each page is an independent
/// statement over its own pool -- see the released `PostgresPagingReader`'s
/// own contract), so this run must leave no leaked idle-in-transaction
/// session either, exactly like cursor mode.
#[tokio::test]
async fn clean_paging_run_leaves_no_open_transaction_or_extra_connections() {
    support::migrate();
    support::seed(SeedOptions { rows: 100, seed: 9 });
    let import_name = support::unique_name("paging_conn_sanity");
    support::run_paging(&import_name, 25);

    let pool = support::pool().await;
    let idle_in_transaction: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM pg_stat_activity \
         WHERE pid <> pg_backend_pid() AND state = 'idle in transaction'",
    )
    .fetch_one(&pool)
    .await
    .unwrap_or(-1);
    assert_eq!(
        idle_in_transaction, 0,
        "no leaked idle-in-transaction session from the exited binary"
    );
}
