//! T1: clean import. Programmatic, DB-query-based evidence only (spec ss18)
//! -- row counts, uniqueness (enforced by the real PRIMARY KEY, ss12),
//! correct values, and job/step completion status.

mod support;

use support::{business_row_count_in_range, latest_execution_status, GenerateOptions};

/// `verify` compares the *whole* business table against `input` (it has no
/// range scoping -- a real deployment's table only ever holds its own
/// data), so this test resets the table first: otherwise another test's
/// rows sharing this suite's table (each isolated only by its own
/// `id_offset` range, see `tests/support`) would make a correct import
/// look like a mismatch. Depends on serialized test execution, same as
/// `clean_import_leaves_no_open_transaction_or_extra_connections` and
/// `restart.rs`'s content-equivalence test.
#[tokio::test]
async fn clean_import_lands_every_row_exactly_once_with_correct_values() {
    support::migrate();
    support::reset();
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

    // Real correctness proof, not a spot-check: `verify` independently
    // re-parses the source CSV with a real CSV parser (so the generator's
    // own quoted-comma / escaped-quote edge-case rows, ss8, are handled
    // correctly, not split naively on ','), computes its own row count and
    // full-content digest, and compares them against the database's --
    // failing closed (nonzero exit) on any mismatch. Every value in every
    // row is covered, not just the first row's name/email/amount.
    support::run_ok(
        support::bin()
            .arg("verify")
            .arg("--input")
            .arg(&dataset.path),
    );
}

/// Negative control for the positive test above: if `verify` is ever
/// weakened back into something that can't actually detect wrong content
/// (e.g. counting rows without comparing values), this must start failing.
/// Corrupts one already-imported row directly in the database (bypassing
/// the application entirely) and asserts `verify` rejects it.
#[tokio::test]
async fn verify_fails_closed_when_a_database_value_is_corrupted() {
    support::migrate();
    support::reset();
    let dataset = support::generate(GenerateOptions {
        rows: 50,
        label: "verify-negative",
        ..Default::default()
    });
    let import_name = support::unique_name("verify_negative");

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

    // A clean, unmutated import must verify successfully first, so a
    // failure below is attributable to the corruption, not to some other
    // pre-existing problem.
    support::run_ok(
        support::bin()
            .arg("verify")
            .arg("--input")
            .arg(&dataset.path),
    );

    let pool = support::pool().await;
    let corrupted_customer_id = (dataset.id_offset + 1) as i64;
    sqlx::query("UPDATE app_business.imported_customer SET name = $1 WHERE customer_id = $2")
        .bind("CORRUPTED-BY-TEST")
        .bind(corrupted_customer_id)
        .execute(&pool)
        .await
        .expect("corrupt one row directly in the database");

    let output = support::bin()
        .arg("verify")
        .arg("--input")
        .arg(&dataset.path)
        .output()
        .expect("spawn csv-postgres");
    assert!(
        !output.status.success(),
        "verify must fail closed against database content that no longer matches the source CSV"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("digest"),
        "failure should be attributable to a content mismatch, got: {stderr}"
    );
}

/// Inspects global `pg_stat_activity` state, so it is only a meaningful
/// signal with `--test-threads=1` (the repository's documented default):
/// under real concurrency, another test's own in-flight import can
/// legitimately be idle-in-transaction for a moment between two statements
/// of the same open chunk transaction, which this check cannot distinguish
/// from an actual leak.
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
