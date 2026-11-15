use anyhow::{bail, Context, Result};
use oxide_batch::{FailureCategory, ItemProcessor, ProcessContext, ProcessOutcome, ProcessorError};
use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::candidate::{
    validate_candidate, CandidateOperationState, CandidatePaymentState, CandidateSnapshot,
    InitialOperationKind,
};
use crate::crash::{CrashControl, CrashPoint};
use crate::http;
use crate::oracle::{
    self, CanonicalObservation, OracleDecision, ProviderObservation, ProviderVisibility,
    RecoveryClassification,
};
use crate::replay::{MerchantReplayClient, MerchantReplayView, ReplayTransportOutcome};

#[derive(Clone, Debug)]
pub struct RecoverySourceRow {
    pub candidate_id: String,
    pub merchant_id: String,
    pub merchant_order_id: String,
    pub merchant_payment_id: String,
    pub operation_id: String,
    pub operation_kind: String,
    pub payment_state: String,
    pub operation_state: String,
    pub amount_minor: i64,
    pub currency: String,
    pub idempotency_key: String,
    pub idempotency_key_digest: String,
    pub payment_method_token: String,
    pub payment_method_token_digest: String,
    pub provider_payment_id: Option<String>,
    pub gateway_operation_id: Option<String>,
    pub source_updated_at: String,
    pub attempt_no: i64,
}

#[derive(Clone, Debug)]
pub struct RecoveryWrite {
    pub run_id: String,
    pub candidate_id: String,
    pub attempt_no: i64,
    pub processing_generation: i64,
    pub attempt_state: String,
    pub merchant_http_status: Option<i64>,
    pub canonical_payment_state: String,
    pub canonical_operation_state: String,
    pub canonical_provider_payment_id: Option<String>,
    pub canonical_gateway_operation_id: Option<String>,
    pub provider_visibility: String,
    pub provider_payment_id: Option<String>,
    pub provider_currency: Option<String>,
    pub provider_status: Option<String>,
    pub provider_authorized_amount_minor: Option<i64>,
    pub provider_captured_amount_minor: Option<i64>,
    pub provider_refunded_amount_minor: Option<i64>,
    pub outcome_kind: String,
    pub classification: String,
    pub duplicate_provider_mutation_count: i64,
    pub result_digest: String,
    pub oracle_violations_json: String,
}

pub struct RecoveryProcessor {
    run_id: String,
    processing_generation: i64,
    merchant: MerchantReplayClient,
    provider_base_url: String,
    crash: CrashControl,
}

impl RecoveryProcessor {
    pub fn new(
        run_id: String,
        processing_generation: i64,
        merchant_base_url: &str,
        provider_base_url: &str,
        crash: CrashControl,
    ) -> Result<Self> {
        if processing_generation <= 0 {
            bail!("processing generation must be positive");
        }
        let provider_base_url = http::normalize_http_base_url("provider", provider_base_url)?;
        Ok(Self {
            run_id,
            processing_generation,
            merchant: MerchantReplayClient::new(merchant_base_url)?,
            provider_base_url,
            crash,
        })
    }

    fn candidate(&self, item: &RecoverySourceRow) -> Result<CandidateSnapshot> {
        let candidate = CandidateSnapshot {
            candidate_id: item.candidate_id.clone(),
            merchant_id: item.merchant_id.clone(),
            merchant_order_id: item.merchant_order_id.clone(),
            merchant_payment_id: item.merchant_payment_id.clone(),
            operation_id: item.operation_id.clone(),
            operation_kind: InitialOperationKind::parse(&item.operation_kind)?,
            payment_state: CandidatePaymentState::parse(&item.payment_state)?,
            operation_state: CandidateOperationState::parse(&item.operation_state)?,
            amount_minor: item.amount_minor,
            currency: item.currency.clone(),
            idempotency_key: item.idempotency_key.clone(),
            idempotency_key_digest: item.idempotency_key_digest.clone(),
            payment_method_token: item.payment_method_token.clone(),
            payment_method_token_digest: item.payment_method_token_digest.clone(),
            provider_payment_id: item.provider_payment_id.clone(),
            gateway_operation_id: item.gateway_operation_id.clone(),
            source_updated_at: item.source_updated_at.clone(),
        };
        validate_candidate(&candidate)?;
        Ok(candidate)
    }

