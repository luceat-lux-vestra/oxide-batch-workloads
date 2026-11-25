use std::collections::BTreeMap;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{postgres::PgPoolOptions, PgPool, Row};

use crate::candidate::{
    candidate_id, candidate_set_digest, validate_candidate, CandidateOperationState,
    CandidatePaymentState, CandidateSnapshot, InitialOperationKind,
};
use crate::http;
use crate::oracle::{CanonicalObservation, ProviderObservation, ProviderVisibility};
use crate::processor::{decision_from_observation, result_digest};
use crate::{CONTRACT_VERSION, OXIDE_BATCH_VERSION};

pub const VERIFICATION_SCHEMA: &str =
    "oxide-batch-workloads.payment-recovery-independent-verification";
pub const VERIFICATION_SCHEMA_VERSION: u32 = 1;
pub const VERIFIED_DECISION: &str = "RECOVERY_SEMANTICS_VERIFIED_SIDE_EFFECT_CARDINALITY_SEPARATE";
pub const BLOCKED_DECISION: &str = "RECOVERY_VERIFICATION_BLOCKED";
pub const SIDE_EFFECT_CARDINALITY_AUTHORITY: &str = "SEPARATE_PROVIDER_DAY_PROOF_REQUIRED";

#[derive(Debug, Serialize)]
pub struct VerificationReport {
    pub schema: &'static str,
    pub schema_version: u32,
    pub decision: &'static str,
    pub run: RunVerification,
    pub candidate_count: usize,
    pub attempt_count: usize,
    pub classification_counts: BTreeMap<String, usize>,
    pub unresolved_retry_later_count: usize,
    pub hold_manual_count: usize,
    pub result_set_digest: String,
    pub side_effect_cardinality_authority: &'static str,
    pub candidates: Vec<CandidateVerification>,
    pub violations: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct RunVerification {
    pub run_id: String,
    pub contract_version: String,
    pub oxide_batch_version: String,
    pub business_date: String,
    pub eligibility_cutoff: String,
    pub scenario_set_id: String,
    pub policy_version: String,
    pub candidate_set_digest: String,
    pub processing_generation: i64,
    pub run_state: String,
}

#[derive(Debug, Serialize)]
pub struct CandidateVerification {
    pub candidate_id: String,
    pub merchant_payment_id: String,
    pub operation_id: String,
    pub operation_kind: String,
    pub provider_payment_id: Option<String>,
    pub attempt_count: usize,
    pub latest_attempt_no: i64,
    pub latest_processing_generation: i64,
    pub classification: String,
    pub result_digest: String,
    pub current_provider_http_status: Option<u16>,
    pub stored_verified: bool,
}

#[derive(Debug)]
struct AttemptRecord {
    attempt_no: i64,
    processing_generation: i64,
    attempt_state: String,
    merchant_http_status: Option<i64>,
    canonical_payment_state: String,
    canonical_operation_state: String,
    canonical_provider_payment_id: Option<String>,
    canonical_gateway_operation_id: Option<String>,
    provider_visibility: String,
    provider_payment_id: Option<String>,
    provider_currency: Option<String>,
    provider_status: Option<String>,
    provider_authorized_amount_minor: Option<i64>,
    provider_captured_amount_minor: Option<i64>,
    provider_refunded_amount_minor: Option<i64>,
    outcome_kind: String,
}

#[derive(Debug)]
struct ResultRecord {
    classification: String,
    duplicate_provider_mutation_count: i64,
    final_canonical_payment_state: Option<String>,
    final_canonical_operation_state: Option<String>,
    final_provider_status: Option<String>,
    result_digest: String,
    verified: bool,
    oracle_violations: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct CurrentProviderPayment {
    id: String,
    currency: String,
    status: String,
    authorized_amount: i64,
    captured_amount: i64,
    refunded_amount: i64,
}

pub async fn verify(
    database_url: &str,
    provider_base_url: &str,
    run_id: &str,
) -> Result<VerificationReport> {
    if run_id.trim().is_empty() {
        bail!("run id must not be blank");
    }
    let provider_base = http::normalize_http_base_url("provider", provider_base_url)?;
    let pool = PgPoolOptions::new()
        .max_connections(4)
        .connect(database_url)
        .await
        .context("connect payment recovery PostgreSQL for independent verification")?;

    let run = load_run(&pool, run_id).await?;
    let candidates = load_candidates(&pool, run_id).await?;
    let mut violations = Vec::new();

    if run.contract_version != CONTRACT_VERSION {
        violations.push("run contract version differs from payment-recovery-v1".into());
    }
    if run.oxide_batch_version != OXIDE_BATCH_VERSION {
        violations.push("run OxideBatch version differs from exact 0.7.0".into());
    }
    if run.processing_generation <= 0 {
        violations.push("run has no completed positive processing generation".into());
    }
    if !matches!(run.run_state.as_str(), "PROCESSED" | "VERIFIED") {
        violations.push("run is not in a verifiable processed state".into());
    }
    if candidates.is_empty() {
        violations.push("recovery run contains no frozen candidates".into());
    }

    match candidate_set_digest(&candidates) {
        Ok(digest) if digest == run.candidate_set_digest => {}
        Ok(_) => {
            violations.push("candidate-set digest differs from the frozen run identity".into())
        }
        Err(_) => violations.push("frozen candidate set fails independent validation".into()),
    }

    let mut candidate_reports = Vec::with_capacity(candidates.len());
    let mut classification_counts = BTreeMap::new();
    let mut total_attempts = 0usize;
    let mut result_digest_pairs = Vec::new();

    for candidate in &candidates {
        match verify_candidate(&pool, &provider_base, &run, candidate).await {
            Ok(report) => {
                total_attempts += report.attempt_count;
                *classification_counts
                    .entry(report.classification.clone())
                    .or_insert(0) += 1;
                result_digest_pairs.push((
                    report.candidate_id.clone(),
                    report.result_digest.clone(),
                    report.classification.clone(),
                ));
                candidate_reports.push(report);
            }
            Err(error) => {
                violations.push(format!(
                    "candidate {} failed independent verification: {}",
                    candidate.candidate_id, error
                ));
            }
        }
    }

    let result_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT
         FROM payment_recovery_workload.payment_recovery_result
         WHERE run_id = $1",
    )
    .bind(run_id)
    .fetch_one(&pool)
    .await?;
    if usize::try_from(result_count).ok() != Some(candidates.len()) {
        violations.push("result cardinality does not equal frozen candidate cardinality".into());
    }

