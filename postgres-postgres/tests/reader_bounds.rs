//! Reader proof obligations (spec: "Reader").
//!
//! - cursor mode reads all rows exactly once on a clean run;
//! - the configured ordering (`customer_id`) is a valid strict total order
//!   (enforced by the real `BIGINT NOT NULL PRIMARY KEY` constraint, so
//!   duplicate/NULL keys are impossible by construction -- judged by the
//!   database, not application code alone);
//! - a dataset materially larger than `--fetch-size` still lands completely
//!   and correctly.
//!
//! # What the fetch-size test below does and does not directly observe
//!
//! `a_dataset_larger_than_the_configured_fetch_size_lands_completely` proves
//! *correctness* across a dataset requiring multiple `fetch_size`-sized
//! batches; it does not, by itself, directly observe the individual
//! `FETCH FORWARD <fetch_size> FROM oxide_batch_cursor` round trips this
//! workload's own `--fetch-size` configuration produces. Two attempts at
//! that direct observation were tried and rejected during this PR's review:
//! `pg_stat_statements`' per-statement `calls` counter undercounts here
//! (confirmed empirically: 3 real, distinct `FETCH` executions against the
//! same open cursor recorded as `calls = 1`, apparently because `sqlx`
//! reuses one server-side prepared statement across them) -- and
//! server-side statement logging counted the same 3 executions correctly,
//! but reading it back would mean this test shelling out to `docker compose
//! logs` and parsing marker-delimited log output, a materially more
//! fragile dependency than this workload's tests otherwise take on.
//!
//! What this test's passing result *does* rest on: `oxide_batch`
//! `postgres_cursor_reader`'s own source at the exact pinned `v0.6.0`
//! release (`crates/oxide-batch/src/item_components/postgres_cursor.rs`)
//! unconditionally issues `FETCH FORWARD <fetch_size> FROM
//! oxide_batch_cursor` in a loop, stopping only once a batch comes back
//! empty -- a structural fact about the exact dependency version this
//! workload consumes, not a runtime inference that could silently drift --
//! and upstream's own `postgres_item_components_cursor.rs` test already
//! exercises that loop directly within the framework's own repository. This
//! workload's job is to prove *this workload* wires the configured bound
//! through to a correct result, not to re-measure a already-tested
//! framework internal.

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
/// lands completely and correctly. See the module documentation above for
/// exactly what this does and does not directly observe about the
/// underlying `FETCH` round trips.
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