    async fn process_one(&self, item: &RecoverySourceRow) -> Result<RecoveryWrite> {
        if item.attempt_no <= 0 {
            bail!("attempt number must be positive");
        }
        let candidate = self.candidate(item)?;
        self.crash
            .pause_if(
                CrashPoint::AfterCandidateReadBeforeReplay,
                &candidate.candidate_id,
            )
            .await;
        let replay = self.merchant.execute(&candidate, &self.crash).await?;

        let (attempt_state, merchant_http_status, outcome_kind, canonical, provider, decision) =
            match replay {
                ReplayTransportOutcome::TransportUnknown => {
                    let canonical = canonical_from_candidate(&candidate);
                    let provider = self
                        .fetch_provider(candidate.provider_payment_id.as_deref())
                        .await?;
                    let decision = OracleDecision {
                        classification: RecoveryClassification::RetryLater,
                        violations: vec!["Merchant replay transport outcome is unknown".into()],
                    };
                    (
                        "CALL_UNKNOWN",
                        None,
                        "MERCHANT_TRANSPORT_UNKNOWN",
                        canonical,
                        provider,
                        decision,
                    )
                }
                ReplayTransportOutcome::Rejected { http_status } => {
                    let canonical = canonical_from_candidate(&candidate);
                    let provider = self
                        .fetch_provider(candidate.provider_payment_id.as_deref())
                        .await?;
                    let retryable = is_retryable_http_status(http_status);
                    let decision = OracleDecision {
                        classification: if retryable {
                            RecoveryClassification::RetryLater
                        } else {
                            RecoveryClassification::HoldManual
                        },
                        violations: if retryable {
                            Vec::new()
                        } else {
                            vec![format!("Merchant replay rejected HTTP {http_status}")]
                        },
                    };
                    (
                        "OBSERVED",
                        Some(i64::from(http_status)),
                        if retryable {
                            "MERCHANT_RETRYABLE_REJECTION"
                        } else {
                            "MERCHANT_NON_RETRYABLE_REJECTION"
                        },
                        canonical,
                        provider,
                        decision,
                    )
                }
                ReplayTransportOutcome::IncoherentResponse { http_status } => {
                    let canonical = canonical_from_candidate(&candidate);
                    let provider = self
                        .fetch_provider(candidate.provider_payment_id.as_deref())
                        .await?;
                    let decision = OracleDecision {
                        classification: RecoveryClassification::HoldManual,
                        violations: vec![
                            "Merchant replay returned an incoherent canonical response".into(),
                        ],
                    };
                    (
                        "OBSERVED",
                        Some(i64::from(http_status)),
                        "MERCHANT_INCOHERENT_RESPONSE",
                        canonical,
                        provider,
                        decision,
                    )
                }
                ReplayTransportOutcome::Canonical { http_status, view } => {
                    let canonical = canonical_from_view(&view);
                    let provider_id = view
                        .mapping
                        .provider_payment_id
                        .as_deref()
                        .or(candidate.provider_payment_id.as_deref());
                    let provider = self.fetch_provider(provider_id).await?;
                    let decision = oracle::classify(&candidate, &canonical, &provider, false);
                    (
                        "OBSERVED",
                        Some(i64::from(http_status)),
                        "MERCHANT_CANONICAL_RESPONSE",
                        canonical,
                        provider,
                        decision,
                    )
                }
            };

        if matches!(
            decision.classification,
            RecoveryClassification::ResolvedSucceeded | RecoveryClassification::ResolvedFailed
        ) {
            self.crash
                .pause_if(
                    CrashPoint::AfterExternalConvergenceBeforeLocalCommit,
                    &candidate.candidate_id,
                )
                .await;
        }

        let oracle_violations_json =
            serde_json::to_string(&decision.violations).context("encode oracle violations")?;
        let classification = decision.classification.as_str().to_owned();
        let result_digest = result_digest(
            &candidate,
            item.attempt_no,
            self.processing_generation,
            &canonical,
            &provider,
            &classification,
            &outcome_kind,
        );

        Ok(RecoveryWrite {
            run_id: self.run_id.clone(),
            candidate_id: candidate.candidate_id,
            attempt_no: item.attempt_no,
            processing_generation: self.processing_generation,
            attempt_state: attempt_state.into(),
            merchant_http_status,
            canonical_payment_state: canonical.payment_state,
            canonical_operation_state: canonical.operation_state,
            canonical_provider_payment_id: canonical.provider_payment_id,
            canonical_gateway_operation_id: canonical.gateway_operation_id,
            provider_visibility: provider.visibility.as_str().into(),
            provider_payment_id: provider.provider_payment_id,
            provider_currency: provider.currency,
            provider_status: provider.status,
            provider_authorized_amount_minor: provider.authorized_amount_minor,
            provider_captured_amount_minor: provider.captured_amount_minor,
            provider_refunded_amount_minor: provider.refunded_amount_minor,
            outcome_kind: outcome_kind.into(),
            classification,
            duplicate_provider_mutation_count: 0,
            result_digest,
            oracle_violations_json,
        })
    }

