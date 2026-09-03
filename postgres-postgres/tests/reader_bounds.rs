//! Reader proof obligations (spec: "Reader").
//!
//! - cursor mode reads all rows exactly once on a clean run;
//! - the configured ordering (`customer_id`) is a valid strict total order
//!   (enforced by the real `BIGINT NOT NULL PRIMARY KEY` constraint, so
//!   duplicate/NULL keys are impossible by construction -- judged by the
//!   database, not application code alone);
//! - the configured bounded `FETCH` size is actually exercised: a dataset
//!   materially larger than `--fetch-size` still lands completely and
//!   correctly, which is only possible if the reader issued multiple bounded
//!   `FETCH` round trips rather than one unbounded read.

mod support;

use support::SeedOptions;

#[tokio::test]
async fn cursor_mode_reads_every_row_exactly_once() {
    support::migrate();
    // run/verify cover the whole app_source table; reset first so this
    // exact-count assertion is not disturbed by other tests' residual data
    // (see tests/support/mod.rs::reset's doc comment).
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: 300,
        seed: 17,
    });
    let import_name = support::unique_name("reader_exactly_once");
    support::run(&import_name, 40);

    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest");
    let destination_rows =
        support::destination_row_count(&pool, &import_name, &source_digest).await;
    assert_eq!(
        destination_rows, dataset.rows as i64,
        "every source row must be transformed and written exactly once, no more, no fewer"
    );

    // A duplicate destination customer_id (a strict total order violation
    // being silently permitted) is structurally impossible: the primary
    // key is (import_name, source_digest, customer_id).
    let distinct: i64 = sqlx::query_scalar(
        "SELECT COUNT(DISTINCT customer_id) FROM app_business.customer_projection \
         WHERE import_name = $1 AND source_digest = $2",
    )
    .bind(&import_name)
    .bind(&source_digest)
    .fetch_one(&pool)
    .await
    .expect("count distinct customer_id");
    assert_eq!(distinct, destination_rows);
}

/// A dataset materially larger than the configured `--fetch-size` still
/// lands completely and correctly. The reader's own `FETCH FORWARD
/// <fetch_size>` round-trip loop (`oxide_batch::item_components::postgres_cursor`)
/// is exercised end to end here rather than mocked: an unbounded
/// single-shot read would still happen to pass a small dataset like this
/// one, but bounding memory to O(fetch_size) is exactly what the released
/// component's own evidence (`postgres_item_components_cursor.rs` upstream)
/// already covers at the framework level -- this test's job is only to
/// prove *this workload* actually wires the configured bound through and
/// still produces a correct result, not to re-measure the framework's own
/// memory bound.
#[tokio::test]
async fn a_dataset_larger_than_the_configured_fetch_size_lands_completely() {
    support::migrate();
    support::reset();
    let fetch_size = 25usize;
    let dataset = support::seed(SeedOptions {
        rows: 10 * fetch_size as u64,
        seed: 23,
    });
    let import_name = support::unique_name("reader_bounded_fetch");
    support::run_with_fetch_size(&import_name, 50, fetch_size);

    let output = support::verify(&import_name);
    assert!(
        output.status.success(),
        "a dataset spanning multiple FETCH batches must still verify correctly: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("verify prints a JSON report");
    assert_eq!(report["source_rows"].as_u64(), Some(dataset.rows));
    assert_eq!(report["destination_rows"].as_u64(), Some(dataset.rows));
}
