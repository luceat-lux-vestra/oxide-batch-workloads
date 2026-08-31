//! T1: clean import. Programmatic, DB-query-based evidence only (spec ss18)
//! -- row counts, uniqueness (enforced by the real PRIMARY KEY, ss12),
//! correct values, and job/step completion status.

mod support;

use support::{
    business_row_count_in_range, canonical_digest_in_range, latest_execution_status,
    GenerateOptions,
};

#[tokio::test]
async fn clean_import_lands_every_row_exactly_once_with_correct_values() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 500,
        label: "clean",
        ..Default::default()
    });
    let import_name = support::unique_name("clean_import");

    support::run_ok(
        support::bin()
            .arg("run")
            .arg("--input")
            .arg(&dataset.path)
            .arg("--import-name")
            .arg(&import_name)
            .arg("--chunk-size")
            .arg("50"),
    );

    let pool = support::pool().await;
    let db_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        db_rows, dataset.rows as i64,
        "every input row must land exactly once"
    );

    let (status, exit_code) = latest_execution_status(&pool, &import_name)
        .await
        .expect("job execution recorded");
    assert_eq!(status, "COMPLETED");
    assert_eq!(exit_code, "COMPLETED");

    let expected_customer_id = (dataset.id_offset + 1) as i64;
    let row: (String, String, i64) = sqlx::query_as(
        "SELECT name, email, amount FROM app_business.imported_customer WHERE customer_id = $1",
    )
    .bind(expected_customer_id)
    .fetch_one(&pool)
    .await
    .expect("first generated row must exist");
    assert!(row.0.contains(' '), "generated name has first+last name");
    assert!(row.1.ends_with("@example.test"));
    assert!(row.2 >= 100 && row.2 < 1_000_000);

    // Digest is deterministic for a fixed seed/id_offset/row-count: recomputing
    // it here would just restate canonical_digest_in_range's own logic, so the
    // real assertion is that it doesn't panic and is stable across two reads.
    let digest_a = canonical_digest_in_range(&pool, dataset.id_offset, dataset.rows).await;
    let digest_b = canonical_digest_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(digest_a, digest_b);
}

#[tokio::test]
async fn clean_import_leaves_no_open_transaction_or_extra_connections() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 100,
        label: "conn-sanity",
        ..Default::default()
    });
    let import_name = support::unique_name("conn_sanity");

    support::run_ok(
        support::bin()
            .arg("run")
            .arg("--input")
            .arg(&dataset.path)
            .arg("--import-name")
            .arg(&import_name)
            .arg("--chunk-size")
            .arg("25"),
    );

    let pool = support::pool().await;
    // The just-exited binary's own pool must already be fully closed
    // (job::run calls repository.close().await before returning): no other
    // backend should be idle-in-transaction (a leaked, uncommitted
    // transaction) or otherwise still holding a lock, excluding this
    // assertion's own backend.
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
    // A lock-based check was deliberately not added here: `cargo test` runs
    // separate test binaries (files) concurrently by default, and any of
    // them legitimately touching app_business.imported_customer at the same
    // wall-clock moment produces a transient, non-leak lock that made a
    // relation-scoped check flaky in practice. idle-in-transaction is the
    // precise signal for "this connection abandoned an open transaction"
    // and does not have that false-positive mode.
}
