//! Negative controls for `verify`: if it were ever weakened into something
//! that can't actually detect wrong content (e.g. counting rows without
//! comparing values), these must start failing. Each test corrupts
//! already-imported destination state directly in the database (bypassing
//! the application entirely) and asserts `verify` rejects it.

mod support;

use support::SeedOptions;

async fn clean_dataset(label: &str, rows: u64) -> (support::Dataset, String, sqlx::PgPool) {
    support::migrate();
    let dataset = support::seed(SeedOptions { rows, seed: 99 });
    let import_name = support::unique_name(label);
    support::run(&import_name, 50);

    // A clean, unmutated run must verify successfully first, so a failure
    // below is attributable to the corruption, not to some other
    // pre-existing problem.
    let clean = support::verify(&import_name);
    assert!(
        clean.status.success(),
        "precondition failed: clean run did not verify: {}",
        String::from_utf8_lossy(&clean.stderr)
    );

    let pool = support::pool().await;
    (dataset, import_name, pool)
}

#[tokio::test]
async fn verify_fails_closed_when_a_destination_value_is_corrupted() {
    let (dataset, import_name, pool) = clean_dataset("corrupt_value", 50).await;

    let corrupted_customer_id = (dataset.id_offset + 1) as i64;
    sqlx::query(
        "UPDATE app_business.customer_projection SET display_name = 'CORRUPTED-BY-TEST' \
         WHERE customer_id = $1",
    )
    .bind(corrupted_customer_id)
    .execute(&pool)
    .await
    .expect("corrupt one destination row directly in the database");

    let output = support::verify(&import_name);
    assert!(
        !output.status.success(),
        "verify must fail closed against a corrupted destination value"
    );
    let report: serde_json::Value = serde_json::from_slice(&output.stdout)
        .expect("verify still prints a JSON report on failure");
    let mismatches = report["mismatches"].as_array().expect("mismatches array");
    assert_eq!(mismatches.len(), 1);
    assert_eq!(
        mismatches[0]["customer_id"].as_i64(),
        Some(corrupted_customer_id)
    );
    assert!(mismatches[0]["reason"]
        .as_str()
        .unwrap()
        .contains("display_name"));
}

#[tokio::test]
async fn verify_fails_closed_when_a_destination_row_is_missing() {
    let (dataset, import_name, pool) = clean_dataset("missing_row", 50).await;

    let missing_customer_id = (dataset.id_offset + 1) as i64;
    sqlx::query("DELETE FROM app_business.customer_projection WHERE customer_id = $1")
        .bind(missing_customer_id)
        .execute(&pool)
        .await
        .expect("delete one destination row directly in the database");

    let output = support::verify(&import_name);
    assert!(
        !output.status.success(),
        "verify must fail closed when a destination row is missing"
    );
    let report: serde_json::Value = serde_json::from_slice(&output.stdout)
        .expect("verify still prints a JSON report on failure");
    let mismatches = report["mismatches"].as_array().expect("mismatches array");
    assert_eq!(mismatches.len(), 1);
    assert_eq!(
        mismatches[0]["customer_id"].as_i64(),
        Some(missing_customer_id)
    );
    assert!(mismatches[0]["reason"]
        .as_str()
        .unwrap()
        .contains("missing destination row"));
}

#[tokio::test]
async fn verify_fails_closed_when_an_unexpected_destination_row_exists() {
    let (dataset, import_name, pool) = clean_dataset("extra_row", 50).await;

    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest for the extra row's scope");
    let extra_customer_id = (dataset.id_offset + dataset.rows + 1) as i64;
    sqlx::query(
        "INSERT INTO app_business.customer_projection \
         (import_name, source_digest, customer_id, display_name, loyalty_score, is_premium, row_fingerprint) \
         VALUES ($1, $2, $3, 'EXTRA-BY-TEST', 0, false, '\\x00')",
    )
    .bind(&import_name)
    .bind(&source_digest)
    .bind(extra_customer_id)
    .execute(&pool)
    .await
    .expect("insert an unexpected destination row directly in the database");

    let output = support::verify(&import_name);
    assert!(
        !output.status.success(),
        "verify must fail closed against an unexpected destination row with no matching source row"
    );
    let report: serde_json::Value = serde_json::from_slice(&output.stdout)
        .expect("verify still prints a JSON report on failure");
    let mismatches = report["mismatches"].as_array().expect("mismatches array");
    assert_eq!(mismatches.len(), 1);
    assert_eq!(
        mismatches[0]["customer_id"].as_i64(),
        Some(extra_customer_id)
    );
    assert!(mismatches[0]["reason"]
        .as_str()
        .unwrap()
        .contains("unexpected destination row"));
}
