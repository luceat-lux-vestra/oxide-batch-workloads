//! Independent, framework-bypassing verification.
//!
//! This module never calls into the OxideBatch execution path (no
//! `JobLauncher`, no `ChunkJob`) and never calls `processor::transform`/
//! `processor::fingerprint` as its expected-value oracle: it hand-writes its
//! own copy of the same transformation arithmetic below
//! (`expected_projection`), so a defect in `src/processor.rs` is not
//! automatically invisible to verification, and a weakened verifier that
//! merely re-ran production code would not be caught by
//! `tests/verifier_negative_control.rs`.
//!
//! Both sides are read by streaming (`sqlx`'s row `fetch`, never
//! `fetch_all`), and compared with a single ordered merge pass over
//! `customer_id` -- source and destination are both read `ORDER BY
//! customer_id`, so a row present on only one side is detected the moment
//! the merge's cursors diverge, without materializing either side.

use futures_util::TryStreamExt;
use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::hex::hex_digest;

const FINGERPRINT_LEN: usize = 16;
/// Independent copy of `processor::PREMIUM_THRESHOLD_CENTS`. Kept as a
/// separate literal, not a shared `use`, so this oracle does not silently
/// track a future change to the production constant.
const PREMIUM_THRESHOLD_CENTS: i64 = 50_000;

#[derive(Debug, PartialEq)]
struct ExpectedRow {
    customer_id: i64,
    display_name: String,
    loyalty_score: i64,
    is_premium: bool,
    row_fingerprint: [u8; FINGERPRINT_LEN],
}

/// Independent reimplementation of `processor::transform`'s arithmetic. See
/// module documentation for why this is a separate copy rather than a call
/// into `processor`.
fn expected_projection(
    customer_id: i64,
    full_name: &str,
    is_active: bool,
    balance_cents: i64,
) -> ExpectedRow {
    let mut hasher = Sha256::new();
    hasher.update(customer_id.to_le_bytes());
    hasher.update(0u8.to_le_bytes());
    hasher.update(full_name.as_bytes());
    hasher.update(0u8.to_le_bytes());
    hasher.update([u8::from(is_active)]);
    hasher.update(balance_cents.to_le_bytes());
    let digest = hasher.finalize();
    let mut row_fingerprint = [0u8; FINGERPRINT_LEN];
    row_fingerprint.copy_from_slice(&digest[..FINGERPRINT_LEN]);

    ExpectedRow {
        customer_id,
        display_name: full_name.to_uppercase(),
        loyalty_score: balance_cents / 100,
        is_premium: balance_cents >= PREMIUM_THRESHOLD_CENTS,
        row_fingerprint,
    }
}

struct ActualRow {
    customer_id: i64,
    display_name: String,
    loyalty_score: i64,
    is_premium: bool,
    row_fingerprint: Vec<u8>,
}

type SourceTuple = (i64, String, bool, i64);
type DestinationTuple = (i64, String, i64, bool, Vec<u8>);

fn to_expected((customer_id, full_name, is_active, balance_cents): SourceTuple) -> ExpectedRow {
    expected_projection(customer_id, &full_name, is_active, balance_cents)
}

fn to_actual(
    (customer_id, display_name, loyalty_score, is_premium, row_fingerprint): DestinationTuple,
) -> ActualRow {
    ActualRow {
        customer_id,
        display_name,
        loyalty_score,
        is_premium,
        row_fingerprint,
    }
}

fn hash_expected(hasher: &mut Sha256, row: &ExpectedRow) {
    hasher.update(row.customer_id.to_le_bytes());
    hasher.update(row.display_name.as_bytes());
    hasher.update([0u8]);
    hasher.update(row.loyalty_score.to_le_bytes());
    hasher.update([u8::from(row.is_premium)]);
    hasher.update(row.row_fingerprint);
    hasher.update([0xFFu8]);
}

fn hash_actual(hasher: &mut Sha256, row: &ActualRow) {
    hasher.update(row.customer_id.to_le_bytes());
    hasher.update(row.display_name.as_bytes());
    hasher.update([0u8]);
    hasher.update(row.loyalty_score.to_le_bytes());
    hasher.update([u8::from(row.is_premium)]);
    hasher.update(&row.row_fingerprint);
    hasher.update([0xFFu8]);
}

#[derive(Serialize)]
struct Mismatch {
    customer_id: i64,
    reason: String,
}

#[derive(Serialize)]
struct VerifyReport {
    import_name: String,
    source_digest: String,
    source_rows: usize,
    destination_rows: usize,
    row_counts_match: bool,
    expected_digest_sha256: String,
    actual_digest_sha256: String,
    digests_match: bool,
    mismatches: Vec<Mismatch>,
}

fn compare(expected: &ExpectedRow, actual: &ActualRow) -> Option<String> {
    let mut reasons = Vec::new();
    if expected.display_name != actual.display_name {
        reasons.push("display_name".to_owned());
    }
    if expected.loyalty_score != actual.loyalty_score {
        reasons.push("loyalty_score".to_owned());
    }
    if expected.is_premium != actual.is_premium {
        reasons.push("is_premium".to_owned());
    }
    if expected.row_fingerprint.as_slice() != actual.row_fingerprint.as_slice() {
        reasons.push("row_fingerprint".to_owned());
    }
    if reasons.is_empty() {
        None
    } else {
        Some(reasons.join(","))
    }
}

