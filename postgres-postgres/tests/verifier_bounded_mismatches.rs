//! Campaign #63 PR 4 / proof obligation P7: `verify` must keep row reads
//! streaming and its retained mismatch *examples* bounded even when many
//! rows are corrupt, while still reporting the true total mismatch count and
//! failing closed on it. This corrupts more destination rows than
//! `verify::MAX_RETAINED_MISMATCHES` (a private constant; this test asserts
//! on the printed report's public JSON contract instead) and checks that:
//!
//! - `total_mismatches` equals the exact number of rows corrupted (correct
//!   total accounting, not merely "some failure was detected");
//! - `mismatches` (the retained diagnostic examples) is strictly smaller than
//!   `total_mismatches`, proving truncation actually happened rather than
//!   every mismatch being silently retained anyway;
//! - `mismatches_truncated` is `true`;
//! - `verify` still exits nonzero (fail-closed is preserved under
//!   truncation, not weakened into an aggregate-only check).

mod support;

use support::SeedOptions;

/// One more than the production `MAX_RETAINED_MISMATCHES` bound (100, see
/// `src/verify.rs`), so this test proves truncation without hard-coding the
/// production constant's exact value into two places.
const ROWS: u64 = 150;
const CORRUPTED: u64 = 120;

#[tokio::test]
async fn verify_bounds_retained_mismatch_examples_but_reports_the_true_total() {
    support::migrate();
    let dataset = support::seed(SeedOptions {
        rows: ROWS,
        seed: 4242,
    });
    let import_name = support::unique_name("bounded_mismatches");
    support::run_cursor(&import_name, 50);

    let clean = support::verify(&import_name);
    assert!(
        clean.status.success(),
        "precondition failed: clean run did not verify: {}",
        String::from_utf8_lossy(&clean.stderr)
    );

    let pool = support::pool().await;
    // Corrupt CORRUPTED distinct destination rows (a contiguous customer_id
    // range starting right after id_offset), each in a way `compare` in
    // `src/verify.rs` actually detects (display_name mismatch).
    let low = (dataset.id_offset + 1) as i64;
    let high = (dataset.id_offset + CORRUPTED) as i64;
    sqlx::query(
        "UPDATE app_business.customer_projection SET display_name = 'CORRUPTED-BY-TEST' \
         WHERE customer_id >= $1 AND customer_id <= $2",
    )
    .bind(low)
    .bind(high)
    .execute(&pool)
    .await
    .expect("corrupt a bounded range of destination rows directly in the database");

    let output = support::verify(&import_name);
    assert!(
        !output.status.success(),
        "verify must still fail closed when many rows are corrupt, not just when few are"
    );
    let report: serde_json::Value = serde_json::from_slice(&output.stdout)
        .expect("verify still prints a JSON report on failure");

    let total_mismatches = report["total_mismatches"]
        .as_u64()
        .expect("total_mismatches must be present");
    assert_eq!(
        total_mismatches, CORRUPTED,
        "total_mismatches must equal the exact number of rows corrupted, not an approximation"
    );

    let retained = report["mismatches"]
        .as_array()
        .expect("mismatches must be an array");
    assert!(
        (retained.len() as u64) < total_mismatches,
        "retained mismatch examples ({}) must be strictly bounded below the true total ({}) -- \
         otherwise this test's CORRUPTED count no longer exceeds the production bound",
        retained.len(),
        total_mismatches
    );
    assert!(
        !retained.is_empty(),
        "bounded retention must still keep some diagnostic examples, not zero"
    );

    assert_eq!(
        report["mismatches_truncated"].as_bool(),
        Some(true),
        "mismatches_truncated must be true once the retained count falls below the true total"
    );

    // Every retained example must itself be a real, distinct corrupted row
    // (not padding/placeholder entries) within the corrupted id range.
    let mut seen_ids = std::collections::HashSet::new();
    for entry in retained {
        let customer_id = entry["customer_id"].as_i64().expect("customer_id present");
        assert!(
            (low..=high).contains(&customer_id),
            "retained mismatch customer_id {customer_id} must be inside the corrupted range"
        );
        assert!(
            entry["reason"].as_str().unwrap().contains("display_name"),
            "retained mismatch reason must identify the actual corrupted field"
        );
        assert!(
            seen_ids.insert(customer_id),
            "retained mismatch examples must not repeat the same customer_id"
        );
    }
}
