use oxide_batch::{FailureCategory, ItemProcessor, ProcessContext, ProcessOutcome, ProcessorError};

#[derive(Clone, Debug)]
pub struct ReconciliationSourceRow {
    pub provider_payment_id: String,
    pub internal_row_count: i64,
    pub provider_row_count: i64,
    pub internal_currency: String,
    pub provider_currency: String,
    pub internal_status: String,
    pub provider_status: String,
    pub internal_authorized_amount_minor: i64,
    pub provider_authorized_amount_minor: i64,
    pub internal_captured_amount_minor: i64,
    pub provider_captured_amount_minor: i64,
    pub internal_refunded_amount_minor: i64,
    pub provider_refunded_amount_minor: i64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Classification {
    Match,
    MissingProvider,
    MissingInternal,
    DuplicateInternal,
    DuplicateProvider,
    AmountMismatch,
    AttributeMismatch,
}

impl Classification {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Match => "MATCH",
            Self::MissingProvider => "MISSING_PROVIDER",
            Self::MissingInternal => "MISSING_INTERNAL",
            Self::DuplicateInternal => "DUPLICATE_INTERNAL",
            Self::DuplicateProvider => "DUPLICATE_PROVIDER",
            Self::AmountMismatch => "AMOUNT_MISMATCH",
            Self::AttributeMismatch => "ATTRIBUTE_MISMATCH",
        }
    }
}

#[derive(Clone, Debug)]
pub struct ReconciliationDetailRow {
    pub run_id: String,
    pub provider_payment_id: String,
    pub classification: String,
    pub internal_row_count: i64,
    pub provider_row_count: i64,
    pub internal_currency: String,
    pub provider_currency: String,
    pub internal_status: String,
    pub provider_status: String,
    pub authorized_delta_minor: i64,
    pub captured_delta_minor: i64,
    pub refunded_delta_minor: i64,
}

pub struct ReconciliationProcessor {
    pub run_id: String,
}

impl ItemProcessor<ReconciliationSourceRow, ReconciliationDetailRow> for ReconciliationProcessor {
    async fn process(
        &self,
        item: &ReconciliationSourceRow,
        _context: ProcessContext<'_>,
    ) -> Result<ProcessOutcome<ReconciliationDetailRow>, ProcessorError> {
        if item.internal_row_count < 0 || item.provider_row_count < 0 {
            return Err(ProcessorError::with_category(FailureCategory::Invariant));
        }

        let classification = classify(item);

        Ok(ProcessOutcome::Item(ReconciliationDetailRow {
            run_id: self.run_id.clone(),
            provider_payment_id: item.provider_payment_id.clone(),
            classification: classification.as_str().to_owned(),
            internal_row_count: item.internal_row_count,
            provider_row_count: item.provider_row_count,
            internal_currency: item.internal_currency.clone(),
            provider_currency: item.provider_currency.clone(),
            internal_status: item.internal_status.clone(),
            provider_status: item.provider_status.clone(),
            authorized_delta_minor: item.provider_authorized_amount_minor
                - item.internal_authorized_amount_minor,
            captured_delta_minor: item.provider_captured_amount_minor
                - item.internal_captured_amount_minor,
            refunded_delta_minor: item.provider_refunded_amount_minor
                - item.internal_refunded_amount_minor,
        }))
    }
}

pub fn classify(item: &ReconciliationSourceRow) -> Classification {
    if item.internal_row_count == 0 {
        Classification::MissingInternal
    } else if item.provider_row_count == 0 {
        Classification::MissingProvider
    } else if item.internal_row_count > 1 {
        Classification::DuplicateInternal
    } else if item.provider_row_count > 1 {
        Classification::DuplicateProvider
    } else if item.internal_authorized_amount_minor != item.provider_authorized_amount_minor
        || item.internal_captured_amount_minor != item.provider_captured_amount_minor
        || item.internal_refunded_amount_minor != item.provider_refunded_amount_minor
    {
        Classification::AmountMismatch
    } else if item.internal_currency != item.provider_currency
        || item.internal_status != item.provider_status
    {
        Classification::AttributeMismatch
    } else {
        Classification::Match
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn matched() -> ReconciliationSourceRow {
        ReconciliationSourceRow {
            provider_payment_id: "provider-1".into(),
            internal_row_count: 1,
            provider_row_count: 1,
            internal_currency: "USD".into(),
            provider_currency: "USD".into(),
            internal_status: "CAPTURED".into(),
            provider_status: "CAPTURED".into(),
            internal_authorized_amount_minor: 100,
            provider_authorized_amount_minor: 100,
            internal_captured_amount_minor: 100,
            provider_captured_amount_minor: 100,
            internal_refunded_amount_minor: 0,
            provider_refunded_amount_minor: 0,
        }
    }

    #[test]
    fn processor_classification_precedence_is_frozen() {
        let mut row = matched();
        assert_eq!(classify(&row), Classification::Match);

        row.provider_row_count = 0;
        assert_eq!(classify(&row), Classification::MissingProvider);

        row = matched();
        row.internal_row_count = 0;
        assert_eq!(classify(&row), Classification::MissingInternal);

        row = matched();
        row.internal_row_count = 2;
        assert_eq!(classify(&row), Classification::DuplicateInternal);

        row = matched();
        row.provider_row_count = 2;
        assert_eq!(classify(&row), Classification::DuplicateProvider);

        row = matched();
        row.provider_captured_amount_minor += 1;
        assert_eq!(classify(&row), Classification::AmountMismatch);

        row = matched();
        row.provider_currency = "EUR".into();
        assert_eq!(classify(&row), Classification::AttributeMismatch);
    }
}