    async fn fetch_provider(
        &self,
        provider_payment_id: Option<&str>,
    ) -> Result<ProviderObservation> {
        let Some(provider_payment_id) = provider_payment_id else {
            return Ok(unavailable_provider(None));
        };
        let url = format!(
            "{}/payments/{}",
            self.provider_base_url,
            percent_encode_path_segment(provider_payment_id)
        );
        let response = match http::request_json("GET", &url, None, None).await {
            Ok(response) => response,
            Err(_) => return Ok(unavailable_provider(Some(provider_payment_id))),
        };

        match response.status {
            200 => {
                let visibility = match response
                    .headers
                    .get("x-payment-recovery-visibility")
                    .map(String::as_str)
                {
                    Some("STALE") => ProviderVisibility::Stale,
                    Some("IMMEDIATE") | None => ProviderVisibility::Immediate,
                    Some(_) => ProviderVisibility::Unavailable,
                };
                let response: ProviderPaymentResponse = serde_json::from_slice(&response.body)
                    .context("decode provider payment response")?;
                Ok(ProviderObservation {
                    visibility,
                    exists: true,
                    provider_payment_id: Some(response.id),
                    currency: Some(response.currency),
                    status: Some(response.status),
                    authorized_amount_minor: Some(response.authorized_amount),
                    captured_amount_minor: Some(response.captured_amount),
                    refunded_amount_minor: Some(response.refunded_amount),
                })
            }
            404 => Ok(ProviderObservation {
                visibility: ProviderVisibility::NotFound,
                exists: false,
                provider_payment_id: Some(provider_payment_id.to_owned()),
                currency: None,
                status: None,
                authorized_amount_minor: None,
                captured_amount_minor: None,
                refunded_amount_minor: None,
            }),
            _ => Ok(unavailable_provider(Some(provider_payment_id))),
        }
    }
}

impl ItemProcessor<RecoverySourceRow, RecoveryWrite> for RecoveryProcessor {
    async fn process(
        &self,
        item: &RecoverySourceRow,
        context: ProcessContext<'_>,
    ) -> Result<ProcessOutcome<RecoveryWrite>, ProcessorError> {
        if context.stop_token().is_stop_requested() {
            return Ok(ProcessOutcome::Stopped);
        }
        if item.attempt_no <= 0 {
            return Err(ProcessorError::with_category(FailureCategory::Invariant));
        }
        self.candidate(item)
            .map_err(|_| ProcessorError::with_category(FailureCategory::Invariant))?;
        self.process_one(item)
            .await
            .map(ProcessOutcome::Item)
            .map_err(|_| ProcessorError::with_category(FailureCategory::TransientInfrastructure))
    }
}