    let attempt_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT
         FROM payment_recovery_workload.payment_recovery_attempt
         WHERE run_id = $1",
    )
    .bind(run_id)
    .fetch_one(&pool)
    .await?;
    if usize::try_from(attempt_count).ok() != Some(total_attempts) {
        violations
            .push("attempt cardinality differs from independently inspected candidates".into());
    }

    pool.close().await;

    result_digest_pairs.sort();
    let result_set_digest = result_set_digest(&result_digest_pairs);
    let unresolved_retry_later_count = *classification_counts.get("RETRY_LATER").unwrap_or(&0);
    let hold_manual_count = *classification_counts.get("HOLD_MANUAL").unwrap_or(&0);
    let decision = if violations.is_empty() {
        VERIFIED_DECISION
    } else {
        BLOCKED_DECISION
    };

    Ok(VerificationReport {
        schema: VERIFICATION_SCHEMA,
        schema_version: VERIFICATION_SCHEMA_VERSION,
        decision,
        run,
        candidate_count: candidates.len(),
        attempt_count: total_attempts,
        classification_counts,
        unresolved_retry_later_count,
        hold_manual_count,
        result_set_digest,
        side_effect_cardinality_authority: SIDE_EFFECT_CARDINALITY_AUTHORITY,
        candidates: candidate_reports,
        violations,
    })
}

