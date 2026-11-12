//! Canonical, provider-neutral payment contract shared by the reference Merchant
//! and Gateway. Provider-specific identifiers and request shapes must stay out of
//! this crate.

use std::fmt;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Currency(String);

impl Currency {
    pub fn parse(value: impl Into<String>) -> Result<Self, ContractError> {
        let value = value.into();
        if value.len() != 3 || !value.bytes().all(|b| b.is_ascii_uppercase()) {
            return Err(ContractError::InvalidCurrency(value));
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Money {
    pub amount_minor: i64,
    pub currency: Currency,
}

impl Money {
    pub fn new(amount_minor: i64, currency: Currency) -> Result<Self, ContractError> {
        if amount_minor <= 0 {
            return Err(ContractError::NonPositiveAmount(amount_minor));
        }
        Ok(Self {
            amount_minor,
            currency,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaymentOperationKind {
    Authorize,
    AuthCapture,
    Capture,
    Cancel,
    Refund,
}

impl PaymentOperationKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Authorize => "AUTHORIZE",
            Self::AuthCapture => "AUTH_CAPTURE",
            Self::Capture => "CAPTURE",
            Self::Cancel => "CANCEL",
            Self::Refund => "REFUND",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PaymentState {
    Created,
    PaymentPending,
    Authorized,
    PartiallyCaptured,
    Paid,
    Cancelled,
    PartiallyRefunded,
    Refunded,
    PaymentFailed,
    PaymentUnknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OperationState {
    Pending,
    Succeeded,
    Failed,
    Unknown,
    RetryPending,
    ManualReview,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PaymentMutationRequest {
    pub merchant_id: String,
    pub merchant_order_id: String,
    pub merchant_payment_id: String,
    pub operation_id: String,
    pub amount_minor: i64,
    pub currency: String,
    pub payment_method_token: Option<String>,
    pub idempotency_key: String,
    pub requested_at: Option<String>,
}

impl PaymentMutationRequest {
    pub fn into_canonical(
        self,
        operation: PaymentOperationKind,
    ) -> Result<CanonicalMutation, ContractError> {
        let money = Money::new(self.amount_minor, Currency::parse(self.currency)?)?;
        let mutation = CanonicalMutation {
            merchant_id: self.merchant_id,
            merchant_order_id: self.merchant_order_id,
            merchant_payment_id: self.merchant_payment_id,
            operation_id: self.operation_id,
            operation,
            money,
            payment_method_token: self.payment_method_token,
            idempotency_key: self.idempotency_key,
            requested_at: self.requested_at,
        };
        mutation.validate()?;
        Ok(mutation)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanonicalMutation {
    pub merchant_id: String,
    pub merchant_order_id: String,
    pub merchant_payment_id: String,
    pub operation_id: String,
    pub operation: PaymentOperationKind,
    pub money: Money,
    pub payment_method_token: Option<String>,
    pub idempotency_key: String,
    pub requested_at: Option<String>,
}

impl CanonicalMutation {
    pub fn validate(&self) -> Result<(), ContractError> {
        require_non_blank("merchant_id", &self.merchant_id)?;
        require_non_blank("merchant_order_id", &self.merchant_order_id)?;
        require_non_blank("merchant_payment_id", &self.merchant_payment_id)?;
        require_non_blank("operation_id", &self.operation_id)?;
        require_non_blank("idempotency_key", &self.idempotency_key)?;

        match self.operation {
            PaymentOperationKind::Authorize | PaymentOperationKind::AuthCapture => {
                let token = self.payment_method_token.as_deref().unwrap_or_default();
                require_non_blank("payment_method_token", token)?;
            }
            PaymentOperationKind::Capture
            | PaymentOperationKind::Cancel
            | PaymentOperationKind::Refund => {}
        }

        Ok(())
    }

    /// Idempotent replay is safe only when the same key refers to the same
    /// canonical business mutation. Request timing is transport metadata and is
    /// intentionally excluded from the comparison.
    pub fn same_idempotent_mutation_as(&self, other: &Self) -> bool {
        self.idempotency_key == other.idempotency_key
            && self.merchant_id == other.merchant_id
            && self.merchant_order_id == other.merchant_order_id
            && self.merchant_payment_id == other.merchant_payment_id
            && self.operation_id == other.operation_id
            && self.operation == other.operation
            && self.money == other.money
            && self.payment_method_token == other.payment_method_token
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ExternalPaymentMapping {
    pub merchant_payment_id: String,
    pub gateway_operation_id: String,
    pub killbill_payment_id: Option<String>,
    pub killbill_transaction_id: Option<String>,
    pub hyperswitch_payment_id: Option<String>,
    pub hyperswitch_attempt_id: Option<String>,
    pub provider_payment_id: Option<String>,
    pub provider_operation_id: Option<String>,
    pub provider_idempotency_key: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CanonicalPaymentView {
    pub merchant_payment_id: String,
    pub payment_state: PaymentState,
    pub last_operation_state: OperationState,
    pub mapping: ExternalPaymentMapping,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContractError {
    InvalidCurrency(String),
    NonPositiveAmount(i64),
    BlankField(&'static str),
}

impl fmt::Display for ContractError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidCurrency(value) => {
                write!(
                    f,
                    "currency must be three uppercase ASCII letters: {value:?}"
                )
            }
            Self::NonPositiveAmount(value) => {
                write!(f, "amount_minor must be positive: {value}")
            }
            Self::BlankField(field) => write!(f, "{field} must not be blank"),
        }
    }
}

impl std::error::Error for ContractError {}

fn require_non_blank(field: &'static str, value: &str) -> Result<(), ContractError> {
    if value.trim().is_empty() {
        return Err(ContractError::BlankField(field));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(key: &str, amount_minor: i64) -> PaymentMutationRequest {
        PaymentMutationRequest {
            merchant_id: "merchant-1".into(),
            merchant_order_id: "order-1".into(),
            merchant_payment_id: "payment-1".into(),
            operation_id: "operation-1".into(),
            amount_minor,
            currency: "KRW".into(),
            payment_method_token: Some("test-token-1".into()),
            idempotency_key: key.into(),
            requested_at: Some("2026-11-12T00:00:00Z".into()),
        }
    }

    #[test]
    fn money_rejects_non_positive_minor_units() {
        assert_eq!(
            Money::new(0, Currency::parse("KRW").unwrap()),
            Err(ContractError::NonPositiveAmount(0))
        );
    }

    #[test]
    fn currency_is_explicit_and_normalized() {
        assert!(Currency::parse("KRW").is_ok());
        assert!(Currency::parse("krw").is_err());
        assert!(Currency::parse("USDT").is_err());
    }

    #[test]
    fn authorization_requires_payment_method_token() {
        let mut request = request("idem-1", 10_000);
        request.payment_method_token = None;
        assert_eq!(
            request.into_canonical(PaymentOperationKind::Authorize),
            Err(ContractError::BlankField("payment_method_token"))
        );
    }

    #[test]
    fn same_idempotency_key_with_conflicting_amount_is_not_same_mutation() {
        let original = request("idem-1", 10_000)
            .into_canonical(PaymentOperationKind::Authorize)
            .unwrap();
        let conflict = request("idem-1", 20_000)
            .into_canonical(PaymentOperationKind::Authorize)
            .unwrap();
        assert!(!original.same_idempotent_mutation_as(&conflict));
    }

    #[test]
    fn exact_replay_is_same_idempotent_mutation_even_when_request_time_differs() {
        let original = request("idem-1", 10_000)
            .into_canonical(PaymentOperationKind::Authorize)
            .unwrap();
        let mut replay_request = request("idem-1", 10_000);
        replay_request.requested_at = Some("2026-11-12T00:00:03Z".into());
        let replay = replay_request
            .into_canonical(PaymentOperationKind::Authorize)
            .unwrap();

        assert!(original.same_idempotent_mutation_as(&replay));
    }
}
