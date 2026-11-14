use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::model::{InternalSnapshotRow, ProviderSnapshotRow};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Classification {
    Match,
    MissingProvider,
    MissingInternal,
    DuplicateInternal,
    DuplicateProvider,
    AmountMismatch,
    AttributeMismatch,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OracleDetail {
    pub provider_payment_id: String,
    pub classification: Classification,
    pub internal_row_count: usize,
    pub provider_row_count: usize,
    pub internal_currency: Option<String>,
    pub provider_currency: Option<String>,
    pub internal_status: Option<String>,
    pub provider_status: Option<String>,
    pub authorized_delta_minor: Option<i64>,
    pub captured_delta_minor: Option<i64>,
    pub refunded_delta_minor: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OracleSummary {
    pub business_state: &'static str,
    pub mismatch_count: usize,
    pub aggregate_delta_minor: i64,
    pub result_digest: String,
    pub details: Vec<OracleDetail>,
}

pub fn reconcile(
    internal: &[InternalSnapshotRow],
    provider: &[ProviderSnapshotRow],
) -> anyhow::Result<OracleSummary> {
    let mut internal_by_key: BTreeMap<&str, Vec<&InternalSnapshotRow>> = BTreeMap::new();
    let mut provider_by_key: BTreeMap<&str, Vec<&ProviderSnapshotRow>> = BTreeMap::new();

    for row in internal {
        internal_by_key
            .entry(row.provider_payment_id.as_str())
            .or_default()
            .push(row);
    }
    for row in provider {
        provider_by_key
            .entry(row.provider_payment_id.as_str())
            .or_default()
            .push(row);
    }

    let keys: BTreeSet<&str> = internal_by_key
        .keys()
        .copied()
        .chain(provider_by_key.keys().copied())
        .collect();

    let mut details = Vec::with_capacity(keys.len());
    for key in keys {
        let internal_rows = internal_by_key.get(key).cloned().unwrap_or_default();
        let provider_rows = provider_by_key.get(key).cloned().unwrap_or_default();
        details.push(classify_key(key, &internal_rows, &provider_rows));
    }

    let aggregate_internal = internal.iter().try_fold(0_i64, |acc, row| {
        acc.checked_add(row.authorized_amount_minor)
            .and_then(|value| value.checked_add(row.captured_amount_minor))
            .and_then(|value| value.checked_add(row.refunded_amount_minor))
            .ok_or_else(|| anyhow::anyhow!("internal aggregate amount overflow"))
    })?;
    let aggregate_provider = provider.iter().try_fold(0_i64, |acc, row| {
        acc.checked_add(row.authorized_amount_minor)
            .and_then(|value| value.checked_add(row.captured_amount_minor))
            .and_then(|value| value.checked_add(row.refunded_amount_minor))
            .ok_or_else(|| anyhow::anyhow!("provider aggregate amount overflow"))
    })?;
    let aggregate_delta_minor = aggregate_provider
        .checked_sub(aggregate_internal)
        .ok_or_else(|| anyhow::anyhow!("aggregate delta overflow"))?;

    let mismatch_count = details
        .iter()
        .filter(|detail| detail.classification != Classification::Match)
        .count();
    let business_state = if mismatch_count == 0 && aggregate_delta_minor == 0 {
        "CLEAR"
    } else {
        "HELD"
    };

    let mut hasher = Sha256::new();
    hasher.update(serde_json::to_vec(&details)?);
    hasher.update(b"\n");
    hasher.update(aggregate_delta_minor.to_string().as_bytes());
    hasher.update(b"\n");
    hasher.update(business_state.as_bytes());

    Ok(OracleSummary {
        business_state,
        mismatch_count,
        aggregate_delta_minor,
        result_digest: format!("{:x}", hasher.finalize()),
        details,
    })
}

fn classify_key(
    key: &str,
    internal: &[&InternalSnapshotRow],
    provider: &[&ProviderSnapshotRow],
) -> OracleDetail {
    let classification = if internal.is_empty() {
        Classification::MissingInternal
    } else if provider.is_empty() {
        Classification::MissingProvider
    } else if internal.len() > 1 {
        Classification::DuplicateInternal
    } else if provider.len() > 1 {
        Classification::DuplicateProvider
    } else {
        let internal_row = internal[0];
        let provider_row = provider[0];
        if amounts_differ(internal_row, provider_row) {
            Classification::AmountMismatch
        } else if internal_row.currency != provider_row.currency
            || internal_row.normalized_status != provider_row.normalized_status
        {
            Classification::AttributeMismatch
        } else {
            Classification::Match
        }
    };

    let internal_row = (internal.len() == 1).then_some(internal[0]);
    let provider_row = (provider.len() == 1).then_some(provider[0]);

    OracleDetail {
        provider_payment_id: key.to_owned(),
        classification,
        internal_row_count: internal.len(),
        provider_row_count: provider.len(),
        internal_currency: internal_row.map(|row| row.currency.clone()),
        provider_currency: provider_row.map(|row| row.currency.clone()),
        internal_status: internal_row.map(|row| row.normalized_status.clone()),
        provider_status: provider_row.map(|row| row.normalized_status.clone()),
        authorized_delta_minor: paired_delta(
            internal_row.map(|row| row.authorized_amount_minor),
            provider_row.map(|row| row.authorized_amount_minor),
        ),
        captured_delta_minor: paired_delta(
            internal_row.map(|row| row.captured_amount_minor),
            provider_row.map(|row| row.captured_amount_minor),
        ),
        refunded_delta_minor: paired_delta(
            internal_row.map(|row| row.refunded_amount_minor),
            provider_row.map(|row| row.refunded_amount_minor),
        ),
    }
}

fn amounts_differ(internal: &InternalSnapshotRow, provider: &ProviderSnapshotRow) -> bool {
    internal.authorized_amount_minor != provider.authorized_amount_minor
        || internal.captured_amount_minor != provider.captured_amount_minor
        || internal.refunded_amount_minor != provider.refunded_amount_minor
}

fn paired_delta(internal: Option<i64>, provider: Option<i64>) -> Option<i64> {
    match (internal, provider) {
        (Some(internal), Some(provider)) => provider.checked_sub(internal),
        _ => None,
    }
}

pub fn contains_classification(summary: &OracleSummary, classification: Classification) -> bool {
    summary
        .details
        .iter()
        .any(|detail| detail.classification == classification)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn internal(id: &str) -> InternalSnapshotRow {
        InternalSnapshotRow {
            source_row_id: format!("internal:{id}"),
            merchant_payment_id: format!("merchant:{id}"),
            provider_payment_id: id.into(),
            currency: "USD".into(),
            normalized_status: "CAPTURED".into(),
            authorized_amount_minor: 100,
            captured_amount_minor: 100,
            refunded_amount_minor: 0,
        }
    }

    fn provider(id: &str) -> ProviderSnapshotRow {
        ProviderSnapshotRow {
            source_row_id: format!("provider:{id}"),
            provider_payment_id: id.into(),
            currency: "USD".into(),
            normalized_status: "CAPTURED".into(),
            authorized_amount_minor: 100,
            captured_amount_minor: 100,
            refunded_amount_minor: 0,
            provider_version: 1,
        }
    }

    #[test]
    fn clean_pair_is_clear() {
        let summary = reconcile(&[internal("p1")], &[provider("p1")]).unwrap();
        assert_eq!(summary.business_state, "CLEAR");
        assert_eq!(summary.mismatch_count, 0);
        assert_eq!(summary.aggregate_delta_minor, 0);
        assert_eq!(summary.details[0].classification, Classification::Match);
    }

    #[test]
    fn classifies_missing_provider() {
        let summary = reconcile(&[internal("p1")], &[]).unwrap();
        assert!(contains_classification(
            &summary,
            Classification::MissingProvider
        ));
        assert_eq!(summary.business_state, "HELD");
    }

    #[test]
    fn classifies_missing_internal() {
        let summary = reconcile(&[], &[provider("p1")]).unwrap();
        assert!(contains_classification(
            &summary,
            Classification::MissingInternal
        ));
    }

    #[test]
    fn classifies_duplicate_internal() {
        let mut duplicate = internal("p1");
        duplicate.source_row_id = "internal:p1:duplicate".into();
        let summary = reconcile(&[internal("p1"), duplicate], &[provider("p1")]).unwrap();
        assert!(contains_classification(
            &summary,
            Classification::DuplicateInternal
        ));
    }

    #[test]
    fn classifies_duplicate_provider() {
        let mut duplicate = provider("p1");
        duplicate.source_row_id = "provider:p1:duplicate".into();
        let summary = reconcile(&[internal("p1")], &[provider("p1"), duplicate]).unwrap();
        assert!(contains_classification(
            &summary,
            Classification::DuplicateProvider
        ));
    }

    #[test]
    fn classifies_amount_mismatch() {
        let mut changed = provider("p1");
        changed.authorized_amount_minor += 1;
        let summary = reconcile(&[internal("p1")], &[changed]).unwrap();
        assert!(contains_classification(
            &summary,
            Classification::AmountMismatch
        ));
    }

    #[test]
    fn classifies_attribute_mismatch() {
        let mut changed = provider("p1");
        changed.currency = "EUR".into();
        let summary = reconcile(&[internal("p1")], &[changed]).unwrap();
        assert!(contains_classification(
            &summary,
            Classification::AttributeMismatch
        ));
    }
}