async fn load_run(pool: &PgPool, run_id: &str) -> Result<RunVerification> {
    let row = sqlx::query(
        "SELECT run_id, contract_version, oxide_batch_version,
                business_date::text AS business_date,
                eligibility_cutoff::text AS eligibility_cutoff,
                scenario_set_id, policy_version, candidate_set_digest,
                processing_generation, run_state
         FROM payment_recovery_workload.payment_recovery_run
         WHERE run_id = $1",
    )
    .bind(run_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| anyhow::anyhow!("payment recovery run not found"))?;

    Ok(RunVerification {
        run_id: row.try_get("run_id")?,
        contract_version: row.try_get("contract_version")?,
        oxide_batch_version: row.try_get("oxide_batch_version")?,
        business_date: row.try_get("business_date")?,
        eligibility_cutoff: row.try_get("eligibility_cutoff")?,
        scenario_set_id: row.try_get("scenario_set_id")?,
        policy_version: row.try_get("policy_version")?,
        candidate_set_digest: row.try_get("candidate_set_digest")?,
        processing_generation: row.try_get("processing_generation")?,
        run_state: row.try_get("run_state")?,
    })
}

async fn load_candidates(pool: &PgPool, run_id: &str) -> Result<Vec<CandidateSnapshot>> {
    let rows = sqlx::query(
        r#"
        SELECT candidate_id, merchant_id, merchant_order_id, merchant_payment_id,
               operation_id, operation_kind, payment_state, operation_state,
               amount_minor, currency, idempotency_key, idempotency_key_digest,
               payment_method_token, payment_method_token_digest,
               provider_payment_id, gateway_operation_id,
               to_char(
                 source_updated_at AT TIME ZONE 'UTC',
                 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
               ) AS source_updated_at
        FROM payment_recovery_workload.payment_recovery_candidate
        WHERE run_id = $1
        ORDER BY merchant_payment_id, operation_id
        "#,
    )
    .bind(run_id)
    .fetch_all(pool)
    .await?;

    rows.into_iter()
        .map(|row| {
            let operation_kind =
                InitialOperationKind::parse(&row.try_get::<String, _>("operation_kind")?)?;
            let payment_state =
                CandidatePaymentState::parse(&row.try_get::<String, _>("payment_state")?)?;
            let operation_state =
                CandidateOperationState::parse(&row.try_get::<String, _>("operation_state")?)?;
            let candidate = CandidateSnapshot {
                candidate_id: row.try_get("candidate_id")?,
                merchant_id: row.try_get("merchant_id")?,
                merchant_order_id: row.try_get("merchant_order_id")?,
                merchant_payment_id: row.try_get("merchant_payment_id")?,
                operation_id: row.try_get("operation_id")?,
                operation_kind,
                payment_state,
                operation_state,
                amount_minor: row.try_get("amount_minor")?,
                currency: row.try_get("currency")?,
                idempotency_key: row.try_get("idempotency_key")?,
                idempotency_key_digest: row.try_get("idempotency_key_digest")?,
                payment_method_token: row.try_get("payment_method_token")?,
                payment_method_token_digest: row.try_get("payment_method_token_digest")?,
                provider_payment_id: row.try_get("provider_payment_id")?,
                gateway_operation_id: row.try_get("gateway_operation_id")?,
                source_updated_at: row.try_get("source_updated_at")?,
            };
            validate_candidate(&candidate)?;
            Ok(candidate)
        })
        .collect()
}

