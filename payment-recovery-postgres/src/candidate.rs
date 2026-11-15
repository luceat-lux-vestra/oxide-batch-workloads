use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{postgres::PgPoolOptions, PgPool, Row};

use crate::replay::ExactReplayRequest;
use crate::{CONTRACT_VERSION, OXIDE_BATCH_VERSION};

pub const DEFAULT_POLICY_VERSION: &str = "payment-recovery-policy-v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum InitialOperationKind {
    Authorize,
    AuthCapture,
}

impl InitialOperationKind {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "AUTHORIZE" => Ok(Self::Authorize),
            "AUTH_CAPTURE" => Ok(Self::AuthCapture),
            other => bail!("unsupported initial recovery operation kind: {other}"),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Authorize => "AUTHORIZE",
            Self::AuthCapture => "AUTH_CAPTURE",
        }
    }

    pub fn merchant_path_suffix(self) -> &'static str {
        match self {
            Self::Authorize => "authorize",
            Self::AuthCapture => "auth-capture",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CandidatePaymentState {
    PaymentUnknown,
    PaymentPending,
}

impl CandidatePaymentState {
    fn parse(value: &str) -> Result<Self> {
        match value {
            "PAYMENT_UNKNOWN" => Ok(Self::PaymentUnknown),
            "PAYMENT_PENDING" => Ok(Self::PaymentPending),
            other => bail!("unsupported candidate payment state: {other}"),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::PaymentUnknown => "PAYMENT_UNKNOWN",
            Self::PaymentPending => "PAYMENT_PENDING",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CandidateOperationState {
    Unknown,
    RetryPending,
}

impl CandidateOperationState {
    fn parse(value: &str) -> Result<Self> {
        match value {
            "UNKNOWN" => Ok(Self::Unknown),
            "RETRY_PENDING" => Ok(Self::RetryPending),
            other => bail!("unsupported candidate operation state: {other}"),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "UNKNOWN",
            Self::RetryPending => "RETRY_PENDING",
        }
    }
}

#[derive(Clone)]
pub struct CandidateSnapshot {
    pub candidate_id: String,
    pub merchant_id: String,
    pub merchant_order_id: String,
    pub merchant_payment_id: String,
    pub operation_id: String,
    pub operation_kind: InitialOperationKind,
    pub payment_state: CandidatePaymentState,
    pub operation_state: CandidateOperationState,
    pub amount_minor: i64,
    pub currency: String,
    pub idempotency_key: String,
    pub idempotency_key_digest: String,
    pub payment_method_token: String,
    pub payment_method_token_digest: String,
    pub provider_payment_id: Option<String>,
    pub gateway_operation_id: Option<String>,
    pub source_updated_at: String,
}

#[derive(Debug, Serialize)]
pub struct SnapshotSummary {
    pub run_id: String,
    pub candidate_count: usize,
    pub candidate_set_digest: String,
}

pub struct SnapshotCandidatesRequest<'a> {
    pub database_url: &'a str,
    pub merchant_database_url: &'a str,
    pub run_id: &'a str,
    pub business_date: &'a str,
    pub eligibility_cutoff: &'a str,
    pub scenario_set_id: &'a str,
    pub policy_version: &'a str,
}

pub async fn snapshot_candidates(request: SnapshotCandidatesRequest<'_>) -> Result<SnapshotSummary> {
    validate_identity("run id", request.run_id)?;
    validate_business_date(request.business_date)?;
    validate_non_blank("eligibility cutoff", request.eligibility_cutoff)?;
    validate_identity("scenario set id", request.scenario_set_id)?;
    validate_identity("policy version", request.policy_version)?;

    let merchant_pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(request.merchant_database_url)
        .await
        .context("connect Reference Merchant PostgreSQL")?;

    let candidates = load_candidates(
        &merchant_pool,
        request.business_date,
        request.eligibility_cutoff,
    )
    .await?;
    merchant_pool.close().await;

    if candidates.is_empty() {
        bail!(
            "no eligible payment recovery candidates for business_date={} cutoff={}",
            request.business_date,
            request.eligibility_cutoff
        );
    }

    for candidate in &candidates {
        validate_candidate(candidate)?;
        ExactReplayRequest::from_candidate(candidate)?;
    }

    let candidate_set_digest = candidate_set_digest(&candidates)?;
    let workload_pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(request.database_url)
        .await
        .context("connect payment recovery PostgreSQL")?;

    persist_snapshot(
        &workload_pool,
        request,
        &candidates,
        &candidate_set_digest,
    )
    .await?;
    workload_pool.close().await;

    Ok(SnapshotSummary {
        run_id: request.run_id.to_owned(),
        candidate_count: candidates.len(),
        candidate_set_digest,
    })
}

async fn load_candidates(
    pool: &PgPool,
    business_date: &str,
    eligibility_cutoff: &str,
) -> Result<Vec<CandidateSnapshot>> {
    let mut tx = pool.begin().await?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        .execute(&mut *tx)
        .await?;

    let rows = sqlx::query(
        r#"
        SELECT
            o.merchant_id,
            mp.merchant_order_id,
            mp.merchant_payment_id,
            mpo.operation_id,
            mpo.operation_kind,
            mp.payment_state,
            mpo.operation_state,
            mpo.amount_minor,
            mp.currency,
            mpo.idempotency_key,
            mp.payment_method_token,
            epm.provider_payment_id,
            epm.gateway_operation_id,
            to_char(
                GREATEST(
                    mp.updated_at,
                    mpo.updated_at,
                    COALESCE(epm.updated_at, mpo.updated_at)
                ) AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            ) AS source_updated_at
        FROM payment_reference_merchant.merchant_payment mp
        JOIN payment_reference_merchant.orders o
          ON o.merchant_order_id = mp.merchant_order_id
        JOIN payment_reference_merchant.merchant_payment_operation mpo
          ON mpo.merchant_payment_id = mp.merchant_payment_id
        LEFT JOIN payment_reference_merchant.external_payment_mapping epm
          ON epm.merchant_payment_id = mp.merchant_payment_id
        WHERE mpo.operation_kind IN ('AUTHORIZE', 'AUTH_CAPTURE')
          AND mpo.amount_minor IS NOT NULL
          AND (
                (mp.payment_state = 'PAYMENT_UNKNOWN' AND mpo.operation_state = 'UNKNOWN')
             OR (
                    mp.payment_state IN ('PAYMENT_UNKNOWN', 'PAYMENT_PENDING')
                AND mpo.operation_state = 'RETRY_PENDING'
                )
          )
          AND mpo.operation_id = (
                SELECT latest.operation_id
                FROM payment_reference_merchant.merchant_payment_operation latest
                WHERE latest.merchant_payment_id = mp.merchant_payment_id
                ORDER BY latest.created_at DESC, latest.operation_id DESC
                LIMIT 1
          )
          AND (mpo.created_at AT TIME ZONE 'UTC')::date = ($1::text)::date
          AND GREATEST(
                mp.updated_at,
                mpo.updated_at,
                COALESCE(epm.updated_at, mpo.updated_at)
              ) < ($2::text)::timestamptz
        ORDER BY mp.merchant_payment_id, mpo.operation_id
        "#,
    )
    .bind(business_date)
    .bind(eligibility_cutoff)
    .fetch_all(&mut *tx)
    .await?;

    let mut candidates = Vec::with_capacity(rows.len());
    for row in rows {
        let amount_minor: i64 = row
            .try_get::<Option<i64>, _>("amount_minor")?
            .context("eligible Merchant operation is missing amount_minor")?;
        let idempotency_key: String = row.try_get("idempotency_key")?;
        let payment_method_token: String = row.try_get("payment_method_token")?;
        let operation_kind =
            InitialOperationKind::parse(&row.try_get::<String, _>("operation_kind")?)?;
        let payment_state =
            CandidatePaymentState::parse(&row.try_get::<String, _>("payment_state")?)?;
        let operation_state =
            CandidateOperationState::parse(&row.try_get::<String, _>("operation_state")?)?;
        let merchant_id: String = row.try_get("merchant_id")?;
        let merchant_order_id: String = row.try_get("merchant_order_id")?;
        let merchant_payment_id: String = row.try_get("merchant_payment_id")?;
        let operation_id: String = row.try_get("operation_id")?;
        let currency: String = row.try_get("currency")?;
        let idempotency_key_digest = secret_digest(&idempotency_key);
        let payment_method_token_digest = secret_digest(&payment_method_token);
        let candidate_id = candidate_id(
            &merchant_payment_id,
            &operation_id,
            operation_kind,
            &idempotency_key_digest,
            amount_minor,
            &currency,
        );

        candidates.push(CandidateSnapshot {
            candidate_id,
            merchant_id,
            merchant_order_id,
            merchant_payment_id,
            operation_id,
            operation_kind,
            payment_state,
            operation_state,
            amount_minor,
            currency,
            idempotency_key,
            idempotency_key_digest,
            payment_method_token,
            payment_method_token_digest,
            provider_payment_id: row.try_get("provider_payment_id")?,
            gateway_operation_id: row.try_get("gateway_operation_id")?,
            source_updated_at: row.try_get("source_updated_at")?,
        });
    }

    tx.commit().await?;
    Ok(candidates)
}

async fn persist_snapshot(
    pool: &PgPool,
    request: SnapshotCandidatesRequest<'_>,
    candidates: &[CandidateSnapshot],
    candidate_set_digest: &str,
) -> Result<()> {
    let mut tx = pool.begin().await?;

    sqlx::query(
        r#"
        INSERT INTO payment_recovery_workload.payment_recovery_run (
            run_id,
            contract_version,
            oxide_batch_version,
            business_date,
            eligibility_cutoff,
            scenario_set_id,
            policy_version,
            candidate_set_digest
        )
        VALUES ($1, $2, $3, ($4::text)::date, ($5::text)::timestamptz, $6, $7, $8)
        "#,
    )
    .bind(request.run_id)
    .bind(CONTRACT_VERSION)
    .bind(OXIDE_BATCH_VERSION)
    .bind(request.business_date)
    .bind(request.eligibility_cutoff)
    .bind(request.scenario_set_id)
    .bind(request.policy_version)
    .bind(candidate_set_digest)
    .execute(&mut *tx)
    .await
    .context("insert payment recovery run identity")?;

    for candidate in candidates {
        sqlx::query(
            r#"
            INSERT INTO payment_recovery_workload.payment_recovery_candidate (
                run_id,
                candidate_id,
                merchant_id,
                merchant_order_id,
                merchant_payment_id,
                operation_id,
                operation_kind,
                payment_state,
                operation_state,
                amount_minor,
                currency,
                idempotency_key,
                idempotency_key_digest,
                payment_method_token,
                payment_method_token_digest,
                provider_payment_id,
                gateway_operation_id,
                source_updated_at
            )
            VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                ($18::text)::timestamptz
            )
            "#,
        )
        .bind(request.run_id)
        .bind(&candidate.candidate_id)
        .bind(&candidate.merchant_id)
        .bind(&candidate.merchant_order_id)
        .bind(&candidate.merchant_payment_id)
        .bind(&candidate.operation_id)
        .bind(candidate.operation_kind.as_str())
        .bind(candidate.payment_state.as_str())
        .bind(candidate.operation_state.as_str())
        .bind(candidate.amount_minor)
        .bind(&candidate.currency)
        .bind(&candidate.idempotency_key)
        .bind(&candidate.idempotency_key_digest)
        .bind(&candidate.payment_method_token)
        .bind(&candidate.payment_method_token_digest)
        .bind(&candidate.provider_payment_id)
        .bind(&candidate.gateway_operation_id)
        .bind(&candidate.source_updated_at)
        .execute(&mut *tx)
        .await?;
    }

    tx.commit().await?;
    Ok(())
}

pub fn validate_candidate(candidate: &CandidateSnapshot) -> Result<()> {
    validate_non_blank("merchant id", &candidate.merchant_id)?;
    validate_non_blank("merchant order id", &candidate.merchant_order_id)?;
    validate_non_blank("merchant payment id", &candidate.merchant_payment_id)?;
    validate_non_blank("operation id", &candidate.operation_id)?;
    validate_non_blank("idempotency key", &candidate.idempotency_key)?;
    validate_non_blank("payment method token", &candidate.payment_method_token)?;
    validate_currency(&candidate.currency)?;
    if candidate.amount_minor <= 0 {
        bail!("candidate amount_minor must be positive");
    }

    let state_pair_valid = matches!(
        (candidate.payment_state, candidate.operation_state),
        (
            CandidatePaymentState::PaymentUnknown,
            CandidateOperationState::Unknown
        ) | (
            CandidatePaymentState::PaymentUnknown,
            CandidateOperationState::RetryPending
        ) | (
            CandidatePaymentState::PaymentPending,
            CandidateOperationState::RetryPending
        )
    );
    if !state_pair_valid {
        bail!("candidate payment/operation state pair is not eligible");
    }
    if candidate.idempotency_key_digest != secret_digest(&candidate.idempotency_key) {
        bail!("candidate idempotency key digest does not match frozen replay input");
    }
    if candidate.payment_method_token_digest != secret_digest(&candidate.payment_method_token) {
        bail!("candidate payment method token digest does not match frozen replay input");
    }
    Ok(())
}

pub fn candidate_set_digest(candidates: &[CandidateSnapshot]) -> Result<String> {
    if candidates.is_empty() {
        bail!("candidate set digest requires at least one candidate");
    }
    let mut ordered = candidates.to_vec();
    ordered.sort_by(|left, right| {
        left.merchant_payment_id
            .cmp(&right.merchant_payment_id)
            .then(left.operation_id.cmp(&right.operation_id))
    });

    let mut hasher = Sha256::new();
    for candidate in &ordered {
        validate_candidate(candidate)?;
        digest_part(&mut hasher, &candidate.candidate_id);
        digest_part(&mut hasher, &candidate.merchant_id);
        digest_part(&mut hasher, &candidate.merchant_order_id);
        digest_part(&mut hasher, &candidate.merchant_payment_id);
        digest_part(&mut hasher, &candidate.operation_id);
        digest_part(&mut hasher, candidate.operation_kind.as_str());
        digest_part(&mut hasher, candidate.payment_state.as_str());
        digest_part(&mut hasher, candidate.operation_state.as_str());
        digest_part(&mut hasher, &candidate.amount_minor.to_string());
        digest_part(&mut hasher, &candidate.currency);
        digest_part(&mut hasher, &candidate.idempotency_key_digest);
        digest_part(&mut hasher, &candidate.payment_method_token_digest);
        digest_part(
            &mut hasher,
            candidate.provider_payment_id.as_deref().unwrap_or(""),
        );
        digest_part(
            &mut hasher,
            candidate.gateway_operation_id.as_deref().unwrap_or(""),
        );
        digest_part(&mut hasher, &candidate.source_updated_at);
    }
    Ok(finish_hex(hasher))
}

fn candidate_id(
    merchant_payment_id: &str,
    operation_id: &str,
    operation_kind: InitialOperationKind,
    idempotency_key_digest: &str,
    amount_minor: i64,
    currency: &str,
) -> String {
    let mut hasher = Sha256::new();
    digest_part(&mut hasher, merchant_payment_id);
    digest_part(&mut hasher, operation_id);
    digest_part(&mut hasher, operation_kind.as_str());
    digest_part(&mut hasher, idempotency_key_digest);
    digest_part(&mut hasher, &amount_minor.to_string());
    digest_part(&mut hasher, currency);
    format!("candidate-{}", finish_hex(hasher))
}

pub fn secret_digest(value: &str) -> String {
    let mut hasher = Sha256::new();
    digest_part(&mut hasher, value);
    finish_hex(hasher)
}

fn digest_part(hasher: &mut Sha256, value: &str) {
    hasher.update(Sha256::digest(value.as_bytes()));
}

fn finish_hex(hasher: Sha256) -> String {
    let digest: [u8; 32] = hasher.finalize().into();
    hex_digest(&digest)
}

fn hex_digest(bytes: &[u8; 32]) -> String {
    let mut output = String::with_capacity(64);
    for byte in bytes {
        output.push(char::from_digit(u32::from(byte >> 4), 16).unwrap_or('0'));
        output.push(char::from_digit(u32::from(byte & 0x0f), 16).unwrap_or('0'));
    }
    output
}

fn validate_non_blank(field: &str, value: &str) -> Result<()> {
    if value.trim().is_empty() {
        bail!("{field} must not be blank");
    }
    Ok(())
}

fn validate_identity(field: &str, value: &str) -> Result<()> {
    validate_non_blank(field, value)?;
    if !value
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        bail!("{field} contains unsupported characters");
    }
    Ok(())
}

fn validate_business_date(value: &str) -> Result<()> {
    let bytes = value.as_bytes();
    if bytes.len() != 10
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || !bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 4 | 7) || byte.is_ascii_digit())
    {
        bail!("business date must use YYYY-MM-DD");
    }
    Ok(())
}

