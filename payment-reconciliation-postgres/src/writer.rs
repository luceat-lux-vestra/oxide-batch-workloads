use oxide_batch::item_components::{
    postgres_batch_writer, PostgresBatchMode, PostgresBatchWriter, PostgresComponentConfigError,
};
use oxide_batch::BusinessValue;

use crate::processor::ReconciliationDetailRow;

const COLUMNS_PER_ROW: usize = 12;

pub fn writer() -> Result<PostgresBatchWriter<ReconciliationDetailRow>, PostgresComponentConfigError>
{
    postgres_batch_writer(
        "INSERT INTO payment_reconciliation.reconciliation_detail          (run_id, provider_payment_id, classification, internal_row_count, provider_row_count,           internal_currency, provider_currency, internal_status, provider_status,           authorized_delta_minor, captured_delta_minor, refunded_delta_minor) VALUES",
        None::<&str>,
        COLUMNS_PER_ROW,
        PostgresBatchMode::multi_row_values(),
        |item: &ReconciliationDetailRow| {
            vec![
                BusinessValue::text(&item.run_id),
                BusinessValue::text(&item.provider_payment_id),
                BusinessValue::text(&item.classification),
                BusinessValue::i64(item.internal_row_count),
                BusinessValue::i64(item.provider_row_count),
                BusinessValue::text(&item.internal_currency),
                BusinessValue::text(&item.provider_currency),
                BusinessValue::text(&item.internal_status),
                BusinessValue::text(&item.provider_status),
                BusinessValue::i64(item.authorized_delta_minor),
                BusinessValue::i64(item.captured_delta_minor),
                BusinessValue::i64(item.refunded_delta_minor),
            ]
        },
    )
}