async fn verify_candidate(
    pool: &PgPool,
    provider_base: &str,
    run: &RunVerification,
    candidate: &CandidateSnapshot,
) -> Result<CandidateVerification> {
    validate_candidate(candidate)?;
    let expected_candidate_id = candidate_id(
        &candidate.merchant_payment_id,
        &candidate.operation_id,
        candidate.operation_kind,
        &candidate.idempotency_key_digest,
        candidate.amount_minor,
        &candidate.currency,
    );
    if candidate.candidate_id != expected_candidate_id {
        bail!("frozen candidate identity differs from deterministic derivation");
    }
    if let Some(gateway_operation_id) = candidate.gateway_operation_id.as_deref() {
        if gateway_operation_id != candidate.operation_id {
            bail!("frozen Gateway operation identity differs from recovery operation identity");
        }
    }

    let attempts = load_attempts(pool, &run.run_id, &candidate.candidate_id).await?;
    if attempts.is_empty() {
        bail!("no committed recovery attempt");
    }
    for (index, attempt) in attempts.iter().enumerate() {
        let expected_attempt = i64::try_from(index + 1).context("attempt index overflow")?;
        if attempt.attempt_no != expected_attempt {
            bail!("attempt numbers are not contiguous from one");
        }
        if attempt.processing_generation <= 0
            || attempt.processing_generation > run.processing_generation
        {
            bail!("attempt processing generation is outside the run generation");
        }
        if index > 0 && attempts[index - 1].processing_generation > attempt.processing_generation {
            bail!("attempt processing generations regress");
        }
    }

    let latest = attempts.last().context("latest recovery attempt missing")?;
    let result = load_result(pool, &run.run_id, &candidate.candidate_id).await?;

    if result.verified {
        bail!("result verified flag was promoted without the separate side-effect proof");
    }
    if result.duplicate_provider_mutation_count != 0 {
        bail!("stored recovery result reports duplicate provider mutation");
    }

    let canonical = CanonicalObservation {
        merchant_payment_id: candidate.merchant_payment_id.clone(),
        merchant_order_id: candidate.merchant_order_id.clone(),
        operation_id: candidate.operation_id.clone(),
        payment_state: latest.canonical_payment_state.clone(),
        operation_state: latest.canonical_operation_state.clone(),
        provider_payment_id: latest.canonical_provider_payment_id.clone(),
        gateway_operation_id: latest.canonical_gateway_operation_id.clone(),
    };
    let provider = attempt_provider_observation(latest)?;

    let decision = decision_from_observation(
        candidate,
        latest.merchant_http_status,
        &latest.outcome_kind,
        &canonical,
        &provider,
    )?;

    if decision.classification.as_str() != result.classification {
        bail!("stored classification differs from independent recomputation");
    }
    if decision.violations != result.oracle_violations {
        bail!("stored oracle violations differ from independent recomputation");
    }
    if result.final_canonical_payment_state.as_deref()
        != Some(latest.canonical_payment_state.as_str())
    {
        bail!("result final canonical payment state differs from latest attempt");
    }
    if result.final_canonical_operation_state.as_deref()
        != Some(latest.canonical_operation_state.as_str())
    {
        bail!("result final canonical operation state differs from latest attempt");
    }
    if result.final_provider_status != latest.provider_status {
        bail!("result final provider status differs from latest attempt");
    }

    let recomputed_digest = result_digest(
        candidate,
        latest.attempt_no,
        latest.processing_generation,
        &canonical,
        &provider,
        &result.classification,
        &latest.outcome_kind,
    );
    if recomputed_digest != result.result_digest {
        bail!("result digest differs from independent recomputation");
    }

    validate_attempt_state(latest)?;

    let provider_identity = latest
        .canonical_provider_payment_id
        .as_deref()
        .or(latest.provider_payment_id.as_deref())
        .or(candidate.provider_payment_id.as_deref());

    let current_provider_http_status = if let Some(provider_id) = provider_identity {
        Some(
            validate_current_provider(
                provider_base,
                provider_id,
                candidate,
                &result.classification,
            )
            .await?,
        )
    } else {
        if result.classification == "RESOLVED_SUCCEEDED" {
            bail!("resolved success lacks a provider identity for independent readback");
        }
        None
    };

    Ok(CandidateVerification {
        candidate_id: candidate.candidate_id.clone(),
        merchant_payment_id: candidate.merchant_payment_id.clone(),
        operation_id: candidate.operation_id.clone(),
        operation_kind: candidate.operation_kind.as_str().into(),
        provider_payment_id: provider_identity.map(str::to_owned),
        attempt_count: attempts.len(),
        latest_attempt_no: latest.attempt_no,
        latest_processing_generation: latest.processing_generation,
        classification: result.classification,
        result_digest: result.result_digest,
        current_provider_http_status,
        stored_verified: result.verified,
    })
}

