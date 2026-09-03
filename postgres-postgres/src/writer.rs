//! Builds the released `postgres_batch_writer` directly (no hand-written
//! `ItemWriter`): `app_business.customer_projection`'s columns are all
//! within `BusinessValue`'s supported representation (`Text`, `I64`,
//! `Bool`, `Bytes`), so unlike `csv-postgres` (which needs a `TIMESTAMPTZ`
//! bind the released writer cannot produce, tracked upstream as #218) this
//! workload has no reason to reimplement the writer by hand.

use oxide_batch::item_components::{
    postgres_batch_writer, PostgresBatchMode, PostgresBatchWriter, PostgresComponentConfigError,
};
use oxide_batch::BusinessValue;

use crate::processor::ProjectedRow;

const COLUMNS_PER_ROW: usize = 7;

/// Builds the enlisted destination writer. `bind` only ever borrows from
/// the item it is given (`ProjectedRow` carries its own `import_name`/
/// `source_digest` identity, cloned per row by `CustomerProjector` -- see
/// `src/processor.rs`), matching `postgres_batch_writer`'s
/// `for<'a> Fn(&'a I) -> Vec<BusinessValue<'a>>` bind signature, which
/// cannot be satisfied by borrowing external state captured by the
/// closure itself.
///
/// # Errors
///
/// Returns [`PostgresComponentConfigError`] if the writer's own
/// construction-time validation rejects the configuration (see
/// `postgres_batch_writer`'s doc comment); this cannot happen with the
/// fixed arguments below, but the error is still propagated rather than
/// unwrapped.
pub fn writer() -> Result<PostgresBatchWriter<ProjectedRow>, PostgresComponentConfigError> {
    postgres_batch_writer(
        "INSERT INTO app_business.customer_projection \
         (import_name, source_digest, customer_id, display_name, loyalty_score, is_premium, row_fingerprint) \
         VALUES",
        None::<&str>,
        COLUMNS_PER_ROW,
        PostgresBatchMode::multi_row_values(),
        |item: &ProjectedRow| {
            vec![
                BusinessValue::text(&item.import_name),
                BusinessValue::text(&item.source_digest),
                BusinessValue::i64(item.customer_id),
                BusinessValue::text(&item.display_name),
                BusinessValue::i64(item.loyalty_score),
                BusinessValue::boolean(item.is_premium),
                BusinessValue::bytes(&item.row_fingerprint),
            ]
        },
    )
}