fn validate_currency(value: &str) -> Result<()> {
    if value.len() != 3 || !value.bytes().all(|byte| byte.is_ascii_uppercase()) {
        bail!("currency must be three uppercase ASCII letters");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidate() -> CandidateSnapshot {
        let key = "idem-1".to_owned();
        let token = "test-token-1".to_owned();
        CandidateSnapshot {
            candidate_id: candidate_id(
                "payment-1",
                "operation-1",
                InitialOperationKind::Authorize,
                &secret_digest(&key),
                10_000,
                "KRW",
            ),
            merchant_id: "merchant-1".into(),
            merchant_order_id: "order-1".into(),
            merchant_payment_id: "payment-1".into(),
            operation_id: "operation-1".into(),
            operation_kind: InitialOperationKind::Authorize,
            payment_state: CandidatePaymentState::PaymentUnknown,
            operation_state: CandidateOperationState::Unknown,
            amount_minor: 10_000,
            currency: "KRW".into(),
            idempotency_key_digest: secret_digest(&key),
            idempotency_key: key,
            payment_method_token_digest: secret_digest(&token),
            payment_method_token: token,
            provider_payment_id: Some("provider-1".into()),
            gateway_operation_id: Some("operation-1".into()),
            source_updated_at: "2026-11-15 01:00:00+00".into(),
        }
    }

    #[test]
    fn eligible_state_pairs_are_fail_closed() {
        let mut value = candidate();
        assert!(validate_candidate(&value).is_ok());
        value.payment_state = CandidatePaymentState::PaymentPending;
        assert!(validate_candidate(&value).is_err());
    }

    #[test]
    fn candidate_set_digest_does_not_depend_on_input_order() {
        let first = candidate();
        let mut second = candidate();
        second.merchant_payment_id = "payment-2".into();
        second.operation_id = "operation-2".into();
        second.candidate_id = candidate_id(
            &second.merchant_payment_id,
            &second.operation_id,
            second.operation_kind,
            &second.idempotency_key_digest,
            second.amount_minor,
            &second.currency,
        );

        let forward = candidate_set_digest(&[first.clone(), second.clone()]).unwrap();
        let reverse = candidate_set_digest(&[second, first]).unwrap();
        assert_eq!(forward, reverse);
    }
}