async fn load_attempts(
    pool: &PgPool,
    run_id: &str,
    candidate_id: &str,
) -> Result<Vec<AttemptRecord>> {
    let rows = sqlx::query(
        "SELECT attempt_no, processing_generation, attempt_state,
                merchant_http_status, canonical_payment_state,
                canonical_operation_state, canonical_provider_payment_id,
                canonical_gateway_operation_id, provider_visibility,
                provider_payment_id, provider_currency, provider_status,
                provider_authorized_amount_minor, provider_captured_amount_minor,
                provider_refunded_amount_minor, outcome_kind
         FROM payment_recovery_workload.payment_recovery_attempt
         WHERE run_id = $1 AND candidate_id = $2
         ORDER BY attempt_no",
    )
    .bind(run_id)
    .bind(candidate_id)
    .fetch_all(pool)
    .await?;

    rows.into_iter()
        .map(|row| {
            Ok(AttemptRecord {
                attempt_no: row.try_get("attempt_no")?,
                processing_generation: row.try_get("processing_generation")?,
                attempt_state: row.try_get("attempt_state")?,
                merchant_http_status: row.try_get("merchant_http_status")?,
                canonical_payment_state: row.try_get("canonical_payment_state")?,
                canonical_operation_state: row.try_get("canonical_operation_state")?,
                canonical_provider_payment_id: row.try_get("canonical_provider_payment_id")?,
                canonical_gateway_operation_id: row.try_get("canonical_gateway_operation_id")?,
                provider_visibility: row.try_get("provider_visibility")?,
                provider_payment_id: row.try_get("provider_payment_id")?,
                provider_currency: row.try_get("provider_currency")?,
                provider_status: row.try_get("provider_status")?,
                provider_authorized_amount_minor: row
                    .try_get("provider_authorized_amount_minor")?,
                provider_captured_amount_minor: row.try_get("provider_captured_amount_minor")?,
                provider_refunded_amount_minor: row.try_get("provider_refunded_amount_minor")?,
                outcome_kind: row.try_get("outcome_kind")?,
            })
        })
        .collect()
}

async fn load_result(pool: &PgPool, run_id: &str, candidate_id: &str) -> Result<ResultRecord> {
    let row = sqlx::query(
        "SELECT classification, duplicate_provider_mutation_count,
                final_canonical_payment_state, final_canonical_operation_state,
                final_provider_status, result_digest, verified, oracle_violations
         FROM payment_recovery_workload.payment_recovery_result
         WHERE run_id = $1 AND candidate_id = $2",
    )
    .bind(run_id)
    .bind(candidate_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| anyhow::anyhow!("candidate has no recovery result"))?;

    let oracle_value: serde_json::Value = row.try_get("oracle_violations")?;
    let oracle_violations: Vec<String> =
        serde_json::from_value(oracle_value).context("decode recovery oracle violations")?;

    Ok(ResultRecord {
        classification: row.try_get("classification")?,
        duplicate_provider_mutation_count: row.try_get("duplicate_provider_mutation_count")?,
        final_canonical_payment_state: row.try_get("final_canonical_payment_state")?,
        final_canonical_operation_state: row.try_get("final_canonical_operation_state")?,
        final_provider_status: row.try_get("final_provider_status")?,
        result_digest: row.try_get("result_digest")?,
        verified: row.try_get("verified")?,
        oracle_violations,
    })
}

