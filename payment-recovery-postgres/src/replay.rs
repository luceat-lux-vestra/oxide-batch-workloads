use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::candidate::{validate_candidate, CandidateSnapshot};
use crate::crash::{CrashControl, CrashPoint};
use crate::http::{self, AfterSendCrash};

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ExactReplayRequest {
    operation_id: String,
    amount_minor: i64,
    idempotency_key: String,
    requested_at: Option<String>,
}

impl ExactReplayRequest {
    pub fn from_candidate(candidate: &CandidateSnapshot) -> Result<Self> {
        validate_candidate(candidate)?;
        Ok(Self {
            operation_id: candidate.operation_id.clone(),
            amount_minor: candidate.amount_minor,
            idempotency_key: candidate.idempotency_key.clone(),
            // Merchant's canonical contract explicitly excludes requestedAt from
            // idempotency equivalence, and Merchant does not persist the original
            // transport timestamp. Recovery therefore omits it rather than inventing one.
            requested_at: None,
        })
    }

    pub fn assert_exact_identity(&self, candidate: &CandidateSnapshot) -> Result<()> {
        if self.operation_id != candidate.operation_id
            || self.amount_minor != candidate.amount_minor
            || self.idempotency_key != candidate.idempotency_key
        {
            bail!("replay request does not preserve the frozen Merchant operation identity");
        }
        Ok(())
    }
}

pub struct PreparedReplayCall {
    pub url: String,
    pub body: Vec<u8>,
}

pub struct MerchantReplayClient {
    base_url: String,
}

#[derive(Debug, Clone)]
pub enum ReplayTransportOutcome {
    Canonical {
        http_status: u16,
        view: Box<MerchantReplayView>,
    },
    Rejected {
        http_status: u16,
    },
    TransportUnknown,
    IncoherentResponse {
        http_status: u16,
    },
}

impl MerchantReplayClient {
    pub fn new(base_url: &str) -> Result<Self> {
        Ok(Self {
            base_url: http::normalize_http_base_url("Merchant", base_url)?,
        })
    }

    pub fn prepare(&self, candidate: &CandidateSnapshot) -> Result<PreparedReplayCall> {
        let request = ExactReplayRequest::from_candidate(candidate)?;
        request.assert_exact_identity(candidate)?;
        let payment_id = percent_encode_path_segment(&candidate.merchant_payment_id);
        let url = format!(
            "{}/payments/{}/{}",
            self.base_url,
            payment_id,
            candidate.operation_kind.merchant_path_suffix()
        );
        let body =
            serde_json::to_vec(&request).context("serialize exact Merchant replay request")?;
        Ok(PreparedReplayCall { url, body })
    }

    pub async fn execute(
        &self,
        candidate: &CandidateSnapshot,
        crash: &CrashControl,
    ) -> Result<ReplayTransportOutcome> {
        let call = self.prepare(candidate)?;
        let response = match http::request_json(
            "POST",
            &call.url,
            Some(&call.body),
            Some(AfterSendCrash {
                control: crash,
                point: CrashPoint::WhileReplayOutcomeUnknown,
                candidate_id: &candidate.candidate_id,
            }),
        )
        .await
        {
            Ok(response) => response,
            Err(_) => return Ok(ReplayTransportOutcome::TransportUnknown),
        };

        match self.decode_response(candidate, response.status, &response.body) {
            Ok(ReplayResponse::Canonical { http_status, view }) => {
                Ok(ReplayTransportOutcome::Canonical { http_status, view })
            }
            Ok(ReplayResponse::Rejected { http_status }) => {
                Ok(ReplayTransportOutcome::Rejected { http_status })
            }
            Err(_) => Ok(ReplayTransportOutcome::IncoherentResponse {
                http_status: response.status,
            }),
        }
    }

    pub fn decode_response(
        &self,
        candidate: &CandidateSnapshot,
        http_status: u16,
        body: &[u8],
    ) -> Result<ReplayResponse> {
        if matches!(http_status, 200 | 202) {
            let view: MerchantReplayView =
                serde_json::from_slice(body).context("decode Merchant replay response")?;
            validate_view_identity(candidate, &view)?;
            Ok(ReplayResponse::Canonical {
                http_status,
                view: Box::new(view),
            })
        } else {
            Ok(ReplayResponse::Rejected { http_status })
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MerchantReplayView {
    pub merchant_payment_id: String,
    pub merchant_order_id: String,
    pub payment_state: String,
    pub operation_id: String,
    pub operation_state: String,
    pub mapping: ReplayMapping,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReplayMapping {
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

#[derive(Debug, Clone)]
pub enum ReplayResponse {
    Canonical {
        http_status: u16,
        view: Box<MerchantReplayView>,
    },
    Rejected {
        http_status: u16,
    },
}

fn validate_view_identity(candidate: &CandidateSnapshot, view: &MerchantReplayView) -> Result<()> {
    if view.merchant_payment_id != candidate.merchant_payment_id
        || view.merchant_order_id != candidate.merchant_order_id
        || view.operation_id != candidate.operation_id
        || view.mapping.merchant_payment_id != candidate.merchant_payment_id
        || view.mapping.gateway_operation_id != candidate.operation_id
    {
        bail!("Merchant replay response identity conflicts with the frozen candidate");
    }
    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::candidate::{
        secret_digest, CandidateOperationState, CandidatePaymentState, InitialOperationKind,
    };

    fn candidate() -> CandidateSnapshot {
        let key = "idem-1".to_owned();
        let token = "test-token-1".to_owned();
        CandidateSnapshot {
            candidate_id: "candidate-1".into(),
            merchant_id: "merchant-1".into(),
            merchant_order_id: "order-1".into(),
            merchant_payment_id: "payment/1".into(),
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
            provider_payment_id: None,
            gateway_operation_id: None,
            source_updated_at: "2026-11-15 01:00:00+00".into(),
        }
    }

    #[test]
    fn prepared_replay_reuses_frozen_identity_and_encodes_payment_id() {
        let candidate = candidate();
        let client = MerchantReplayClient::new("http://merchant.internal:8081/").unwrap();
        let call = client.prepare(&candidate).unwrap();

        assert_eq!(
            call.url,
            "http://merchant.internal:8081/payments/payment%2F1/authorize"
        );
        let value: serde_json::Value = serde_json::from_slice(&call.body).unwrap();
        assert_eq!(value["operationId"], "operation-1");
        assert_eq!(value["amountMinor"], 10_000);
        assert_eq!(value["idempotencyKey"], "idem-1");
        assert!(value["requestedAt"].is_null());
    }

    #[test]
    fn canonical_response_identity_is_fail_closed() {
        let candidate = candidate();
        let client = MerchantReplayClient::new("http://merchant.internal:8081").unwrap();
        let view = serde_json::json!({
            "merchantPaymentId": "different-payment",
            "merchantOrderId": "order-1",
            "paymentState": "AUTHORIZED",
            "operationId": "operation-1",
            "operationState": "SUCCEEDED",
            "mapping": {
                "merchantPaymentId": "different-payment",
                "gatewayOperationId": "operation-1",
                "killbillPaymentId": null,
                "killbillTransactionId": null,
                "hyperswitchPaymentId": null,
                "hyperswitchAttemptId": null,
                "providerPaymentId": "provider-1",
                "providerOperationId": null,
                "providerIdempotencyKey": null
            }
        });
        assert!(client
            .decode_response(&candidate, 200, &serde_json::to_vec(&view).unwrap())
            .is_err());
    }
}
