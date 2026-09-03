//! Deterministic, streaming source content identity.
//!
//! [`compute`] hashes every business-significant column of
//! `app_source.source_customer` in canonical `customer_id` order, using
//! `sqlx`'s row stream (`fetch`, never `fetch_all`) so memory stays bounded
//! regardless of source size. The result becomes an identifying
//! `JobParameter` (see `job::run`) alongside the user-facing import name,
//! and is also the scoping key `app_business.customer_projection` rows are
//! namespaced by (see migrations/001_init.sql) -- a changed source
//! therefore both resolves to a distinct job identity *and* writes into a
//! disjoint destination scope, so it can never collide with or silently
//! resume a previous run's checkpoint.

use futures_util::TryStreamExt;
use sha2::{Digest, Sha256};

use crate::hex::hex_digest;

/// One row's canonical byte encoding, shared by nothing else: this is the
/// single place that defines what "the same source content" means.
/// NUL/0xFF separators bound each field and terminate each row so no
/// concatenation of adjacent values can collide (e.g. `("ab", "c")` vs
/// `("a", "bc")`).
fn encode_row(customer_id: i64, full_name: &str, is_active: bool, balance_cents: i64) -> Vec<u8> {
    let mut buffer = Vec::with_capacity(full_name.len() + 32);
    buffer.extend_from_slice(&customer_id.to_le_bytes());
    buffer.push(0);
    buffer.extend_from_slice(full_name.as_bytes());
    buffer.push(0);
    buffer.push(u8::from(is_active));
    buffer.extend_from_slice(&balance_cents.to_le_bytes());
    buffer.push(0xFF);
    buffer
}

/// Streams `app_source.source_customer` ordered by `customer_id` and
/// returns its canonical content digest (hex-encoded SHA-256).
///
/// # Errors
///
/// Returns an error if the query fails.
pub async fn compute(pool: &sqlx::PgPool) -> anyhow::Result<String> {
    let mut hasher = Sha256::new();
    let mut rows = sqlx::query_as::<_, (i64, String, bool, i64)>(
        "SELECT customer_id, full_name, is_active, balance_cents \
         FROM app_source.source_customer ORDER BY customer_id",
    )
    .fetch(pool);
    while let Some((customer_id, full_name, is_active, balance_cents)) = rows.try_next().await? {
        hasher.update(encode_row(
            customer_id,
            &full_name,
            is_active,
            balance_cents,
        ));
    }
    Ok(hex_digest(&hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::encode_row;

    #[test]
    fn encoding_is_deterministic_for_identical_input() {
        assert_eq!(
            encode_row(1, "Alice", true, 500),
            encode_row(1, "Alice", true, 500)
        );
    }

    #[test]
    fn a_changed_field_changes_the_encoding() {
        let base = encode_row(1, "Alice", true, 500);
        assert_ne!(base, encode_row(2, "Alice", true, 500));
        assert_ne!(base, encode_row(1, "Bob", true, 500));
        assert_ne!(base, encode_row(1, "Alice", false, 500));
        assert_ne!(base, encode_row(1, "Alice", true, 501));
    }
}
