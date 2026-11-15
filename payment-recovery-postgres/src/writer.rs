use oxide_batch::{
    BusinessStatement, BusinessTransactionError, BusinessValue, FailureCategory, ItemWriter,
    WriteContext, WriteOutcome, WriterError,
};

use crate::crash::{CrashControl, CrashPoint};
use crate::processor::RecoveryWrite;

const ATTEMPT_INSERT: &str = "
INSERT INTO payment_recovery_workload.payment_recovery_attempt (
    run_id,
    candidate_id,
    attempt_no,
    processing_generation,
    attempt_state,
    merchant_http_status,
    canonical_payment_state,
    canonical_operation_state,
    canonical_provider_payment_id,
    canonical_gateway_operation_id,
    provider_visibility,
    provider_payment_id,
    provider_currency,
    provider_status,
    provider_authorized_amount_minor,
    provider_captured_amount_minor,
    provider_refunded_amount_minor,
    outcome_kind,
    completed_at
)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,NOW())
";

const RESULT_UPSERT: &str = "
INSERT INTO payment_recovery_workload.payment_recovery_result (
    run_id,
    candidate_id,
    classification,
    duplicate_provider_mutation_count,
    final_canonical_payment_state,
    final_canonical_operation_state,
    final_provider_status,
    result_digest,
    verified,
    oracle_violations,
    updated_at
)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,FALSE,$9::jsonb,NOW())
ON CONFLICT (run_id, candidate_id) DO UPDATE
SET classification = EXCLUDED.classification,
    duplicate_provider_mutation_count = EXCLUDED.duplicate_provider_mutation_count,
    final_canonical_payment_state = EXCLUDED.final_canonical_payment_state,
    final_canonical_operation_state = EXCLUDED.final_canonical_operation_state,
    final_provider_status = EXCLUDED.final_provider_status,
    result_digest = EXCLUDED.result_digest,
    verified = FALSE,
    oracle_violations = EXCLUDED.oracle_violations,
    updated_at = NOW()
";

pub struct RecoveryWriter {
    crash: CrashControl,
}

impl RecoveryWriter {
    pub const fn new(crash: CrashControl) -> Self {
        Self { crash }
    }
}

impl ItemWriter<RecoveryWrite> for RecoveryWriter {
    async fn write<'a>(
        &'a self,
        items: &'a [RecoveryWrite],
        mut context: WriteContext<'a>,
    ) -> Result<WriteOutcome, WriterError> {
        if context.stop_token().is_stop_requested() {
            return Ok(WriteOutcome::Stopped);
        }
        let transaction = context
            .transaction()
            .ok_or_else(|| WriterError::with_category(FailureCategory::UnsupportedCapability))?;

        for item in items {
            let attempt_values = [
                BusinessValue::text(&item.run_id),
                BusinessValue::text(&item.candidate_id),
                BusinessValue::i64(item.attempt_no),
                BusinessValue::i64(item.processing_generation),
                BusinessValue::text(&item.attempt_state),
                optional_i64(item.merchant_http_status),
                BusinessValue::text(&item.canonical_payment_state),
                BusinessValue::text(&item.canonical_operation_state),
                optional_text(item.canonical_provider_payment_id.as_deref()),
                optional_text(item.canonical_gateway_operation_id.as_deref()),
                BusinessValue::text(&item.provider_visibility),
                optional_text(item.provider_payment_id.as_deref()),
                optional_text(item.provider_currency.as_deref()),
                optional_text(item.provider_status.as_deref()),
                optional_i64(item.provider_authorized_amount_minor),
                optional_i64(item.provider_captured_amount_minor),
                optional_i64(item.provider_refunded_amount_minor),
                BusinessValue::text(&item.outcome_kind),
            ];
            let attempt = transaction
                .execute(BusinessStatement::new(ATTEMPT_INSERT, &attempt_values))
                .await
                .map_err(|error| {
                    eprintln!("payment-recovery writer failure stage=attempt-insert");
                    classify_transaction_error(error)
                })?;
            if attempt.rows_affected() != 1 {
                return Err(WriterError::with_category(FailureCategory::Invariant));
            }

            let result_values = [
                BusinessValue::text(&item.run_id),
                BusinessValue::text(&item.candidate_id),
                BusinessValue::text(&item.classification),
                BusinessValue::i64(item.duplicate_provider_mutation_count),
                BusinessValue::text(&item.canonical_payment_state),
                BusinessValue::text(&item.canonical_operation_state),
                optional_text(item.provider_status.as_deref()),
                BusinessValue::text(&item.result_digest),
                BusinessValue::text(&item.oracle_violations_json),
            ];
            let result = transaction
                .execute(BusinessStatement::new(RESULT_UPSERT, &result_values))
                .await
                .map_err(|error| {
                    eprintln!("payment-recovery writer failure stage=result-upsert");
                    classify_transaction_error(error)
                })?;
            if result.rows_affected() != 1 {
                return Err(WriterError::with_category(FailureCategory::Invariant));
            }

            self.crash
                .pause_if(
                    CrashPoint::AfterLocalWriteBeforeCheckpointCommit,
                    &item.candidate_id,
                )
                .await;
        }

        Ok(WriteOutcome::Written)
    }
}

fn optional_text(value: Option<&str>) -> BusinessValue<'_> {
    match value {
        Some(value) => BusinessValue::text(value),
        None => BusinessValue::null(),
    }
}

fn optional_i64(value: Option<i64>) -> BusinessValue<'static> {
    match value {
        Some(value) => BusinessValue::i64(value),
        None => BusinessValue::null(),
    }
}

fn classify_transaction_error(error: BusinessTransactionError) -> WriterError {
    match error {
        BusinessTransactionError::Infrastructure => {
            WriterError::with_category(FailureCategory::TransientInfrastructure)
        }
        BusinessTransactionError::Rejected => {
            WriterError::with_category(FailureCategory::UserComponent)
        }
        BusinessTransactionError::Cancelled => {
            WriterError::with_category(FailureCategory::Cancelled)
        }
        _ => WriterError::with_category(FailureCategory::UserComponent),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_and_result_sql_do_not_touch_framework_metadata() {
        for sql in [ATTEMPT_INSERT, RESULT_UPSERT] {
            assert!(sql.contains("payment_recovery_workload."));
            assert!(!sql.contains("oxide_batch."));
        }
    }

    #[test]
    fn result_upsert_resets_independent_verification() {
        assert!(RESULT_UPSERT.contains("verified = FALSE"));
        assert!(RESULT_UPSERT.contains("oracle_violations"));
    }
}
