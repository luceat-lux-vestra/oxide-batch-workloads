use serde::{Deserialize, Serialize};

use crate::candidate::{CandidateSnapshot, InitialOperationKind};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RecoveryClassification {
    ResolvedSucceeded,
    ResolvedFailed,
    RetryLater,
    HoldManual,
    NotEligible,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProviderVisibility {
    Immediate,
    NotFound,
    Stale,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CanonicalObservation {
    pub merchant_payment_id: String,
    pub merchant_order_id: String,
    pub operation_id: String,
    pub payment_state: String,
    pub operation_state: String,
    pub provider_payment_id: Option<String>,
    pub gateway_operation_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderObservation {
    pub visibility: ProviderVisibility,
    pub exists: bool,
    pub status: Option<String>,
    pub authorized_amount_minor: Option<i64>,
    pub captured_amount_minor: Option<i64>,
    pub refunded_amount_minor: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OracleDecision {
    pub classification: RecoveryClassification,
    pub violations: Vec<String>,
}

pub fn classify(
    candidate: &CandidateSnapshot,
    canonical: &CanonicalObservation,
    provider: &ProviderObservation,
    duplicate_provider_mutation_observed: bool,
) -> OracleDecision {
    let mut violations = identity_violations(candidate, canonical);

    if duplicate_provider_mutation_observed {
        violations.push("duplicate provider mutation observed".into());
    }

    if !violations.is_empty() {
        return OracleDecision {
            classification: RecoveryClassification::HoldManual,
            violations,
        };
    }

    if canonical.operation_state == "MANUAL_REVIEW" {
        return decision(RecoveryClassification::HoldManual);
    }

    if matches!(
        provider.visibility,
        ProviderVisibility::NotFound | ProviderVisibility::Stale
    ) {
        return decision(RecoveryClassification::RetryLater);
    }

    match canonical.operation_state.as_str() {
        "PENDING" | "UNKNOWN" | "RETRY_PENDING" => {
            decision(RecoveryClassification::RetryLater)
        }
        "SUCCEEDED" => classify_success(candidate, canonical, provider),
        "FAILED" => classify_failure(canonical, provider),
        _ => OracleDecision {
            classification: RecoveryClassification::HoldManual,
            violations: vec![format!(
                "unsupported canonical operation state: {}",
                canonical.operation_state
            )],
        },
    }
}

fn classify_success(
    candidate: &CandidateSnapshot,
    canonical: &CanonicalObservation,
    provider: &ProviderObservation,
) -> OracleDecision {
    if !provider.exists {
        return OracleDecision {
            classification: RecoveryClassification::HoldManual,
            violations: vec![
                "canonical success conflicts with authoritative provider absence".into(),
            ],
        };
    }

    let expected_payment_state = match candidate.operation_kind {
        InitialOperationKind::Authorize => "AUTHORIZED",
        InitialOperationKind::AuthCapture => "PAID",
    };
    if canonical.payment_state != expected_payment_state {
        return OracleDecision {
            classification: RecoveryClassification::HoldManual,
            violations: vec![format!(
                "successful {} replay has incompatible payment state {}",
                candidate.operation_kind.as_str(),
                canonical.payment_state
            )],
        };
    }

    let financial_match = match candidate.operation_kind {
        InitialOperationKind::Authorize => {
            provider.authorized_amount_minor == Some(candidate.amount_minor)
        }
        InitialOperationKind::AuthCapture => {
            provider.authorized_amount_minor == Some(candidate.amount_minor)
                && provider.captured_amount_minor == Some(candidate.amount_minor)
        }
    };

    if !financial_match {
        return OracleDecision {
            classification: RecoveryClassification::HoldManual,
            violations: vec!["provider financial truth does not match the frozen mutation".into()],
        };
    }

    decision(RecoveryClassification::ResolvedSucceeded)
}

fn classify_failure(
    canonical: &CanonicalObservation,
    provider: &ProviderObservation,
) -> OracleDecision {
    if canonical.payment_state != "PAYMENT_FAILED" {
        return OracleDecision {
            classification: RecoveryClassification::HoldManual,
            violations: vec![format!(
                "canonical FAILED operation has incompatible payment state {}",
                canonical.payment_state
            )],
        };
    }

    let no_successful_effect = !provider.exists
        || (provider.status.as_deref() == Some("failed")
            && provider.authorized_amount_minor.unwrap_or(0) == 0
            && provider.captured_amount_minor.unwrap_or(0) == 0
            && provider.refunded_amount_minor.unwrap_or(0) == 0);

    if no_successful_effect {
        decision(RecoveryClassification::ResolvedFailed)
    } else {
        OracleDecision {
            classification: RecoveryClassification::HoldManual,
            violations: vec![
                "canonical failure conflicts with provider financial side-effect evidence".into(),
            ],
        }
    }
}

fn identity_violations(
    candidate: &CandidateSnapshot,
    canonical: &CanonicalObservation,
) -> Vec<String> {
    let mut violations = Vec::new();
    if canonical.merchant_payment_id != candidate.merchant_payment_id {
        violations.push("merchant payment identity mismatch".into());
    }
    if canonical.merchant_order_id != candidate.merchant_order_id {
        violations.push("merchant order identity mismatch".into());
    }
    if canonical.operation_id != candidate.operation_id {
        violations.push("operation identity mismatch".into());
    }
    if canonical.gateway_operation_id.as_deref() != Some(candidate.operation_id.as_str()) {
        violations.push("gateway operation identity mismatch".into());
    }
    if let (Some(expected), Some(observed)) = (
        candidate.provider_payment_id.as_deref(),
        canonical.provider_payment_id.as_deref(),
    ) {
        if expected != observed {
            violations.push("provider payment identity mismatch".into());
        }
    }
    violations
}

fn decision(classification: RecoveryClassification) -> OracleDecision {
    OracleDecision {
        classification,
        violations: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::candidate::{
        secret_digest, CandidateOperationState, CandidatePaymentState, InitialOperationKind,
    };

    fn candidate(kind: InitialOperationKind) -> CandidateSnapshot {
        let key = "idem-1".to_owned();
        let token = "test-token-1".to_owned();
        CandidateSnapshot {
            candidate_id: "candidate-1".into(),
            merchant_id: "merchant-1".into(),
            merchant_order_id: "order-1".into(),
            merchant_payment_id: "payment-1".into(),
            operation_id: "operation-1".into(),
            operation_kind: kind,
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

    fn canonical(payment_state: &str, operation_state: &str) -> CanonicalObservation {
        CanonicalObservation {
            merchant_payment_id: "payment-1".into(),
            merchant_order_id: "order-1".into(),
            operation_id: "operation-1".into(),
            payment_state: payment_state.into(),
            operation_state: operation_state.into(),
            provider_payment_id: Some("provider-1".into()),
            gateway_operation_id: Some("operation-1".into()),
        }
    }

    #[test]
    fn stale_provider_visibility_remains_retry_later() {
        let decision = classify(
            &candidate(InitialOperationKind::Authorize),
            &canonical("PAYMENT_UNKNOWN", "UNKNOWN"),
            &ProviderObservation {
                visibility: ProviderVisibility::Stale,
                exists: true,
                status: Some("authorized".into()),
                authorized_amount_minor: Some(10_000),
                captured_amount_minor: Some(0),
                refunded_amount_minor: Some(0),
            },
            false,
        );
        assert_eq!(
            decision.classification,
            RecoveryClassification::RetryLater
        );
    }

    #[test]
    fn authorize_success_requires_matching_provider_truth() {
        let decision = classify(
            &candidate(InitialOperationKind::Authorize),
            &canonical("AUTHORIZED", "SUCCEEDED"),
            &ProviderObservation {
                visibility: ProviderVisibility::Immediate,
                exists: true,
                status: Some("authorized".into()),
                authorized_amount_minor: Some(10_000),
                captured_amount_minor: Some(0),
                refunded_amount_minor: Some(0),
            },
            false,
        );
        assert_eq!(
            decision.classification,
            RecoveryClassification::ResolvedSucceeded
        );
        assert!(decision.violations.is_empty());
    }

    #[test]
    fn duplicate_provider_mutation_fails_closed() {
        let decision = classify(
            &candidate(InitialOperationKind::AuthCapture),
            &canonical("PAID", "SUCCEEDED"),
            &ProviderObservation {
                visibility: ProviderVisibility::Immediate,
                exists: true,
                status: Some("captured".into()),
                authorized_amount_minor: Some(10_000),
                captured_amount_minor: Some(10_000),
                refunded_amount_minor: Some(0),
            },
            true,
        );
        assert_eq!(decision.classification, RecoveryClassification::HoldManual);
        assert!(!decision.violations.is_empty());
    }

    #[test]
    fn authoritative_failure_requires_zero_financial_effect() {
        let decision = classify(
            &candidate(InitialOperationKind::Authorize),
            &canonical("PAYMENT_FAILED", "FAILED"),
            &ProviderObservation {
                visibility: ProviderVisibility::Immediate,
                exists: true,
                status: Some("failed".into()),
                authorized_amount_minor: Some(0),
                captured_amount_minor: Some(0),
                refunded_amount_minor: Some(0),
            },
            false,
        );
        assert_eq!(
            decision.classification,
            RecoveryClassification::ResolvedFailed
        );
    }
}
