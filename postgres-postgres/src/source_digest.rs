//! Deterministic, streaming source content identity, plus the DB-enforced
//! stability guarantee that identity actually depends on.
//!
//! [`compute`] hashes every business-significant column of
//! `app_source.source_customer` in canonical `customer_id` order, using
//! `sqlx`'s row stream (`fetch`, never `fetch_all`) so memory stays bounded
//! regardless of source size. The result becomes an identifying
//! `JobParameter` (see `job::run`) alongside the user-facing import name,
//! and is also the scoping key `app_business.customer_projection` rows are
//! namespaced by (see migrations/001_init.sql).
//!
//! # Why a digest alone is not enough: the TOCTOU window
//!
//! Computing a digest over the source and then, separately, reading the
//! source again (whether for the actual transform-and-write pass in
//! `job::run`, or for the independent comparison in `verify`) is a
//! time-of-check-to-time-of-use hazard on its own: a source_digest computed
//! against connection/snapshot A says nothing about what a *different*,
//! later connection/snapshot B will observe if `app_source.source_customer`
//! is mutated in between. Closing that window by *inference* (e.g. "no
//! other writer plausibly ran in that gap") is not a guarantee; it is a
//! hope. [`lock_source_for_stable_read`] closes it for real, at the
//! database level.
//!
//! [`lock_source_for_stable_read`] opens a dedicated transaction and takes
//! `LOCK TABLE app_source.source_customer IN SHARE MODE`. `SHARE MODE`:
//!
//! - is compatible with plain reads (`ACCESS SHARE`), so it never blocks
//!   `postgres_cursor_reader`'s own dedicated read-only transaction, or
//!   `verify`'s own source/destination streaming queries;
//! - conflicts with every write (`ROW EXCLUSIVE` and above -- `INSERT`,
//!   `UPDATE`, `DELETE`, `TRUNCATE`), so any such statement against this
//!   table, from any session, blocks for as long as the guard transaction
//!   is open, and any writer already in flight is waited on before the
//!   lock is even granted;
//! - is released the moment the guard transaction ends -- commit, rollback,
//!   or the connection simply dropping (e.g. a hard process crash) -- so a
//!   crashed guard can never leak a permanent lock.
//!
//! This is a real PostgreSQL-enforced guarantee, not a cooperative
//! convention every caller must remember to honor: a concurrent `seed` (or
//! any other writer) is *actually blocked*, not merely asked nicely to
//! wait. Both `job::run` and `verify` acquire this guard *before* computing
//! the digest and hold it for the entire duration their own digest and
//! subsequent source read must stay consistent with -- see each module's
//! own use of [`lock_source_for_stable_read`]. `tests/source_stability.rs`
//! exercises this directly: it proves a concurrent write attempt is
//! actually blocked while a run is in flight, not merely usually-fine in
//! practice.

use futures_util::TryStreamExt;
use sha2::{Digest, Sha256};
use sqlx::{Executor, PgPool, Postgres, Transaction};

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

/// Streams `app_source.source_customer` ordered by `customer_id` through
/// `executor` and returns its canonical content digest (hex-encoded
/// SHA-256).
///
/// `executor` is generic (`&PgPool`, `&mut PgConnection`, or `&mut
/// Transaction<'_, Postgres>` all work) so a caller that needs this
/// computation to run *inside* the same guard transaction returned by
/// [`lock_source_for_stable_read`] can pass `&mut *guard` directly -- see
/// `job::run` and `verify::verify`, and the module documentation above for
/// why that matters.
///
/// # Errors
///
/// Returns an error if the query fails.
pub async fn compute<'e, E>(executor: E) -> anyhow::Result<String>
where
    E: Executor<'e, Database = Postgres>,
{
    let mut hasher = Sha256::new();
    let mut rows = sqlx::query_as::<_, (i64, String, bool, i64)>(
        "SELECT customer_id, full_name, is_active, balance_cents \
         FROM app_source.source_customer ORDER BY customer_id",
    )
    .fetch(executor);
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

/// Opens a dedicated transaction holding `LOCK TABLE
/// app_source.source_customer IN SHARE MODE` for as long as the returned
/// transaction stays open. See the module documentation for exactly what
/// this does and does not block, and why it is the mechanism (not a
/// convention) that keeps a computed [`compute`] digest honest about what
/// gets read afterward.
///
/// The caller is responsible for keeping the returned transaction alive
/// for the entire span that must stay consistent with the digest (ending
/// it -- via `.commit()`, `.rollback()`, or simply dropping it -- releases
/// the lock immediately).
///
/// # Errors
///
/// Returns an error if the transaction cannot be started or the lock
/// statement fails.
pub async fn lock_source_for_stable_read(
    pool: &PgPool,
) -> anyhow::Result<Transaction<'static, Postgres>> {
    let mut guard = pool.begin().await?;
    sqlx::query("LOCK TABLE app_source.source_customer IN SHARE MODE")
        .execute(&mut *guard)
        .await?;
    Ok(guard)
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
