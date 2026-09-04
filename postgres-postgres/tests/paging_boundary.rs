//! Paging boundary behavior (campaign #63, PR 2): a page size materially
//! smaller than the dataset, a page size and chunk size that are not
//! multiples of each other, and a source with gaps in `customer_id` (not
//! the contiguous range `seed` always produces on its own) -- all at once,
//! so a page-boundary defect has nowhere to hide behind a coincidentally
//! aligned size or an artificially contiguous keyset.

mod support;

use support::SeedOptions;

#[tokio::test]
async fn paging_lands_every_row_once_across_non_aligned_boundaries_with_gaps() {
    support::migrate();
    support::reset();
    // 401 is prime, safely larger than the page size below (so multiple
    // full pages plus one short final page are exercised), and not a
    // multiple of the chunk size either.
    let dataset = support::seed(SeedOptions {
        rows: 401,
        seed: 71,
    });

    let pool = support::pool().await;
    // Punch non-contiguous gaps directly into the source (bypassing the
    // application, same as this workload's other direct-DB-mutation
    // negative controls -- see tests/verifier_negative_control.rs): every
    // 11th customer_id in this dataset's range is deleted, so the paging
    // reader's keyset predicate must skip cleanly over missing keys rather
    // than assuming a dense range.
    let deleted: i64 = sqlx::query_scalar(
        "WITH gone AS ( \
            DELETE FROM app_source.source_customer \
            WHERE customer_id > $1 AND customer_id <= $2 \
              AND (customer_id - $1) % 11 = 0 \
            RETURNING 1 \
         ) SELECT COUNT(*) FROM gone",
    )
    .bind(dataset.id_offset as i64)
    .bind((dataset.id_offset + dataset.rows) as i64)
    .fetch_one(&pool)
    .await
    .expect("delete a non-contiguous subset of source rows");
    assert!(
        deleted > 0,
        "precondition failed: the gap-punching delete removed no rows"
    );
    let remaining_rows = (dataset.rows as i64) - deleted;

    let import_name = support::unique_name("paging_boundary");
    // page_size (33) is smaller than the remaining dataset, not a multiple
    // of chunk_size (50), and neither divides remaining_rows evenly --
    // guarantees at least one short/partial page and at least one chunk
    // that spans a page boundary.
    support::run_paging_with_page_size(&import_name, 50, 33);

    let verify_output = support::verify(&import_name);
    assert!(
        verify_output.status.success(),
        "paging must land every remaining (non-contiguous) row exactly once: stdout={}\nstderr={}",
        String::from_utf8_lossy(&verify_output.stdout),
        String::from_utf8_lossy(&verify_output.stderr),
    );
    let report: serde_json::Value =
        serde_json::from_slice(&verify_output.stdout).expect("verify prints a JSON report");
    assert_eq!(report["source_rows"].as_u64(), Some(remaining_rows as u64));
    assert_eq!(
        report["destination_rows"].as_u64(),
        Some(remaining_rows as u64)
    );
    assert_eq!(report["row_counts_match"].as_bool(), Some(true));
    assert_eq!(report["digests_match"].as_bool(), Some(true));
    assert!(report["mismatches"]
        .as_array()
        .expect("mismatches is an array")
        .is_empty());

    // Directly confirm no duplicated final rows: the primary key
    // (import_name, source_digest, customer_id) makes a literal duplicate
    // impossible, but a strict count-of-distinct check makes the "no
    // duplicated final rows" claim explicit and independent of that
    // constraint existing at all.
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest");
    let distinct: i64 = sqlx::query_scalar(
        "SELECT COUNT(DISTINCT customer_id) FROM app_business.customer_projection \
         WHERE import_name = $1 AND source_digest = $2",
    )
    .bind(&import_name)
    .bind(&source_digest)
    .fetch_one(&pool)
    .await
    .expect("count distinct customer_id");
    assert_eq!(distinct, remaining_rows);
}