/// Streams `app_source.source_customer` and the destination projection
/// scoped to `(import_name, source_digest)`, both ordered by `customer_id`,
/// and merge-compares them field by field. Prints a JSON report to stdout
/// regardless of outcome, and fails closed (nonzero exit) on any row-count,
/// per-field, or digest mismatch.
///
/// `source_digest` is recomputed live from the source table's current
/// content (the same streaming mechanism `job::run` uses to compute the
/// identifying job parameter -- see `source_digest::compute`), so `verify`
/// always checks the destination scope that the *current* source content
/// would resolve to.
///
/// # Errors
///
/// Returns an error (nonzero process exit) if the database is unreachable
/// or any row is missing, extra, or field-mismatched, or if the two content
/// digests disagree.
pub async fn verify(database_url: &str, import_name: &str) -> anyhow::Result<()> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(database_url)
        .await?;

    // Held for this whole function's source-touching span: the digest
    // below, and the source_rows stream built from the same guarded
    // connection right after it, must describe the same content, or a
    // concurrent writer landing between the two could make `verify` derive
    // its "expected" values from content that disagrees with the very
    // digest it used to pick a destination scope. See
    // `src/source_digest.rs`'s module documentation and
    // `tests/source_stability.rs`.
    let mut source_guard = crate::source_digest::lock_source_for_stable_read(&pool).await?;
    let source_digest = crate::source_digest::compute(&mut *source_guard).await?;

    let mut source_rows = sqlx::query_as::<_, SourceTuple>(
        "SELECT customer_id, full_name, is_active, balance_cents \
         FROM app_source.source_customer ORDER BY customer_id",
    )
    .fetch(&mut *source_guard);
    let mut destination_rows = sqlx::query_as::<_, DestinationTuple>(
        "SELECT customer_id, display_name, loyalty_score, is_premium, row_fingerprint \
         FROM app_business.customer_projection \
         WHERE import_name = $1 AND source_digest = $2 \
         ORDER BY customer_id",
    )
    .bind(import_name)
    .bind(source_digest.clone())
    .fetch(&pool);

    let mut expected_hasher = Sha256::new();
    let mut actual_hasher = Sha256::new();
    let mut mismatches = Vec::new();
    let mut source_count = 0usize;
    let mut destination_count = 0usize;

    let mut next_expected = source_rows.try_next().await?.map(to_expected);
    let mut next_actual = destination_rows.try_next().await?.map(to_actual);

    loop {
        match (next_expected, next_actual) {
            (None, None) => break,
            (Some(expected), None) => {
                source_count += 1;
                mismatches.push(Mismatch {
                    customer_id: expected.customer_id,
                    reason: "missing destination row".to_owned(),
                });
                hash_expected(&mut expected_hasher, &expected);
                next_expected = source_rows.try_next().await?.map(to_expected);
                next_actual = None;
            }
            (None, Some(actual)) => {
                destination_count += 1;
                mismatches.push(Mismatch {
                    customer_id: actual.customer_id,
                    reason: "unexpected destination row (no matching source row)".to_owned(),
                });
                hash_actual(&mut actual_hasher, &actual);
                next_actual = destination_rows.try_next().await?.map(to_actual);
                next_expected = None;
            }
            (Some(expected), Some(actual)) => {
                if expected.customer_id == actual.customer_id {
                    source_count += 1;
                    destination_count += 1;
                    if let Some(reason) = compare(&expected, &actual) {
                        mismatches.push(Mismatch {
                            customer_id: expected.customer_id,
                            reason,
                        });
                    }
                    hash_expected(&mut expected_hasher, &expected);
                    hash_actual(&mut actual_hasher, &actual);
                    next_expected = source_rows.try_next().await?.map(to_expected);
                    next_actual = destination_rows.try_next().await?.map(to_actual);
                } else if expected.customer_id < actual.customer_id {
                    source_count += 1;
                    mismatches.push(Mismatch {
                        customer_id: expected.customer_id,
                        reason: "missing destination row".to_owned(),
                    });
                    hash_expected(&mut expected_hasher, &expected);
                    next_expected = source_rows.try_next().await?.map(to_expected);
                    next_actual = Some(actual);
                } else {
                    destination_count += 1;
                    mismatches.push(Mismatch {
                        customer_id: actual.customer_id,
                        reason: "unexpected destination row (no matching source row)".to_owned(),
                    });
                    hash_actual(&mut actual_hasher, &actual);
                    next_actual = destination_rows.try_next().await?.map(to_actual);
                    next_expected = Some(expected);
                }
            }
        }
    }
    drop(source_rows);
    drop(destination_rows);
    // The source-stability window closes here: everything above that
    // needed the digest and the source read to agree is now done.
    source_guard.commit().await?;
    pool.close().await;

    let row_counts_match = source_count == destination_count;
    let expected_digest_sha256 = hex_digest(&expected_hasher.finalize());
    let actual_digest_sha256 = hex_digest(&actual_hasher.finalize());
    let digests_match = expected_digest_sha256 == actual_digest_sha256;

    let report = VerifyReport {
        import_name: import_name.to_owned(),
        source_digest,
        source_rows: source_count,
        destination_rows: destination_count,
        row_counts_match,
        expected_digest_sha256,
        actual_digest_sha256,
        digests_match,
        mismatches,
    };
    println!("{}", serde_json::to_string_pretty(&report)?);

    if !report.mismatches.is_empty() {
        anyhow::bail!(
            "verify failed: {} row mismatch(es) between source-derived expectation and destination content",
            report.mismatches.len()
        );
    }
    if !row_counts_match {
        anyhow::bail!(
            "verify failed: source has {source_count} business-significant rows, destination has {destination_count}"
        );
    }
    if !digests_match {
        anyhow::bail!("verify failed: expected and actual content digests do not match");
    }
    Ok(())
}
