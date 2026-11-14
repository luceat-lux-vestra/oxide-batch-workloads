use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const CONTRACT_VERSION: &str = "payment-reconciliation-v1";
pub const DEFAULT_POLICY_VERSION: &str = "payment-reconciliation-policy-v1";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotMetadata {
    pub snapshot_id: String,
    pub business_date: String,
    pub provider_scope: String,
    pub scenario_case: String,
    pub base_snapshot_id: Option<String>,
    pub internal_snapshot_digest: String,
    pub provider_snapshot_digest: String,
    pub policy_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InternalSnapshotRow {
    pub source_row_id: String,
    pub merchant_payment_id: String,
    pub provider_payment_id: String,
    pub currency: String,
    pub normalized_status: String,
    pub authorized_amount_minor: i64,
    pub captured_amount_minor: i64,
    pub refunded_amount_minor: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderSnapshotRow {
    pub source_row_id: String,
    pub provider_payment_id: String,
    pub currency: String,
    pub normalized_status: String,
    pub authorized_amount_minor: i64,
    pub captured_amount_minor: i64,
    pub refunded_amount_minor: i64,
    pub provider_version: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotBundle {
    pub metadata: SnapshotMetadata,
    pub internal: Vec<InternalSnapshotRow>,
    pub provider: Vec<ProviderSnapshotRow>,
}

pub fn internal_digest(rows: &[InternalSnapshotRow]) -> anyhow::Result<String> {
    digest_rows(rows)
}

pub fn provider_digest(rows: &[ProviderSnapshotRow]) -> anyhow::Result<String> {
    digest_rows(rows)
}

fn digest_rows<T>(rows: &[T]) -> anyhow::Result<String>
where
    T: Serialize + Clone + RowIdentity,
{
    let mut rows = rows.to_vec();
    rows.sort_by(|left, right| left.row_identity().cmp(right.row_identity()));

    let mut hasher = Sha256::new();
    for row in rows {
        let encoded = serde_json::to_vec(&row)?;
        hasher.update(encoded);
        hasher.update(b"\n");
    }
    Ok(format!("{:x}", hasher.finalize()))
}

trait RowIdentity {
    fn row_identity(&self) -> &str;
}

impl RowIdentity for InternalSnapshotRow {
    fn row_identity(&self) -> &str {
        &self.source_row_id
    }
}

impl RowIdentity for ProviderSnapshotRow {
    fn row_identity(&self) -> &str {
        &self.source_row_id
    }
}

pub fn normalize_internal_status(status: &str) -> anyhow::Result<&'static str> {
    match status {
        "AUTHORIZED" => Ok("AUTHORIZED"),
        "PAID" => Ok("CAPTURED"),
        "REFUNDED" => Ok("REFUNDED"),
        other => anyhow::bail!("unsupported v1 Merchant payment state: {other}"),
    }
}

pub fn normalize_provider_status(status: &str) -> anyhow::Result<&'static str> {
    match status {
        "authorized" => Ok("AUTHORIZED"),
        "captured" => Ok("CAPTURED"),
        "refunded" => Ok("REFUNDED"),
        other => anyhow::bail!("unsupported v1 provider payment status: {other}"),
    }
}

pub fn validate_currency(currency: &str) -> anyhow::Result<()> {
    if currency.len() == 3 && currency.bytes().all(|byte| byte.is_ascii_uppercase()) {
        return Ok(());
    }
    anyhow::bail!("currency must be exactly three uppercase ASCII letters: {currency}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_normalization_matches_frozen_v1_scope() {
        assert_eq!(normalize_internal_status("AUTHORIZED").unwrap(), "AUTHORIZED");
        assert_eq!(normalize_internal_status("PAID").unwrap(), "CAPTURED");
        assert_eq!(normalize_internal_status("REFUNDED").unwrap(), "REFUNDED");
        assert_eq!(normalize_provider_status("authorized").unwrap(), "AUTHORIZED");
        assert_eq!(normalize_provider_status("captured").unwrap(), "CAPTURED");
        assert_eq!(normalize_provider_status("refunded").unwrap(), "REFUNDED");
        assert!(normalize_internal_status("PAYMENT_UNKNOWN").is_err());
        assert!(normalize_provider_status("pending").is_err());
    }

    #[test]
    fn digest_is_order_independent_but_content_sensitive() {
        let a = InternalSnapshotRow {
            source_row_id: "internal:b".into(),
            merchant_payment_id: "merchant-b".into(),
            provider_payment_id: "provider-b".into(),
            currency: "USD".into(),
            normalized_status: "AUTHORIZED".into(),
            authorized_amount_minor: 10,
            captured_amount_minor: 0,
            refunded_amount_minor: 0,
        };
        let mut b = a.clone();
        b.source_row_id = "internal:a".into();
        b.merchant_payment_id = "merchant-a".into();
        b.provider_payment_id = "provider-a".into();

        assert_eq!(
            internal_digest(&[a.clone(), b.clone()]).unwrap(),
            internal_digest(&[b.clone(), a.clone()]).unwrap()
        );

        b.authorized_amount_minor += 1;
        assert_ne!(
            internal_digest(&[a.clone(), b.clone()]).unwrap(),
            internal_digest(&[a, {
                let mut changed = b;
                changed.authorized_amount_minor += 1;
                changed
            }])
            .unwrap()
        );
    }
}
