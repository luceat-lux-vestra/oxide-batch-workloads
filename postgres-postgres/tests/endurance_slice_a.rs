//! Campaign #203 Slice A: deterministic planning and destination-only cleanup.

mod support;

use std::process::Command;

use postgres_postgres::endurance::{cleanup_business, plan_iteration};
use support::SeedOptions;

#[test]
fn endurancectl_plan_is_machine_readable_and_deterministic() {
    let output = Command::new(env!("CARGO_BIN_EXE_postgres-postgres-endurancectl"))
        .args([
            "plan",
            "--campaign-id",
            "s10-baseline",
            "--sequence",
            "24",
        ])
        .output()
        .expect("spawn postgres-postgres-endurancectl");

    assert!(
        output.status.success(),
        "plan command failed: stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );

    let plan: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("plan command prints JSON");
    assert_eq!(plan["campaign_id"], "s10-baseline");
    assert_eq!(plan["sequence"], 24);
    assert_eq!(plan["reader"], "paging");
    assert_eq!(plan["failure"], "before-commit");
    assert_eq!(
        plan["import_name"],
        "endurance_s10-baseline_000024_paging"
    );
}

async fn metadata_counts(pool: &sqlx::PgPool) -> (i64, i64, i64) {
    sqlx::query_as(
        "SELECT            (SELECT COUNT(*)::bigint FROM oxide_batch.ob_job_instance),            (SELECT COUNT(*)::bigint FROM oxide_batch.ob_job_execution),            (SELECT COUNT(*)::bigint FROM oxide_batch.ob_step_execution)",
    )
    .fetch_one(pool)
    .await
    .expect("count OxideBatch metadata rows")
}

async fn source_row_count(pool: &sqlx::PgPool) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*)::bigint FROM app_source.source_customer")
        .fetch_one(pool)
        .await
        .expect("count source rows")
}

#[tokio::test]
async fn cleanup_removes_only_target_business_rows() {
    support::migrate();
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: 137,
        seed: 20260904,
    });

    let campaign_id = format!("test{}", support::nonce());
    let target = plan_iteration(&campaign_id, 1)
        .expect("target plan")
        .import_name;
    let control = plan_iteration(&campaign_id, 2)
        .expect("control plan")
        .import_name;

    support::run_cursor_with_fetch_size(&target, 31, 17);
    support::run_paging_with_page_size(&control, 29, 19);

    let target_verify = support::verify(&target);
    assert!(
        target_verify.status.success(),
        "target run must verify before cleanup: stderr={}",
        String::from_utf8_lossy(&target_verify.stderr)
    );
    let control_verify = support::verify(&control);
    assert!(
        control_verify.status.success(),
        "control run must verify before cleanup: stderr={}",
        String::from_utf8_lossy(&control_verify.stderr)
    );

    let pool = support::pool().await;
    let source_digest_before = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest before cleanup");
    let source_rows_before = source_row_count(&pool).await;
    let metadata_before = metadata_counts(&pool).await;
    let target_metadata_before = support::job_instance_count(&pool, &target).await;

    assert_eq!(source_rows_before, dataset.rows as i64);
    assert_eq!(
        support::destination_row_count(&pool, &target, &source_digest_before).await,
        dataset.rows as i64
    );
    assert_eq!(
        support::destination_row_count(&pool, &control, &source_digest_before).await,
        dataset.rows as i64
    );
    assert!(
        target_metadata_before > 0,
        "target execution must have durable framework metadata before cleanup"
    );

    let deleted = cleanup_business(&support::database_url(), &target)
        .await
        .expect("target-only business cleanup succeeds");
    assert_eq!(deleted, dataset.rows);

    let source_digest_after = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest after cleanup");
    let source_rows_after = source_row_count(&pool).await;
    let metadata_after = metadata_counts(&pool).await;
    let target_metadata_after = support::job_instance_count(&pool, &target).await;

    assert_eq!(source_rows_after, source_rows_before);
    assert_eq!(source_digest_after, source_digest_before);
    assert_eq!(metadata_after, metadata_before);
    assert_eq!(target_metadata_after, target_metadata_before);
    assert_eq!(
        support::destination_row_count(&pool, &target, &source_digest_after).await,
        0
    );
    assert_eq!(
        support::destination_row_count(&pool, &control, &source_digest_after).await,
        dataset.rows as i64
    );

    let control_verify_after = support::verify(&control);
    assert!(
        control_verify_after.status.success(),
        "cleanup must not damage another iteration: stderr={}",
        String::from_utf8_lossy(&control_verify_after.stderr)
    );

    assert_eq!(
        cleanup_business(&support::database_url(), &target)
            .await
            .expect("cleanup is idempotent after target rows are gone"),
        0
    );

    pool.close().await;
}