#[derive(Debug, Deserialize)]
struct ProviderPaymentResponse {
    id: String,
    currency: String,
    status: String,
    authorized_amount: i64,
    captured_amount: i64,
    refunded_amount: i64,
}

fn canonical_from_candidate(candidate: &CandidateSnapshot) -> CanonicalObservation {
    CanonicalObservation {
        merchant_payment_id: candidate.merchant_payment_id.clone(),
        merchant_order_id: candidate.merchant_order_id.clone(),
        operation_id: candidate.operation_id.clone(),
        payment_state: candidate.payment_state.as_str().into(),
        operation_state: candidate.operation_state.as_str().into(),
        provider_payment_id: candidate.provider_payment_id.clone(),
        gateway_operation_id: candidate.gateway_operation_id.clone(),
    }
}

fn canonical_from_view(view: &MerchantReplayView) -> CanonicalObservation {
    CanonicalObservation {
        merchant_payment_id: view.merchant_payment_id.clone(),
        merchant_order_id: view.merchant_order_id.clone(),
        operation_id: view.operation_id.clone(),
        payment_state: view.payment_state.clone(),
        operation_state: view.operation_state.clone(),
        provider_payment_id: view.mapping.provider_payment_id.clone(),
        gateway_operation_id: Some(view.mapping.gateway_operation_id.clone()),
    }
}

fn unavailable_provider(provider_payment_id: Option<&str>) -> ProviderObservation {
    ProviderObservation {
        visibility: ProviderVisibility::Unavailable,
        exists: false,
        provider_payment_id: provider_payment_id.map(str::to_owned),
        currency: None,
        status: None,
        authorized_amount_minor: None,
        captured_amount_minor: None,
        refunded_amount_minor: None,
    }
}

fn is_retryable_http_status(status: u16) -> bool {
    matches!(status, 408 | 425 | 429 | 500..=599)
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

fn result_digest(
    candidate: &CandidateSnapshot,
    attempt_no: i64,
    processing_generation: i64,
    canonical: &CanonicalObservation,
    provider: &ProviderObservation,
    classification: &str,
    outcome_kind: &str,
) -> String {
    let mut hasher = Sha256::new();
    for value in [
        candidate.candidate_id.as_str(),
        candidate.merchant_payment_id.as_str(),
        candidate.operation_id.as_str(),
        candidate.operation_kind.as_str(),
        candidate.idempotency_key_digest.as_str(),
        candidate.currency.as_str(),
        canonical.payment_state.as_str(),
        canonical.operation_state.as_str(),
        canonical.provider_payment_id.as_deref().unwrap_or(""),
        canonical.gateway_operation_id.as_deref().unwrap_or(""),
        provider.visibility.as_str(),
        provider.provider_payment_id.as_deref().unwrap_or(""),
        provider.currency.as_deref().unwrap_or(""),
        provider.status.as_deref().unwrap_or(""),
        classification,
        outcome_kind,
    ] {
        hasher.update(value.as_bytes());
        hasher.update([0]);
    }
    hasher.update(attempt_no.to_be_bytes());
    hasher.update(processing_generation.to_be_bytes());
    hasher.update(candidate.amount_minor.to_be_bytes());
    for value in [
        provider.authorized_amount_minor,
        provider.captured_amount_minor,
        provider.refunded_amount_minor,
    ] {
        match value {
            Some(value) => {
                hasher.update([1]);
                hasher.update(value.to_be_bytes());
            }
            None => hasher.update([0]),
        }
    }
    let digest: [u8; 32] = hasher.finalize().into();
    hex_digest(&digest)
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

    #[test]
    fn retryable_status_contract_matches_reference_merchant() {
        for status in [408, 425, 429, 500, 502, 503, 504, 599] {
            assert!(is_retryable_http_status(status));
        }
        for status in [400, 404, 409, 422] {
            assert!(!is_retryable_http_status(status));
        }
    }

    #[test]
    fn provider_path_segment_is_encoded() {
        assert_eq!(percent_encode_path_segment("provider/1"), "provider%2F1");
    }
}
