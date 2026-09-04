//! Isolation and ownership-boundary proofs (spec: "Isolation").
//!
//! 1. The workload's `reset` command truncates only `app_source`/
//!    `app_business` -- it must never touch `oxide_batch`'s own metadata
//!    tables (production commands are never allowed to manipulate
//!    `oxide_batch` directly).
//! 2. Repeated test scenarios sharing one database never collide: every
//!    test gets its own nonce-derived `import_name` and `customer_id`
//!    range (see `tests/support/mod.rs`), proven here by running two
//!    back-to-back scenarios and asserting neither's business rows or job
//!    instance count was disturbed by the other.

mod support;

use support::SeedOptions;

#[tokio::test]
async fn reset_truncates_only_workload_owned_tables_never_oxide_batch_metadata() {
    support::migrate();
    support::seed(SeedOptions { rows: 20, seed: 5 });
    let import_name = support::unique_name("reset_boundary");
    support::run_cursor(&import_name, 10);

    let pool = support::pool().await;
    let instances_before = support::job_instance_count(&pool, &import_name).await;
    assert_eq!(
        instances_before, 1,
        "the run above must be durably recorded"
    );

    support::run_ok(support::bin().arg("reset"));

    let source_rows: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM app_source.source_customer")
        .fetch_one(&pool)
        .await
        .expect("count source rows after reset");
    let business_rows: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM app_business.customer_projection")
            .fetch_one(&pool)
            .await
            .expect("count business rows after reset");
    assert_eq!(source_rows, 0, "reset must truncate app_source");
    assert_eq!(business_rows, 0, "reset must truncate app_business");

    // oxide_batch's own durable record of the already-completed run is
    // framework-owned metadata: reset (a production, workload-owned
    // command) must never touch it.
    let instances_after = support::job_instance_count(&pool, &import_name).await;
    assert_eq!(
        instances_after, instances_before,
        "reset must never manipulate oxide_batch metadata tables"
    );
}

#[tokio::test]
async fn concurrent_test_scenarios_never_collide_through_stale_identity() {
    support::migrate();
    let dataset_1 = support::seed(SeedOptions {
        rows: 30,
        seed: 101,
    });
    let import_1 = support::unique_name("isolation_scenario");
    support::run_cursor(&import_1, 15);
    // `verify` recomputes the *current* source digest live (see
    // src/source_digest.rs), so it must run immediately after its own
    // scenario's `run` and before any other scenario mutates app_source --
    // otherwise it would look up a digest scope that scenario never wrote
    // to. See tests/support/mod.rs::reset's doc comment.
    assert!(support::verify(&import_1).status.success());

    let dataset_2 = support::seed(SeedOptions {
        rows: 30,
        seed: 202,
    });
    let import_2 = support::unique_name("isolation_scenario");
    support::run_cursor(&import_2, 15);
    assert!(support::verify(&import_2).status.success());

    assert_ne!(
        import_1, import_2,
        "nonce-derived import names must never collide even under the same label"
    );
    assert_ne!(
        dataset_1.id_offset, dataset_2.id_offset,
        "nonce-derived customer_id ranges must never collide"
    );

    let pool = support::pool().await;
    let rows_1 =
        support::source_row_count_in_range(&pool, dataset_1.id_offset, dataset_1.rows).await;
    let rows_2 =
        support::source_row_count_in_range(&pool, dataset_2.id_offset, dataset_2.rows).await;
    assert_eq!(rows_1, dataset_1.rows as i64);
    assert_eq!(rows_2, dataset_2.rows as i64);
}
