//! Query-based verification: never trusts log strings. Compares the
//! **source CSV's own values** against the **database's own values** --
//! row count and a full-content canonical digest, both computed by
//! streaming (never buffering the whole file or the whole result set) --
//! and fails closed (nonzero exit) on any mismatch. Prints a
//! machine-readable JSON report to stdout either way, so a caller can
//! redirect it straight into `validation/*.json` evidence and still see
//! what was compared when it fails.
//!
//! This does not "trust" the CLI's own generator/processor: the CSV side
//! is parsed here independently with the `csv` crate (a real parser, so a
//! quoted comma or an escaped quote in a field is handled correctly, not
//! split naively on `,`), producing the same canonicalized row string the
//! database side does, so the two are directly comparable.
//!
//! **Scope**: this verifier requires the source CSV to be in strictly
//! ascending `customer_id` order (validated, fails closed if not -- see
//! `source_digest`'s doc comment for why: the database side is always
//! read `ORDER BY customer_id`, and this tool is not order-independent).
//! Every dataset this workload's own `generate` command produces satisfies
//! this by construction.

use std::path::Path;

use futures_util::TryStreamExt;
use serde::Serialize;
use sha2::{Digest, Sha256};

#[derive(Serialize)]
struct VerifyReport {
    source_rows: usize,
    db_row_count: i64,
    row_counts_match: bool,
    source_digest_sha256: String,
    db_digest_sha256: String,
    digests_match: bool,
}

/// One row's canonical representation, shared by both the CSV-parsing side
/// and the database-fetching side so the two digests are directly
/// comparable. `created_at` is normalized by parsing then re-rendering as
/// RFC 3339 on both sides (the CSV field and the database's decoded
/// `TIMESTAMPTZ` could otherwise differ in formatting despite representing
/// the same instant).
fn canonical_row(
    customer_id: i64,
    name: &str,
    email: &str,
    amount: i64,
    created_at_rfc3339: &str,
) -> String {
    format!("{customer_id}\0{name}\0{email}\0{amount}\0{created_at_rfc3339}\u{ff}")
}

/// Streams `input` through a real CSV parser (handles quoted fields and
/// escaped/doubled quotes correctly), returning the row count and a
/// canonical-content digest.
///
/// **Ordering contract**: the database side of this comparison
/// (`db_digest`) reads `ORDER BY customer_id`, and the two digests are
/// accumulated into a single running hash in the order each side is
/// visited -- so a genuinely order-independent comparison would need a
/// commutative combining step (e.g. summing or XORing independent
/// per-row digests) instead. This tool intentionally does not do that:
/// it requires and validates that the source CSV is itself in strictly
/// ascending `customer_id` order (true for every dataset this workload's
/// own `generate` command produces, by construction) and fails closed
/// with a clear error otherwise, rather than silently accepting an
/// unordered file whose match against the always-`customer_id`-ordered
/// database side would be coincidental. A verifier for arbitrarily
/// ordered source files is a different, larger tool than this workload
/// needed.
///
/// # Errors
///
/// Returns an error if `input` cannot be opened, a row fails to parse
/// (wrong field count, non-numeric `customer_id`/`amount`, or an
/// unparseable `created_at`), or `customer_id` is not strictly ascending
/// -- fails closed rather than skipping or silently mismatching.
fn source_digest(input: &Path) -> anyhow::Result<(usize, String)> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .flexible(false)
        .from_path(input)?;
    let mut hasher = Sha256::new();
    let mut rows = 0usize;
    let mut last_customer_id: Option<i64> = None;
    for record in reader.records() {
        let record = record?;
        if record.len() != 5 {
            anyhow::bail!(
                "source row {} has {} fields, expected 5",
                rows + 1,
                record.len()
            );
        }
        let customer_id: i64 = record[0].parse().map_err(|_| {
            anyhow::anyhow!(
                "source row {}: invalid customer_id '{}'",
                rows + 1,
                &record[0]
            )
        })?;
        if let Some(last) = last_customer_id {
            if customer_id <= last {
                anyhow::bail!(
                    "source row {}: customer_id {customer_id} is not strictly greater than the \
                     previous row's {last} -- verify requires the source CSV to be in strictly \
                     ascending customer_id order (see source_digest's doc comment); this file is \
                     out of scope for this verifier, not a content mismatch",
                    rows + 1
                );
            }
        }
        last_customer_id = Some(customer_id);
        let amount: i64 = record[3].parse().map_err(|_| {
            anyhow::anyhow!("source row {}: invalid amount '{}'", rows + 1, &record[3])
        })?;
        let created_at: chrono::DateTime<chrono::Utc> = record[4].parse().map_err(|_| {
            anyhow::anyhow!(
                "source row {}: invalid created_at '{}'",
                rows + 1,
                &record[4]
            )
        })?;
        hasher.update(
            canonical_row(
                customer_id,
                &record[1],
                &record[2],
                amount,
                &created_at.to_rfc3339(),
            )
            .as_bytes(),
        );
        rows += 1;
    }
    Ok((rows, format!("{:x}", hasher.finalize())))
}

/// Streams the business table (ordered by `customer_id`, matching the
/// source file's own ascending generation order) through `sqlx`'s row
/// stream (`fetch`, not `fetch_all`), returning the row count and a
/// canonical-content digest in the same format `source_digest` produces.
///
/// # Errors
///
/// Returns an error if the database is unreachable or the query fails.
async fn db_digest(pool: &sqlx::PgPool) -> anyhow::Result<(i64, String)> {
    let mut hasher = Sha256::new();
    let mut count = 0i64;
    let mut rows = sqlx::query_as::<_, (i64, String, String, i64, chrono::DateTime<chrono::Utc>)>(
        "SELECT customer_id, name, email, amount, created_at \
         FROM app_business.imported_customer ORDER BY customer_id",
    )
    .fetch(pool);
    while let Some((customer_id, name, email, amount, created_at)) = rows.try_next().await? {
        hasher.update(
            canonical_row(customer_id, &name, &email, amount, &created_at.to_rfc3339()).as_bytes(),
        );
        count += 1;
    }
    Ok((count, format!("{:x}", hasher.finalize())))
}

/// Compares `input`'s own content against the business table's content --
/// row count and a full-content digest, both computed independently on
/// each side. Prints a JSON report to stdout regardless of outcome.
///
/// # Errors
///
/// Returns an error (nonzero process exit) if the database is
/// unreachable, `input` cannot be parsed, or the two sides' row count or
/// digest do not match.
pub async fn verify(database_url: &str, input: &Path) -> anyhow::Result<()> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(database_url)
        .await?;

    let (source_rows, source_digest_sha256) = source_digest(input)?;
    let (db_row_count, db_digest_sha256) = db_digest(&pool).await?;
    pool.close().await;

    let row_counts_match = source_rows as i64 == db_row_count;
    let digests_match = source_digest_sha256 == db_digest_sha256;

    let report = VerifyReport {
        source_rows,
        db_row_count,
        row_counts_match,
        source_digest_sha256,
        db_digest_sha256,
        digests_match,
    };
    println!("{}", serde_json::to_string_pretty(&report)?);

    if !row_counts_match {
        anyhow::bail!("verify failed: source has {source_rows} rows, database has {db_row_count}");
    }
    if !digests_match {
        anyhow::bail!("verify failed: source and database content digests do not match");
    }
    Ok(())
}
