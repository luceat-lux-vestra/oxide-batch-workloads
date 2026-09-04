//! The production `ItemProcessor<SourceRow, ProjectedRow>`.
//!
//! Deliberately simple, fixed arithmetic so `tests/support/mod.rs`'s
//! `expected_projection` (the independent verifier's oracle, in
//! `src/verify.rs`) can reimplement it separately without calling this
//! module -- see `src/verify.rs`'s module documentation for why sharing a
//! function here would defeat independent verification.

use oxide_batch::{FailureCategory, ItemProcessor, ProcessContext, ProcessOutcome, ProcessorError};
use sha2::{Digest, Sha256};

/// A source row as read by `postgres_cursor_reader`'s `map_row` (see
/// `job::run`).
#[derive(Clone, Debug)]
pub struct SourceRow {
    pub customer_id: i64,
    pub full_name: String,
    pub is_active: bool,
    pub balance_cents: i64,
}

/// The transformed destination row `writer::bind` binds into
/// `app_business.customer_projection`. Carries its own `import_name`/
/// `source_digest` identity (cloned per row by `CustomerProjector` from its
/// own fixed-for-the-job configuration) rather than the writer's bind
/// closure capturing them externally: `postgres_batch_writer`'s bind
/// signature is `for<'a> Fn(&'a I) -> Vec<BusinessValue<'a>>`, so every
/// bound value must borrow from the item itself.
#[derive(Clone, Debug)]
pub struct ProjectedRow {
    pub import_name: String,
    pub source_digest: String,
    pub customer_id: i64,
    pub display_name: String,
    pub loyalty_score: i64,
    pub is_premium: bool,
    pub row_fingerprint: [u8; FINGERPRINT_LEN],
}

pub const FINGERPRINT_LEN: usize = 16;

/// Rows with at least this balance are flagged `is_premium`. $500.00 in
/// cents.
pub const PREMIUM_THRESHOLD_CENTS: i64 = 50_000;

/// The pure transformed fields for one source row, with no identity
/// attached -- the arithmetic core both `CustomerProjector::process` and
/// this module's own unit tests exercise directly.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransformedFields {
    pub display_name: String,
    pub loyalty_score: i64,
    pub is_premium: bool,
    pub row_fingerprint: [u8; FINGERPRINT_LEN],
}

/// Computes the deterministic transformed fields for one source row. Used
/// by `CustomerProjector::process` below. `src/verify.rs`'s independent
/// verifier deliberately does **not** call this function -- it carries its
/// own separate, hand-written copy of this same arithmetic, so a defect
/// introduced here is not automatically invisible to verification. See
/// `src/verify.rs`'s module documentation.
#[must_use]
pub fn transform(
    customer_id: i64,
    full_name: &str,
    is_active: bool,
    balance_cents: i64,
) -> TransformedFields {
    TransformedFields {
        display_name: full_name.to_uppercase(),
        loyalty_score: balance_cents / 100,
        is_premium: balance_cents >= PREMIUM_THRESHOLD_CENTS,
        row_fingerprint: fingerprint(customer_id, full_name, is_active, balance_cents),
    }
}

/// A bounded 16-byte content fingerprint over the source row's own
/// business-significant fields, truncated from a full SHA-256 digest.
/// Demonstrates `postgres_batch_writer`'s `BusinessValue::bytes` support
/// with a value that is itself independently reproducible from the exact
/// same source fields.
#[must_use]
pub fn fingerprint(
    customer_id: i64,
    full_name: &str,
    is_active: bool,
    balance_cents: i64,
) -> [u8; FINGERPRINT_LEN] {
    let mut hasher = Sha256::new();
    hasher.update(customer_id.to_le_bytes());
    hasher.update(0u8.to_le_bytes());
    hasher.update(full_name.as_bytes());
    hasher.update(0u8.to_le_bytes());
    hasher.update([u8::from(is_active)]);
    hasher.update(balance_cents.to_le_bytes());
    let digest = hasher.finalize();
    let mut out = [0u8; FINGERPRINT_LEN];
    out.copy_from_slice(&digest[..FINGERPRINT_LEN]);
    out
}

/// Fixed for the whole job: the same `(import_name, source_digest)` that
/// `job::run` computed as the job's identifying parameters (see
/// `src/source_digest.rs`), cloned onto every emitted `ProjectedRow`.
pub struct CustomerProjector {
    pub import_name: String,
    pub source_digest: String,
}

impl ItemProcessor<SourceRow, ProjectedRow> for CustomerProjector {
    async fn process(
        &self,
        item: &SourceRow,
        _context: ProcessContext<'_>,
    ) -> Result<ProcessOutcome<ProjectedRow>, ProcessorError> {
        if item.full_name.trim().is_empty() {
            return Err(ProcessorError::with_category(
                FailureCategory::UserComponent,
            ));
        }
        let fields = transform(
            item.customer_id,
            &item.full_name,
            item.is_active,
            item.balance_cents,
        );
        Ok(ProcessOutcome::Item(ProjectedRow {
            import_name: self.import_name.clone(),
            source_digest: self.source_digest.clone(),
            customer_id: item.customer_id,
            display_name: fields.display_name,
            loyalty_score: fields.loyalty_score,
            is_premium: fields.is_premium,
            row_fingerprint: fields.row_fingerprint,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transform_is_deterministic() {
        let a = transform(1, "Alice Smith", true, 60_000);
        let b = transform(1, "Alice Smith", true, 60_000);
        assert_eq!(a.display_name, b.display_name);
        assert_eq!(a.loyalty_score, b.loyalty_score);
        assert_eq!(a.is_premium, b.is_premium);
        assert_eq!(a.row_fingerprint, b.row_fingerprint);
    }

    #[test]
    fn display_name_is_uppercased() {
        let row = transform(1, "Alice Smith", true, 0);
        assert_eq!(row.display_name, "ALICE SMITH");
    }

    #[test]
    fn loyalty_score_is_balance_cents_divided_by_100() {
        let row = transform(1, "Alice Smith", true, 12_345);
        assert_eq!(row.loyalty_score, 123);
    }

    #[test]
    fn is_premium_uses_the_documented_threshold() {
        assert!(!transform(1, "A", true, PREMIUM_THRESHOLD_CENTS - 1).is_premium);
        assert!(transform(1, "A", true, PREMIUM_THRESHOLD_CENTS).is_premium);
    }

    #[test]
    fn fingerprint_changes_when_any_source_field_changes() {
        let base = fingerprint(1, "Alice", true, 100);
        assert_ne!(base, fingerprint(2, "Alice", true, 100));
        assert_ne!(base, fingerprint(1, "Bob", true, 100));
        assert_ne!(base, fingerprint(1, "Alice", false, 100));
        assert_ne!(base, fingerprint(1, "Alice", true, 101));
    }
}