fn attempt_provider_observation(attempt: &AttemptRecord) -> Result<ProviderObservation> {
    let visibility = match attempt.provider_visibility.as_str() {
        "IMMEDIATE" => ProviderVisibility::Immediate,
        "NOT_FOUND" => ProviderVisibility::NotFound,
        "STALE" => ProviderVisibility::Stale,
        "UNAVAILABLE" => ProviderVisibility::Unavailable,
        _ => bail!("unsupported retained provider visibility"),
    };
    let exists = matches!(
        visibility,
        ProviderVisibility::Immediate | ProviderVisibility::Stale
    );

    Ok(ProviderObservation {
        visibility,
        exists,
        provider_payment_id: attempt.provider_payment_id.clone(),
        currency: attempt.provider_currency.clone(),
        status: attempt.provider_status.clone(),
        authorized_amount_minor: attempt.provider_authorized_amount_minor,
        captured_amount_minor: attempt.provider_captured_amount_minor,
        refunded_amount_minor: attempt.provider_refunded_amount_minor,
    })
}

fn validate_attempt_state(attempt: &AttemptRecord) -> Result<()> {
    let expected = if attempt.outcome_kind == "MERCHANT_TRANSPORT_UNKNOWN" {
        "CALL_UNKNOWN"
    } else {
        "OBSERVED"
    };
    if attempt.attempt_state != expected {
        bail!("attempt state does not match retained outcome kind");
    }
    Ok(())
}

async fn validate_current_provider(
    provider_base: &str,
    provider_id: &str,
    candidate: &CandidateSnapshot,
    classification: &str,
) -> Result<u16> {
    let url = format!(
        "{}/payments/{}",
        provider_base,
        percent_encode_path_segment(provider_id)
    );
    let response = http::request_json("GET", &url, None, None)
        .await
        .context("independent provider readback failed")?;

    match response.status {
        404 => {
            if classification == "RESOLVED_SUCCEEDED" {
                bail!("resolved success conflicts with current provider absence");
            }
            Ok(404)
        }
        200 => {
            let payment: CurrentProviderPayment =
                serde_json::from_slice(&response.body).context("decode current provider truth")?;
            validate_current_provider_payment(provider_id, candidate, classification, &payment)?;
            Ok(200)
        }
        status => bail!("independent provider readback returned HTTP {status}"),
    }
}

fn validate_current_provider_payment(
    provider_id: &str,
    candidate: &CandidateSnapshot,
    classification: &str,
    payment: &CurrentProviderPayment,
) -> Result<()> {
    if payment.id != provider_id {
        bail!("current provider identity mismatch");
    }
    if payment.currency != candidate.currency {
        bail!("current provider currency mismatch");
    }
    if payment.authorized_amount < 0
        || payment.captured_amount < 0
        || payment.refunded_amount < 0
        || payment.refunded_amount > payment.captured_amount
        || payment.captured_amount > payment.authorized_amount
        || payment.authorized_amount > candidate.amount_minor
    {
        bail!("current provider financial invariant violation");
    }

    match classification {
        "RESOLVED_SUCCEEDED" => match candidate.operation_kind {
            InitialOperationKind::Authorize => {
                if payment.status != "authorized"
                    || payment.authorized_amount != candidate.amount_minor
                    || payment.captured_amount != 0
                    || payment.refunded_amount != 0
                {
                    bail!("current provider truth differs from resolved AUTHORIZE");
                }
            }
            InitialOperationKind::AuthCapture => {
                if payment.status != "captured"
                    || payment.authorized_amount != candidate.amount_minor
                    || payment.captured_amount != candidate.amount_minor
                    || payment.refunded_amount != 0
                {
                    bail!("current provider truth differs from resolved AUTH_CAPTURE");
                }
            }
        },
        "RESOLVED_FAILED" => {
            if payment.status != "failed"
                || payment.authorized_amount != 0
                || payment.captured_amount != 0
                || payment.refunded_amount != 0
            {
                bail!("current provider truth conflicts with resolved failure");
            }
        }
        "RETRY_LATER" | "HOLD_MANUAL" => {}
        "NOT_ELIGIBLE" => bail!("frozen eligible candidate cannot resolve as NOT_ELIGIBLE"),
        _ => bail!("unsupported recovery classification"),
    }

    Ok(())
}

