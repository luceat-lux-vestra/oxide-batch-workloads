//! Cursor/paging business parity (campaign #63, PR 2).
//!
//! One identical source dataset, transformed once under cursor mode and
//! once under paging mode (two distinct import names, so their destination
//! scopes never collide -- see `tests/reader_mode_identity.rs` for the
//! identity mechanics), independently verified on both sides, and then
//! compared directly against each other field by field -- not merely "both
//! verifiers passed," which would not by itself prove the two readers
//! produced the *same* result, only that each independently matches its own
//! source-derived expectation (which they should, since both share the
//! exact same `expected_projection` oracle -- see `src/verify.rs`).
//!
//! Every size below (`fetch_size`, `page_size`, and both runs' `chunk_size`)
//! and the dataset row count are deliberately non-multiples of each other
//! (733 is prime), so a boundary coincidence -- e.g. a page/fetch size that
//! happens to evenly divide the chunk size or the dataset -- cannot mask an
//! off-by-one or duplicate/drop defect at a page, fetch, or chunk boundary.

mod support;

use support::SeedOptions;

#[tokio::test]
async fn cursor_and_paging_produce_field_for_field_identical_projections() {
    support::migrate();
    // Both runs below must observe the exact same source content (and
    // therefore the exact same source_digest); reset first so no other
    // test's residual data changes what either run sees.
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: 733,
        seed: 2024,
    });

    let import_cursor = support::unique_name("parity_cursor");
    support::run_cursor_with_fetch_size(&import_cursor, 53, 37);

    let import_paging = support::unique_name("parity_paging");
    support::run_paging_with_page_size(&import_paging, 59, 41);

    assert_ne!(import_cursor, import_paging);

    let verify_cursor = support::verify(&import_cursor);
    assert!(
        verify_cursor.status.success(),
        "cursor run must verify cleanly: {}",
        String::from_utf8_lossy(&verify_cursor.stderr)
    );
    let verify_paging = support::verify(&import_paging);
    assert!(
        verify_paging.status.success(),
        "paging run must verify cleanly: {}",
        String::from_utf8_lossy(&verify_paging.stderr)
    );

    let cursor_report: serde_json::Value =
        serde_json::from_slice(&verify_cursor.stdout).expect("cursor verify prints a JSON report");
    let paging_report: serde_json::Value =
        serde_json::from_slice(&verify_paging.stdout).expect("paging verify prints a JSON report");
    assert_eq!(
        cursor_report["source_digest"], paging_report["source_digest"],
        "both runs must have observed the exact same source content identity"
    );
    assert_eq!(cursor_report["source_rows"].as_u64(), Some(dataset.rows));
    assert_eq!(paging_report["source_rows"].as_u64(), Some(dataset.rows));

    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute the shared source digest both runs observed");

    let cursor_rows = support::full_projection_rows(&pool, &import_cursor, &source_digest).await;
    let paging_rows = support::full_projection_rows(&pool, &import_paging, &source_digest).await;

    assert_eq!(
        cursor_rows.len(),
        dataset.rows as usize,
        "cursor destination row count must equal the source row count"
    );
    assert_eq!(
        paging_rows.len(),
        dataset.rows as usize,
        "paging destination row count must equal the source row count"
    );
    assert_eq!(
        cursor_rows.len(),
        paging_rows.len(),
        "cursor and paging must produce the same number of destination rows"
    );

    // Field-by-field, not a digest-only comparison: a pairwise Vec
    // comparison over two ORDER BY customer_id result sets directly proves
    // zero rows differ in either direction (a row present in one but not
    // the other would show up as a length mismatch above, or -- if it
    // replaced a different row at the same position -- as an unequal tuple
    // right here, since customer_id is itself part of each tuple).
    let mut divergences = Vec::new();
    for (cursor_row, paging_row) in cursor_rows.iter().zip(paging_rows.iter()) {
        if cursor_row != paging_row {
            divergences.push((cursor_row.0, paging_row.0));
        }
    }
    assert!(
        divergences.is_empty(),
        "cursor and paging projections diverged at customer_id pairs: {divergences:?}"
    );
    assert_eq!(
        cursor_rows, paging_rows,
        "cursor and paging must produce byte-for-byte identical projections over identical source content"
    );
}
