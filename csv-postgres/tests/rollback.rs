//! T3 + T7: a real PostgreSQL PRIMARY KEY violation (duplicate business key,
//! ss12) is the DB failure under test. Two policies, deliberately not
//! blended (ss26): strict insert (no `ON CONFLICT`) must fail the whole
//! chunk transaction closed; the idempotent (`ON CONFLICT DO NOTHING`)
//! variant must complete with the duplicate silently absorbed and the
//! correct final row count, not `rows + 1`.

mod support;

use support::{business_row_count_in_range, latest_execution_status, GenerateOptions};

#[tokio::test]
async fn strict_insert_rolls_back_the_whole_chunk_on_duplicate_business_key() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 200,
        duplicate_at: Some(50),
        label: "dup-strict",
        ..Default::default()
    });
    let import_name = support::unique_name("dup_strict");

    support::bin()
        .arg("run")
        .arg("--input")
        .arg(&dataset.path)
        .arg("--import-name")
        .arg(&import_name)
        .arg("--chunk-size")
        .arg("500") // whole file is one chunk
        .output()
        .expect("spawn csv-postgres");

    let pool = support::pool().await;
    let (status, _) = latest_execution_status(&pool, &import_name)
        .await
        .expect("job execution recorded");
    assert_eq!(
        status, "FAILED",
        "a real PRIMARY KEY violation must fail the job"
    );

    let db_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        db_rows, 0,
        "strict insert: the whole chunk transaction rolls back, not just the duplicate row"
    );
}

#[tokio::test]
async fn idempotent_insert_absorbs_the_duplicate_and_completes_with_the_correct_count() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 200,
        duplicate_at: Some(50),
        label: "dup-idempotent",
        ..Default::default()
    });
    let import_name = support::unique_name("dup_idempotent");

    support::run_ok(
        support::bin()
            .arg("run")
            .arg("--input")
            .arg(&dataset.path)
            .arg("--import-name")
            .arg(&import_name)
            .arg("--chunk-size")
            .arg("500")
            .arg("--idempotent-writes"),
    );

    let pool = support::pool().await;
    let (status, _) = latest_execution_status(&pool, &import_name)
        .await
        .expect("job execution recorded");
    assert_eq!(
        status, "COMPLETED",
        "ON CONFLICT DO NOTHING absorbs the duplicate rather than failing"
    );

    let db_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        db_rows, dataset.rows as i64,
        "final state has no duplicate: exactly `rows` distinct customer_ids, not rows + 1"
    );
}
