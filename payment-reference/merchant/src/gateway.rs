use std::fmt;

use payment_reference_contract::{
    CanonicalPaymentView, PaymentMutationRequest, PaymentOperationKind,
};
use reqwest::{Client, Url};
use serde::Deserialize;

#[derive(Clone)]
pub struct GatewayClient {
    client: Client,
    base_url: Url,
}

#[derive(Debug)]
pub enum GatewayOutcome {
    View {
        status: u16,
        view: CanonicalPaymentView,
    },
    Rejected {
        status: u16,
        message: String,
    },
}

#[derive(Debug)]
pub enum GatewayClientError {
    InvalidBaseUrl(String),
    Transport(String),
    Protocol(String),
}

impl fmt::Display for GatewayClientError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidBaseUrl(message) => write!(f, "invalid Gateway base URL: {message}"),
            Self::Transport(message) => write!(f, "Gateway transport failure: {message}"),
            Self::Protocol(message) => write!(f, "Gateway protocol failure: {message}"),
        }
    }
}

impl std::error::Error for GatewayClientError {}

#[derive(Deserialize)]
struct GatewayErrorBody {
    error: String,
}

impl GatewayClient {
    pub fn new(base_url: &str) -> Result<Self, GatewayClientError> {
        let mut base_url = Url::parse(base_url)
            .map_err(|error| GatewayClientError::InvalidBaseUrl(error.to_string()))?;
        if !matches!(base_url.scheme(), "http" | "https") {
            return Err(GatewayClientError::InvalidBaseUrl(
                "scheme must be http or https".into(),
            ));
        }
        if base_url.host_str().is_none() {
            return Err(GatewayClientError::InvalidBaseUrl(
                "host is required".into(),
            ));
        }
        base_url.set_query(None);
        base_url.set_fragment(None);
        base_url.set_path("/");

        let client = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|error| GatewayClientError::Protocol(error.to_string()))?;

        Ok(Self { client, base_url })
    }

    pub async fn mutate(
        &self,
        operation: PaymentOperationKind,
        merchant_payment_id: &str,
        request: &PaymentMutationRequest,
    ) -> Result<GatewayOutcome, GatewayClientError> {
        let url = mutation_url(&self.base_url, operation, merchant_payment_id)?;
        let response = self
            .client
            .post(url)
            .json(request)
            .send()
            .await
            .map_err(|error| GatewayClientError::Transport(error.to_string()))?;

        decode_response(response).await
    }
}

fn mutation_url(
    base_url: &Url,
    operation: PaymentOperationKind,
    merchant_payment_id: &str,
) -> Result<Url, GatewayClientError> {
    let mut url = base_url.clone();
    {
        let mut segments = url.path_segments_mut().map_err(|_| {
            GatewayClientError::InvalidBaseUrl("URL cannot be used as a hierarchical base".into())
        })?;
        segments.clear();
        segments.push("payments");
        match operation {
            PaymentOperationKind::Authorize => {
                segments.push("authorize");
            }
            PaymentOperationKind::AuthCapture => {
                segments.push("auth-capture");
            }
            PaymentOperationKind::Capture => {
                segments.push(merchant_payment_id);
                segments.push("capture");
            }
            PaymentOperationKind::Cancel => {
                segments.push(merchant_payment_id);
                segments.push("cancel");
            }
            PaymentOperationKind::Refund => {
                segments.push(merchant_payment_id);
                segments.push("refund");
            }
        }
    }
    Ok(url)
}

async fn decode_response(response: reqwest::Response) -> Result<GatewayOutcome, GatewayClientError> {
    let status = response.status().as_u16();
    let body = response
        .bytes()
        .await
        .map_err(|error| GatewayClientError::Transport(error.to_string()))?;

    if matches!(status, 200 | 202) {
        let view = serde_json::from_slice::<CanonicalPaymentView>(&body).map_err(|error| {
            GatewayClientError::Protocol(format!(
                "Gateway returned HTTP {status} with an invalid canonical view: {error}"
            ))
        })?;
        return Ok(GatewayOutcome::View { status, view });
    }

    if status == 409 {
        if let Ok(view) = serde_json::from_slice::<CanonicalPaymentView>(&body) {
            return Ok(GatewayOutcome::View { status, view });
        }
    }

    let message = serde_json::from_slice::<GatewayErrorBody>(&body)
        .map(|body| body.error)
        .unwrap_or_else(|_| {
            String::from_utf8_lossy(&body)
                .trim()
                .chars()
                .take(512)
                .collect()
        });

    if message.is_empty() {
        return Err(GatewayClientError::Protocol(format!(
            "Gateway returned HTTP {status} without a canonical view or error body"
        )));
    }

    Ok(GatewayOutcome::Rejected { status, message })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mutation_paths_match_the_canonical_gateway_contract() {
        let base = Url::parse("http://gateway.internal:8080/ignored/prefix").unwrap();

        assert_eq!(
            mutation_url(&base, PaymentOperationKind::Authorize, "pay-1")
                .unwrap()
                .as_str(),
            "http://gateway.internal:8080/payments/authorize"
        );
        assert_eq!(
            mutation_url(&base, PaymentOperationKind::AuthCapture, "pay-1")
                .unwrap()
                .as_str(),
            "http://gateway.internal:8080/payments/auth-capture"
        );
        assert_eq!(
            mutation_url(&base, PaymentOperationKind::Capture, "pay/1")
                .unwrap()
                .as_str(),
            "http://gateway.internal:8080/payments/pay%2F1/capture"
        );
        assert_eq!(
            mutation_url(&base, PaymentOperationKind::Cancel, "pay-1")
                .unwrap()
                .as_str(),
            "http://gateway.internal:8080/payments/pay-1/cancel"
        );
        assert_eq!(
            mutation_url(&base, PaymentOperationKind::Refund, "pay-1")
                .unwrap()
                .as_str(),
            "http://gateway.internal:8080/payments/pay-1/refund"
        );
    }

    #[test]
    fn rejects_non_http_gateway_base_urls() {
        let error = GatewayClient::new("file:///tmp/gateway")
            .err()
            .expect("file URL must be rejected");
        assert!(matches!(error, GatewayClientError::InvalidBaseUrl(_)));
    }
}