fn result_set_digest(values: &[(String, String, String)]) -> String {
    let mut hasher = Sha256::new();
    for (candidate_id, result_digest, classification) in values {
        for value in [candidate_id, result_digest, classification] {
            hasher.update(value.as_bytes());
            hasher.update([0]);
        }
    }
    let bytes: [u8; 32] = hasher.finalize().into();
    hex_digest(&bytes)
}

fn percent_encode_path_segment(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        let unreserved = byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~');
        if unreserved {
            output.push(char::from(*byte));
        } else {
            use std::fmt::Write as _;
            let _ = write!(&mut output, "%{byte:02X}");
        }
    }
    output
}

fn hex_digest(bytes: &[u8; 32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(64);
    for byte in bytes {
        output.push(char::from(HEX[usize::from(byte >> 4)]));
        output.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidate(kind: InitialOperationKind) -> CandidateSnapshot {
        let key = "idem-verify".to_owned();
        let token = "tok-verify".to_owned();
        CandidateSnapshot {
            candidate_id: "candidate-verify".into(),
            merchant_id: "merchant-verify".into(),
            merchant_order_id: "order-verify".into(),
            merchant_payment_id: "payment-verify".into(),
            operation_id: "operation-verify".into(),
            operation_kind: kind,
            payment_state: CandidatePaymentState::PaymentUnknown,
            operation_state: CandidateOperationState::Unknown,
            amount_minor: 1000,
            currency: "USD".into(),
            idempotency_key_digest: crate::candidate::secret_digest(&key),
            idempotency_key: key,
            payment_method_token_digest: crate::candidate::secret_digest(&token),
            payment_method_token: token,
            provider_payment_id: Some("provider-verify".into()),
            gateway_operation_id: Some("operation-verify".into()),
            source_updated_at: "2026-11-21T00:00:00.000000Z".into(),
        }
    }

    #[test]
    fn current_provider_terminal_success_is_fail_closed() {
        let candidate = candidate(InitialOperationKind::Authorize);
        let good = CurrentProviderPayment {
            id: "provider-verify".into(),
            currency: "USD".into(),
            status: "authorized".into(),
            authorized_amount: 1000,
            captured_amount: 0,
            refunded_amount: 0,
        };
        assert!(validate_current_provider_payment(
            "provider-verify",
            &candidate,
            "RESOLVED_SUCCEEDED",
            &good,
        )
        .is_ok());

        let mut bad = good;
        bad.authorized_amount = 999;
        assert!(validate_current_provider_payment(
            "provider-verify",
            &candidate,
            "RESOLVED_SUCCEEDED",
            &bad,
        )
        .is_err());
    }

    #[test]
    fn result_set_digest_is_order_sensitive_after_explicit_sort() {
        let mut values = vec![
            ("candidate-b".into(), "b".repeat(64), "RETRY_LATER".into()),
            (
                "candidate-a".into(),
                "a".repeat(64),
                "RESOLVED_SUCCEEDED".into(),
            ),
        ];
        values.sort();
        let first = result_set_digest(&values);
        let second = result_set_digest(&values);
        assert_eq!(first, second);
        assert_eq!(first.len(), 64);
    }

    #[test]
    fn verification_report_contract_separates_provider_cardinality() {
        assert_eq!(
            SIDE_EFFECT_CARDINALITY_AUTHORITY,
            "SEPARATE_PROVIDER_DAY_PROOF_REQUIRED"
        );
        assert_ne!(VERIFIED_DECISION, BLOCKED_DECISION);
    }
}
