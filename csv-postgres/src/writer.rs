//! A hand-written `ItemWriter<CustomerRow>` using the lower-level
//! `BusinessTransaction`/`BusinessStatement` primitives directly, instead of
//! `oxide_batch::item_components::postgres_batch_writer`.
//!
//! FINDING (API ergonomics / missing capability): `postgres_batch_writer`
//! generates each row's placeholder group as a fixed `($1, $2, ...)` -- there
//! is no way for a caller to add a per-column cast (e.g. `$5::timestamptz`),
//! and `BusinessValue` has no temporal variant (only `Text`, `Bytes`, `I64`,
//! `Bool`, `Null`). Binding a timestamp as `BusinessValue::text(...)` against
//! a `TIMESTAMPTZ` column fails at the database: PostgreSQL's extended query
//! protocol does not apply an implicit/assignment cast from an *explicitly
//! text-typed* bound parameter to `timestamptz` (unlike an "unknown"-typed
//! SQL literal) -- confirmed directly against PostgreSQL 18 with `PREPARE`/
//! `EXECUTE`:
//!   `ERROR: column "created_at" is of type timestamp with time zone but
//!   expression is of type text`
//! So `postgres_batch_writer` is unusable as-is for any table with a
//! non-text/i64/bool/bytes column. This writer instead builds the same
//! bounded, chunked multi-row `INSERT ... VALUES` shape by hand, adding an
//! explicit `::timestamptz` cast on the one column that needs it, using the
//! same `BusinessTransaction`/`BusinessStatement`/`BusinessValue` primitives
//! `postgres_batch_writer` itself is built on -- not a reimplementation of
//! the framework, just its lower extension point instead of the
//! higher-level convenience one.

use oxide_batch::{
    BusinessStatement, BusinessValue, FailureCategory, ItemWriter, WriteContext, WriteOutcome,
    WriterError,
};

use crate::processor::CustomerRow;

/// Deliberately well under PostgreSQL's 65,535 bind-parameter ceiling, same
/// bound `postgres_batch_writer`'s own default uses.
const MAX_PARAMETERS_PER_STATEMENT: usize = 2000;
const COLUMNS_PER_ROW: usize = 5;

pub struct CustomerRowWriter {
    conflict_clause: Option<&'static str>,
}

impl CustomerRowWriter {
    pub const fn new(conflict_clause: Option<&'static str>) -> Self {
        Self { conflict_clause }
    }

    fn insert_sql(&self, row_count: usize) -> String {
        let mut param = 1usize;
        let rows = (0..row_count)
            .map(|_| {
                let (c1, c2, c3, c4, c5) = (param, param + 1, param + 2, param + 3, param + 4);
                param += COLUMNS_PER_ROW;
                format!("(${c1}, ${c2}, ${c3}, ${c4}, ${c5}::timestamptz)")
            })
            .collect::<Vec<_>>()
            .join(", ");
        let prefix = "INSERT INTO app_business.imported_customer \
             (customer_id, name, email, amount, created_at) VALUES";
        match self.conflict_clause {
            Some(conflict) => format!("{prefix} {rows} {conflict}"),
            None => format!("{prefix} {rows}"),
        }
    }
}

impl ItemWriter<CustomerRow> for CustomerRowWriter {
    async fn write(
        &self,
        items: &[CustomerRow],
        mut context: WriteContext<'_>,
    ) -> Result<WriteOutcome, WriterError> {
        if context.stop_token().is_stop_requested() {
            return Ok(WriteOutcome::Stopped);
        }
        if items.is_empty() {
            return Ok(WriteOutcome::Written);
        }
        let transaction = context
            .transaction()
            .ok_or_else(|| WriterError::with_category(FailureCategory::UnsupportedCapability))?;

        let rows_per_statement = (MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW).max(1);
        for chunk in items.chunks(rows_per_statement) {
            let mut values = Vec::with_capacity(chunk.len() * COLUMNS_PER_ROW);
            for item in chunk {
                values.push(BusinessValue::i64(item.customer_id));
                values.push(BusinessValue::text(&item.name));
                values.push(BusinessValue::text(&item.email));
                values.push(BusinessValue::i64(item.amount));
                values.push(BusinessValue::text(item.created_at_rfc3339.as_str()));
            }
            let text = self.insert_sql(chunk.len());
            let statement = BusinessStatement::new(&text, &values);
            // FRAMEWORK LIMITATION, not a workaround opportunity: `error`
            // here is already `BusinessTransactionError` (Infrastructure /
            // Rejected / Cancelled) -- the framework's own PostgreSQL
            // adapter has already discarded the SQLSTATE, constraint name,
            // and driver error before this code ever sees it (that
            // redaction happens inside `BusinessTransaction::execute`
            // itself, at a boundary this consumer has no way to reach
            // behind). A real PRIMARY KEY violation and a transient
            // connection failure are both indistinguishable stable
            // categories once mapped to `WriterError` below. Transaction
            // correctness (the row's chunk still rolls back correctly
            // either way) is unaffected and independently verified in
            // tests/rollback.rs; root-cause diagnosability through the
            // public API is what's limited. Filed as
            // luceat-lux-vestra/oxide-batch#220.
            transaction
                .execute(statement)
                .await
                .map_err(|error| match error {
                    oxide_batch::BusinessTransactionError::Infrastructure => {
                        WriterError::with_category(FailureCategory::TransientInfrastructure)
                    }
                    oxide_batch::BusinessTransactionError::Rejected => {
                        WriterError::with_category(FailureCategory::UserComponent)
                    }
                    oxide_batch::BusinessTransactionError::Cancelled => {
                        WriterError::with_category(FailureCategory::Cancelled)
                    }
                    _ => WriterError::with_category(FailureCategory::TransientInfrastructure),
                })?;
        }
        Ok(WriteOutcome::Written)
    }
}
