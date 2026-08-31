//! Query-based verification: never trusts log strings. Prints machine-
//! readable JSON to stdout so a caller can redirect it straight into
//! `validation/*.json` evidence.

use std::path::Path;

use serde::Serialize;
use sha2::{Digest, Sha256};

#[derive(Serialize)]
struct VerifyReport {
    db_row_count: i64,
    csv_non_empty_lines: usize,
    canonical_digest_sha256: String,
}

/// Computes a canonical digest of the business table's contents in
/// `customer_id` order, so a clean run and a recovered run can be compared
/// on actual contents, not just row counts.
///
/// # Errors
///
/// Returns an error if the database is unreachable or `input` cannot be read.
pub async fn verify(database_url: &str, input: &Path) -> anyhow::Result<()> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(database_url)
        .await?;

    let db_row_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM app_business.imported_customer")
        .fetch_one(&pool)
        .await?;

    let rows: Vec<(i64, String, String, i64, chrono::DateTime<chrono::Utc>)> = sqlx::query_as(
        "SELECT customer_id, name, email, amount, created_at \
         FROM app_business.imported_customer ORDER BY customer_id",
    )
    .fetch_all(&pool)
    .await?;

    let mut hasher = Sha256::new();
    for (customer_id, name, email, amount, created_at) in &rows {
        hasher.update(customer_id.to_le_bytes());
        hasher.update(0u8.to_le_bytes());
        hasher.update(name.as_bytes());
        hasher.update(0u8.to_le_bytes());
        hasher.update(email.as_bytes());
        hasher.update(0u8.to_le_bytes());
        hasher.update(amount.to_le_bytes());
        hasher.update(0u8.to_le_bytes());
        hasher.update(created_at.to_rfc3339().as_bytes());
        hasher.update(1u8.to_le_bytes());
    }
    let canonical_digest_sha256 = format!("{:x}", hasher.finalize());

    let csv_non_empty_lines = std::fs::read_to_string(input)?
        .lines()
        .filter(|line| !line.is_empty())
        .count();

    pool.close().await;

    let report = VerifyReport {
        db_row_count,
        csv_non_empty_lines,
        canonical_digest_sha256,
    };
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
