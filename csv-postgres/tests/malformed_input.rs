//! T2: malformed input. Spec ss28 mandates fail-fast, no silent skip: an
//! invalid row must produce an explicit job failure with the containing
//! chunk rolled back, never a partially-imported result.

mod support;

use support::{business_row_count_in_range, latest_execution_status, GenerateOptions};

#[tokio::test]
async fn malformed_field_count_row_fails_the_job_and_rolls_back_its_chunk() {
    support::migrate();
    // chunk_size >= rows, so the whole file is one chunk: any commit at all
    // would show up as a nonzero row count.
    let dataset = support::generate(GenerateOptions {
        rows: 200,
        malformed_at: Some(50),
        label: "malformed-field-count",
        ..Default::default()
    });
    let import_name = support::unique_name("malformed_field_count");

    let output = support::bin()
        .arg("run")
        .arg("--input")
        .arg(&dataset.path)
        .arg("--import-name")
        .arg(&import_name)
        .arg("--chunk-size")
        .arg("500")
        .output()
        .expect("spawn csv-postgres");

    let pool = support::pool().await;
    let (status, _exit_code) = support::latest_execution_status(&pool, &import_name)
        .await
        .expect("job execution recorded even on failure");
    assert_eq!(
        status, "FAILED",
        "a malformed row must fail the job, never be silently skipped"
    );

    let db_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        db_rows, 0,
        "the whole (single) chunk containing the malformed row must roll back"
    );

    // The CLI process itself is allowed to exit success or failure depending
    // on how job status maps to process exit code -- the load-bearing
    // assertion is the durable job/DB state above, not the process exit code.
    let _ = output.status;
}

#[tokio::test]
async fn malformed_amount_row_fails_the_job_and_rolls_back_its_chunk() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 200,
        bad_amount_at: Some(50),
        label: "malformed-amount",
        ..Default::default()
    });
    let import_name = support::unique_name("malformed_amount");

    support::bin()
        .arg("run")
        .arg("--input")
        .arg(&dataset.path)
        .arg("--import-name")
        .arg(&import_name)
        .arg("--chunk-size")
        .arg("500")
        .output()
        .expect("spawn csv-postgres");

    let pool = support::pool().await;
    let (status, _) = latest_execution_status(&pool, &import_name)
        .await
        .expect("job execution recorded");
    assert_eq!(status, "FAILED");

    let db_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        db_rows, 0,
        "a non-numeric amount must fail validation before any write"
    );
}
