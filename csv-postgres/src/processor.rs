//! Validates and parses one delimited CSV record into a `CustomerRow`.
//!
//! Row *shape* (wrong field count) is already rejected by
//! `oxide_batch::item_components::delimited_file_reader` itself (see
//! `job::reader`), so this processor only has to validate field *values*.
//! Fail-fast, no skip policy (spec ss28): any invalid value is a typed
//! `ProcessorError`, which rolls back the current chunk and fails the job.

use chrono::{DateTime, Utc};
use oxide_batch::item_components::DelimitedRecord;
use oxide_batch::{FailureCategory, ProcessContext, ProcessOutcome, ProcessorError};

pub const EXPECTED_FIELD_COUNT: usize = 5;

#[derive(Clone, Debug)]
pub struct CustomerRow {
    pub customer_id: i64,
    pub name: String,
    pub email: String,
    pub amount: i64,
    /// Owned RFC 3339 rendering of the validated `created_at` timestamp, so
    /// the `postgres_batch_writer` bind closure (which returns
    /// `BusinessValue<'a>` borrowed from `&'a CustomerRow`) can borrow it
    /// directly rather than binding a temporary.
    pub created_at_rfc3339: String,
}

pub struct CustomerRowProcessor;

impl oxide_batch::ItemProcessor<DelimitedRecord, CustomerRow> for CustomerRowProcessor {
    async fn process(
        &self,
        item: &DelimitedRecord,
        _context: ProcessContext<'_>,
    ) -> Result<ProcessOutcome<CustomerRow>, ProcessorError> {
        if item.len() != EXPECTED_FIELD_COUNT {
            tracing::warn!(field_count = item.len(), "malformed row: unexpected field count");
            return Err(ProcessorError::with_category(FailureCategory::UserComponent));
        }
        let reject = |field: &'static str| {
            tracing::warn!(field, "malformed row: unparseable field");
            ProcessorError::with_category(FailureCategory::UserComponent)
        };

        let customer_id: i64 = item
            .get(0)
            .ok_or_else(|| reject("customer_id"))?
            .parse()
            .map_err(|_| reject("customer_id"))?;
        let name = item.get(1).ok_or_else(|| reject("name"))?.to_owned();
        let email = item.get(2).ok_or_else(|| reject("email"))?.to_owned();
        let amount: i64 = item
            .get(3)
            .ok_or_else(|| reject("amount"))?
            .parse()
            .map_err(|_| reject("amount"))?;
        let created_at_field = item.get(4).ok_or_else(|| reject("created_at"))?;
        let created_at: DateTime<Utc> = created_at_field
            .parse()
            .map_err(|_| reject("created_at"))?;

        Ok(ProcessOutcome::Item(CustomerRow {
            customer_id,
            name,
            email,
            amount,
            created_at_rfc3339: created_at.to_rfc3339(),
        }))
    }
}
