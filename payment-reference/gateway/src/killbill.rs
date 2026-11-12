use std::fmt;
use std::time::Duration;

use payment_reference_contract::{OperationState, PaymentOperationKind, PaymentState};
use reqwest::header::{ACCEPT, LOCATION};
use reqwest::{Method, StatusCode, Url};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::Number;
use sha2::{Digest, Sha256};
use uuid::Uuid;

const CREATED_BY: &str = "oxide-batch-payment-reference-gateway";
const PAYMENT_PLUGIN_NAME: &str = "killbill-payment-sandbox";
const ERROR_BODY_LIMIT: usize = 4096;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KillBillClientErrorKind {
    InvalidConfiguration,
    UnsupportedCurrency,
    InvalidAmount,
    Transport,
    Http,
    Decode,
    MissingLocation,
    CrossOriginLocation,
    Invariant,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillBillClientError {
    pub kind: KillBillClientErrorKind,
    pub message: String,
    pub status: Option<u16>,
    pub location: Option<String>,
    pub requires_read_before_retry: bool,
}

impl KillBillClientError {
    fn new(kind: KillBillClientErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            status: None,
            location: None,
            requires_read_before_retry: false,
        }
    }

    fn transport(error: reqwest::Error, side_effecting: bool) -> Self {
        Self {
            kind: KillBillClientErrorKind::Transport,
            message: error.to_string(),
            status: None,
            location: None,
            requires_read_before_retry: side_effecting,
        }
    }

    fn decode(error: impl fmt::Display, side_effecting: bool) -> Self {
        Self {
            kind: KillBillClientErrorKind::Decode,
            message: error.to_string(),
            status: None,
            location: None,
            requires_read_before_retry: side_effecting,
        }
    }

    fn invariant(message: impl Into<String>) -> Self {
        Self::new(KillBillClientErrorKind::Invariant, message)
    }

    fn mark_read_before_retry(mut self) -> Self {
        self.requires_read_before_retry = true;
        self
    }
}

impl fmt::Display for KillBillClientError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}: {}", self.kind, self.message)
    }
}

impl std::error::Error for KillBillClientError {}

#[derive(Clone)]
pub struct KillBillClientConfig {
    base_url: Url,
    username: String,
    password: String,
    api_key: String,
    api_secret: String,
    timeout: Duration,
}

impl KillBillClientConfig {
    pub fn new(
        base_url: &str,
        username: impl Into<String>,
        password: impl Into<String>,
        api_key: impl Into<String>,
        api_secret: impl Into<String>,
    ) -> Result<Self, KillBillClientError> {
        let mut base_url = Url::parse(base_url).map_err(|error| {
            KillBillClientError::new(
                KillBillClientErrorKind::InvalidConfiguration,
                format!("invalid Kill Bill base URL: {error}"),
            )
        })?;
        if !matches!(base_url.scheme(), "http" | "https") {
            return Err(KillBillClientError::new(
                KillBillClientErrorKind::InvalidConfiguration,
                "Kill Bill base URL must use http or https",
            ));
        }
        if base_url.query().is_some() || base_url.fragment().is_some() {
            return Err(KillBillClientError::new(
                KillBillClientErrorKind::InvalidConfiguration,
                "Kill Bill base URL must not contain query or fragment components",
            ));
        }
        if !base_url.path().ends_with('/') {
            let path = format!("{}/", base_url.path());
            base_url.set_path(&path);
        }

        let username = username.into();
        let password = password.into();
        let api_key = api_key.into();
        let api_secret = api_secret.into();
        for (name, value) in [
            ("username", username.as_str()),
            ("password", password.as_str()),
            ("api_key", api_key.as_str()),
            ("api_secret", api_secret.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(KillBillClientError::new(
                    KillBillClientErrorKind::InvalidConfiguration,
                    format!("Kill Bill {name} must not be blank"),
                ));
            }
        }

        Ok(Self {
            base_url,
            username,
            password,
            api_key,
            api_secret,
            timeout: Duration::from_secs(10),
        })
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Result<Self, KillBillClientError> {
        if timeout.is_zero() {
            return Err(KillBillClientError::new(
                KillBillClientErrorKind::InvalidConfiguration,
                "Kill Bill timeout must be positive",
            ));
        }
        self.timeout = timeout;
        Ok(self)
    }
}

#[derive(Clone)]
pub struct KillBillClient {
    http: reqwest::Client,
    config: KillBillClientConfig,
}

impl KillBillClient {
    pub fn new(config: KillBillClientConfig) -> Result<Self, KillBillClientError> {
        let http = reqwest::Client::builder()
            .timeout(config.timeout)
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|error| {
                KillBillClientError::new(
                    KillBillClientErrorKind::InvalidConfiguration,
                    format!("unable to construct Kill Bill HTTP client: {error}"),
                )
            })?;
        Ok(Self { http, config })
    }

    pub async fn get_account_by_external_key(
        &self,
        external_key: &str,
    ) -> Result<Option<KillBillAccount>, KillBillClientError> {
        let url = self.endpoint("1.0/kb/accounts")?;
        let response = self
            .send(
                self.request(Method::GET, url)
                    .query(&[("externalKey", external_key)]),
                false,
            )
            .await?;
        if response.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        self.decode_success(response, false).await.map(Some)
    }

    pub async fn create_account(
        &self,
        request: &KillBillCreateAccountRequest,
    ) -> Result<KillBillAccount, KillBillClientError> {
        let url = self.endpoint("1.0/kb/accounts")?;
        self.mutate_location_then_get(self.request(Method::POST, url).json(request), false)
            .await
    }

    pub async fn list_payment_methods(
        &self,
        account_id: Uuid,
    ) -> Result<Vec<KillBillPaymentMethod>, KillBillClientError> {
        let url = self.endpoint(&format!(
            "1.0/kb/accounts/{account_id}/paymentMethods"
        ))?;
        let response = self.send(self.request(Method::GET, url), false).await?;
        self.decode_success(response, false).await
    }

    pub async fn create_payment_method(
        &self,
        account_id: Uuid,
        external_key: impl Into<String>,
    ) -> Result<KillBillPaymentMethod, KillBillClientError> {
        let url = self.endpoint(&format!(
            "1.0/kb/accounts/{account_id}/paymentMethods"
        ))?;
        let request = KillBillCreatePaymentMethodRequest {
            external_key: external_key.into(),
            plugin_name: PAYMENT_PLUGIN_NAME.to_owned(),
        };
        self.mutate_location_then_get(
            self.request(Method::POST, url)
                .query(&[("isDefault", "false")])
                .json(&request),
            false,
        )
        .await
    }

    pub async fn create_initial_payment(
        &self,
        account_id: Uuid,
        payment_method_id: Uuid,
        transaction_type: KillBillInitialTransactionType,
        mutation: &KillBillMutation,
    ) -> Result<KillBillPayment, KillBillClientError> {
        let url = self.endpoint(&format!("1.0/kb/accounts/{account_id}/payments"))?;
        let body = InitialPaymentRequest::from_mutation(transaction_type, mutation)?;
        self.mutate_location_then_get(
            self.request(Method::POST, url)
                .query(&[("paymentMethodId", payment_method_id.to_string())])
                .json(&body),
            true,
        )
        .await
    }

    pub async fn capture_payment(
        &self,
        payment_id: Uuid,
        mutation: &KillBillMutation,
    ) -> Result<KillBillPayment, KillBillClientError> {
        let url = self.endpoint(&format!("1.0/kb/payments/{payment_id}/"))?;
        let body = FollowUpPaymentRequest::from_mutation(mutation)?;
        self.mutate_location_then_get(self.request(Method::POST, url).json(&body), true)
            .await
    }

    pub async fn refund_payment(
        &self,
        payment_id: Uuid,
        mutation: &KillBillMutation,
    ) -> Result<KillBillPayment, KillBillClientError> {
        let url = self.endpoint(&format!("1.0/kb/payments/{payment_id}/refunds"))?;
        let body = FollowUpPaymentRequest::from_mutation(mutation)?;
        self.mutate_location_then_get(self.request(Method::POST, url).json(&body), true)
            .await
    }

    pub async fn void_payment(
        &self,
        payment_id: Uuid,
        payment_external_key: &str,
        transaction_external_key: &str,
    ) -> Result<KillBillPayment, KillBillClientError> {
        let url = self.endpoint(&format!("1.0/kb/payments/{payment_id}/"))?;
        let body = VoidPaymentRequest {
            payment_external_key: payment_external_key.to_owned(),
            transaction_external_key: transaction_external_key.to_owned(),
        };
        let response = self
            .send(self.request(Method::DELETE, url).json(&body), true)
            .await?;
        if !response.status().is_success() {
            return Err(self.error_from_response(response, true).await);
        }
        self.get_payment_by_id(payment_id)
            .await
            .map_err(KillBillClientError::mark_read_before_retry)
    }

    pub async fn get_payment_by_id(
        &self,
        payment_id: Uuid,
    ) -> Result<KillBillPayment, KillBillClientError> {
        let url = self.endpoint(&format!("1.0/kb/payments/{payment_id}/"))?;
        let response = self
            .send(
                self.request(Method::GET, url)
                    .query(&[("withPluginInfo", "true")]),
                false,
            )
            .await?;
        self.decode_success(response, false).await
    }

    pub async fn get_payment_by_external_key(
        &self,
        external_key: &str,
    ) -> Result<Option<KillBillPayment>, KillBillClientError> {
        let url = self.endpoint("1.0/kb/payments")?;
        let response = self
            .send(
                self.request(Method::GET, url).query(&[
                    ("externalKey", external_key),
                    ("withPluginInfo", "true"),
                ]),
                false,
            )
            .await?;
        if response.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        self.decode_success(response, false).await.map(Some)
    }

    fn request(&self, method: Method, url: Url) -> reqwest::RequestBuilder {
        self.http
            .request(method, url)
            .basic_auth(&self.config.username, Some(&self.config.password))
            .header("X-Killbill-ApiKey", &self.config.api_key)
            .header("X-Killbill-ApiSecret", &self.config.api_secret)
            .header("X-Killbill-CreatedBy", CREATED_BY)
            .header(ACCEPT, "application/json")
    }

    fn endpoint(&self, path: &str) -> Result<Url, KillBillClientError> {
        self.config.base_url.join(path).map_err(|error| {
            KillBillClientError::new(
                KillBillClientErrorKind::InvalidConfiguration,
                format!("unable to build Kill Bill URL: {error}"),
            )
        })
    }

    async fn send(
        &self,
        request: reqwest::RequestBuilder,
        side_effecting: bool,
    ) -> Result<reqwest::Response, KillBillClientError> {
        request
            .send()
            .await
            .map_err(|error| KillBillClientError::transport(error, side_effecting))
    }

    async fn decode_success<T: DeserializeOwned>(
        &self,
        response: reqwest::Response,
        side_effecting: bool,
    ) -> Result<T, KillBillClientError> {
        if !response.status().is_success() {
            return Err(self.error_from_response(response, side_effecting).await);
        }
        let bytes = response
            .bytes()
            .await
            .map_err(|error| KillBillClientError::transport(error, side_effecting))?;
        serde_json::from_slice(&bytes)
            .map_err(|error| KillBillClientError::decode(error, side_effecting))
    }

    async fn mutate_location_then_get<T: DeserializeOwned>(
        &self,
        request: reqwest::RequestBuilder,
        with_plugin_info: bool,
    ) -> Result<T, KillBillClientError> {
        let response = self.send(request, true).await?;
        if !response.status().is_success() {
            return Err(self.error_from_response(response, true).await);
        }
        let location = response
            .headers()
            .get(LOCATION)
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| {
                KillBillClientError {
                    kind: KillBillClientErrorKind::MissingLocation,
                    message: "successful Kill Bill mutation omitted Location".into(),
                    status: Some(response.status().as_u16()),
                    location: None,
                    requires_read_before_retry: true,
                }
            })?;
        let mut location = self.resolve_location(location)?;
        if with_plugin_info {
            location
                .query_pairs_mut()
                .append_pair("withPluginInfo", "true");
        }
        let response = self
            .send(self.request(Method::GET, location), false)
            .await
            .map_err(KillBillClientError::mark_read_before_retry)?;
        self.decode_success(response, false)
            .await
            .map_err(KillBillClientError::mark_read_before_retry)
    }

    fn resolve_location(&self, location: &str) -> Result<Url, KillBillClientError> {
        let resolved = self.config.base_url.join(location).map_err(|error| {
            KillBillClientError::new(
                KillBillClientErrorKind::MissingLocation,
                format!("invalid Kill Bill Location header: {error}"),
            )
            .mark_read_before_retry()
        })?;
        if resolved.origin() != self.config.base_url.origin() {
            return Err(KillBillClientError {
                kind: KillBillClientErrorKind::CrossOriginLocation,
                message: "Kill Bill Location points to a different origin".into(),
                status: None,
                location: Some(resolved.to_string()),
                requires_read_before_retry: true,
            });
        }
        Ok(resolved)
    }

    async fn error_from_response(
        &self,
        response: reqwest::Response,
        side_effecting: bool,
    ) -> KillBillClientError {
        let status = response.status();
        let location = response
            .headers()
            .get(LOCATION)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let body = match response.bytes().await {
            Ok(bytes) => {
                let visible = &bytes[..bytes.len().min(ERROR_BODY_LIMIT)];
                String::from_utf8_lossy(visible).into_owned()
            }
            Err(error) => format!("<unable to read response body: {error}>"),
        };
        let requires_read_before_retry = side_effecting
            && (status == StatusCode::PAYMENT_REQUIRED
                || status == StatusCode::BAD_GATEWAY
                || status == StatusCode::SERVICE_UNAVAILABLE
                || status == StatusCode::GATEWAY_TIMEOUT
                || status.is_server_error());
        KillBillClientError {
            kind: KillBillClientErrorKind::Http,
            message: format!("Kill Bill returned HTTP {}: {}", status.as_u16(), body),
            status: Some(status.as_u16()),
            location,
            requires_read_before_retry,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillBillMutation {
    pub amount_minor: i64,
    pub currency: String,
    pub payment_external_key: String,
    pub transaction_external_key: String,
}

impl KillBillMutation {
    pub fn new(
        amount_minor: i64,
        currency: impl Into<String>,
        payment_external_key: impl Into<String>,
        transaction_external_key: impl Into<String>,
    ) -> Result<Self, KillBillClientError> {
        if amount_minor <= 0 {
            return Err(KillBillClientError::new(
                KillBillClientErrorKind::InvalidAmount,
                "Kill Bill mutation amount must be positive",
            ));
        }
        let currency = currency.into();
        currency_scale(&currency)?;
        Ok(Self {
            amount_minor,
            currency,
            payment_external_key: payment_external_key.into(),
            transaction_external_key: transaction_external_key.into(),
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum KillBillInitialTransactionType {
    Authorize,
    Purchase,
}

impl TryFrom<PaymentOperationKind> for KillBillInitialTransactionType {
    type Error = KillBillClientError;

    fn try_from(value: PaymentOperationKind) -> Result<Self, Self::Error> {
        match value {
            PaymentOperationKind::Authorize => Ok(Self::Authorize),
            PaymentOperationKind::AuthCapture => Ok(Self::Purchase),
            _ => Err(KillBillClientError::invariant(format!(
                "{} is not an initial Kill Bill transaction",
                value.as_str()
            ))),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KillBillCreateAccountRequest {
    pub external_key: String,
    pub name: String,
    pub email: String,
    pub currency: String,
    pub time_zone: String,
}

impl KillBillCreateAccountRequest {
    pub fn reference(
        external_key: impl Into<String>,
        currency: impl Into<String>,
    ) -> Result<Self, KillBillClientError> {
        let currency = currency.into();
        currency_scale(&currency)?;
        Ok(Self {
            external_key: external_key.into(),
            name: "oxide-batch-payment-reference".into(),
            email: "payment-reference@oxide-batch.invalid".into(),
            currency,
            time_zone: "UTC".into(),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
struct KillBillCreatePaymentMethodRequest {
    external_key: String,
    plugin_name: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
struct InitialPaymentRequest {
    transaction_type: KillBillInitialTransactionType,
    amount: Number,
    currency: String,
    payment_external_key: String,
    transaction_external_key: String,
}

impl InitialPaymentRequest {
    fn from_mutation(
        transaction_type: KillBillInitialTransactionType,
        mutation: &KillBillMutation,
    ) -> Result<Self, KillBillClientError> {
        Ok(Self {
            transaction_type,
            amount: minor_units_to_major_number(mutation.amount_minor, &mutation.currency)?,
            currency: mutation.currency.clone(),
            payment_external_key: mutation.payment_external_key.clone(),
            transaction_external_key: mutation.transaction_external_key.clone(),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
struct FollowUpPaymentRequest {
    amount: Number,
    currency: String,
    payment_external_key: String,
    transaction_external_key: String,
}

impl FollowUpPaymentRequest {
    fn from_mutation(mutation: &KillBillMutation) -> Result<Self, KillBillClientError> {
        Ok(Self {
            amount: minor_units_to_major_number(mutation.amount_minor, &mutation.currency)?,
            currency: mutation.currency.clone(),
            payment_external_key: mutation.payment_external_key.clone(),
            transaction_external_key: mutation.transaction_external_key.clone(),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
struct VoidPaymentRequest {
    payment_external_key: String,
    transaction_external_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct KillBillAccount {
    pub account_id: Uuid,
    pub external_key: String,
    pub currency: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct KillBillPaymentMethod {
    pub payment_method_id: Uuid,
    pub external_key: String,
    pub account_id: Uuid,
    pub plugin_name: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct KillBillPayment {
    pub account_id: Uuid,
    pub payment_id: Uuid,
    pub payment_external_key: String,
    pub auth_amount: Option<Number>,
    pub captured_amount: Option<Number>,
    pub purchased_amount: Option<Number>,
    pub refunded_amount: Option<Number>,
    pub currency: Option<String>,
    pub payment_method_id: Option<Uuid>,
    #[serde(default)]
    pub transactions: Vec<KillBillTransaction>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct KillBillTransaction {
    pub transaction_id: Uuid,
    pub transaction_external_key: Option<String>,
    pub transaction_type: String,
    pub status: KillBillTransactionStatus,
    pub amount: Option<Number>,
    pub currency: Option<String>,
    pub gateway_error_code: Option<String>,
    pub gateway_error_msg: Option<String>,
    pub first_payment_reference_id: Option<String>,
    pub second_payment_reference_id: Option<String>,
    #[serde(default)]
    pub properties: Vec<KillBillPluginProperty>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct KillBillPluginProperty {
    pub key: String,
    pub value: Option<String>,
    #[serde(default)]
    pub is_updatable: Option<bool>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum KillBillTransactionStatus {
    Success,
    Unknown,
    Pending,
    PaymentFailure,
    PluginFailure,
    PaymentSystemOff,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillBillMappedOutcome {
    pub killbill_payment_id: Uuid,
    pub killbill_transaction_id: Uuid,
    pub operation_state: OperationState,
    pub payment_state: Option<PaymentState>,
    pub provider_payment_id: Option<String>,
    pub provider_operation_id: Option<String>,
    pub provider_idempotency_key: Option<String>,
}

pub fn map_payment_outcome(
    payment: &KillBillPayment,
    expected_payment_external_key: &str,
    expected_transaction_external_key: &str,
    operation: PaymentOperationKind,
    expected_amount_minor: Option<i64>,
    expected_currency: &str,
) -> Result<KillBillMappedOutcome, KillBillClientError> {
    currency_scale(expected_currency)?;
    if payment.payment_external_key != expected_payment_external_key {
        return Err(KillBillClientError::invariant(format!(
            "Kill Bill payment external key mismatch: expected {expected_payment_external_key}, got {}",
            payment.payment_external_key
        )));
    }
    if payment.currency.as_deref() != Some(expected_currency) {
        return Err(KillBillClientError::invariant(format!(
            "Kill Bill payment currency mismatch: expected {expected_currency}, got {:?}",
            payment.currency
        )));
    }

    let matching: Vec<&KillBillTransaction> = payment
        .transactions
        .iter()
        .filter(|transaction| {
            transaction.transaction_external_key.as_deref()
                == Some(expected_transaction_external_key)
        })
        .collect();
    let transaction = match matching.as_slice() {
        [transaction] => *transaction,
        [] => {
            return Err(KillBillClientError::invariant(format!(
                "Kill Bill payment does not contain transactionExternalKey {expected_transaction_external_key}"
            )));
        }
        _ => {
            return Err(KillBillClientError::invariant(format!(
                "Kill Bill payment contains duplicate transactionExternalKey {expected_transaction_external_key}"
            )));
        }
    };

    let expected_type = expected_transaction_type(operation);
    if transaction.transaction_type != expected_type {
        return Err(KillBillClientError::invariant(format!(
            "Kill Bill transaction type mismatch: expected {expected_type}, got {}",
            transaction.transaction_type
        )));
    }
    if transaction.currency.as_deref() != Some(expected_currency) {
        return Err(KillBillClientError::invariant(format!(
            "Kill Bill transaction currency mismatch: expected {expected_currency}, got {:?}",
            transaction.currency
        )));
    }
    if let Some(expected_amount_minor) = expected_amount_minor {
        let actual_amount = transaction.amount.as_ref().ok_or_else(|| {
            KillBillClientError::invariant("Kill Bill transaction omitted amount")
        })?;
        let actual_amount_minor = major_number_to_minor_units(actual_amount, expected_currency)?;
        if actual_amount_minor != expected_amount_minor {
            return Err(KillBillClientError::invariant(format!(
                "Kill Bill transaction amount mismatch: expected {expected_amount_minor} minor units, got {actual_amount_minor}"
            )));
        }
    }

    let (operation_state, payment_state) = match transaction.status {
        KillBillTransactionStatus::Success => (
            OperationState::Succeeded,
            Some(success_payment_state(payment, operation, expected_currency)?),
        ),
        KillBillTransactionStatus::Pending => (OperationState::Pending, None),
        KillBillTransactionStatus::Unknown => {
            (OperationState::Unknown, Some(PaymentState::PaymentUnknown))
        }
        KillBillTransactionStatus::PaymentFailure => (
            OperationState::Failed,
            match operation {
                PaymentOperationKind::Authorize | PaymentOperationKind::AuthCapture => {
                    Some(PaymentState::PaymentFailed)
                }
                PaymentOperationKind::Capture
                | PaymentOperationKind::Cancel
                | PaymentOperationKind::Refund => None,
            },
        )
        KillBillTransactionStatus::PluginFailure
        | KillBillTransactionStatus::PaymentSystemOff => (OperationState::RetryPending, None),
    };

    Ok(KillBillMappedOutcome {
        killbill_payment_id: payment.payment_id,
        killbill_transaction_id: transaction.transaction_id,
        operation_state,
        payment_state,
        provider_payment_id: transaction.first_payment_reference_id.clone(),
        provider_operation_id: None,
        provider_idempotency_key: transaction.second_payment_reference_id.clone(),
    })
}

fn success_payment_state(
    payment: &KillBillPayment,
    operation: PaymentOperationKind,
    currency: &str,
) -> Result<PaymentState, KillBillClientError> {
    match operation {
        PaymentOperationKind::Authorize => Ok(PaymentState::Authorized),
        PaymentOperationKind::AuthCapture => Ok(PaymentState::Paid),
        PaymentOperationKind::Cancel => Ok(PaymentState::Cancelled),
        PaymentOperationKind::Capture => {
            let auth = required_minor(payment.auth_amount.as_ref(), currency, "authAmount")?;
            let captured =
                required_minor(payment.captured_amount.as_ref(), currency, "capturedAmount")?;
            if captured > auth {
                return Err(KillBillClientError::invariant(
                    "Kill Bill capturedAmount exceeds authAmount",
                ));
            }
            if captured < auth {
                Ok(PaymentState::PartiallyCaptured)
            } else {
                Ok(PaymentState::Paid)
            }
        }
        PaymentOperationKind::Refund => {
            let purchased = optional_minor(payment.purchased_amount.as_ref(), currency)?;
            let captured = optional_minor(payment.captured_amount.as_ref(), currency)?;
            let refunded =
                required_minor(payment.refunded_amount.as_ref(), currency, "refundedAmount")?;
            let paid = purchased.checked_add(captured).ok_or_else(|| {
                KillBillClientError::invariant("Kill Bill paid amount overflow")
            })?;
            if paid <= 0 {
                return Err(KillBillClientError::invariant(
                    "Kill Bill refund succeeded without a positive paid amount",
                ));
            }
            if refunded > paid {
                return Err(KillBillClientError::invariant(
                    "Kill Bill refundedAmount exceeds paid amount",
                ));
            }
            if refunded < paid {
                Ok(PaymentState::PartiallyRefunded)
            } else {
                Ok(PaymentState::Refunded)
            }
        }
    }
}

fn required_minor(
    number: Option<&Number>,
    currency: &str,
    field: &str,
) -> Result<i64, KillBillClientError> {
    let number =
        number.ok_or_else(|| KillBillClientError::invariant(format!("Kill Bill omitted {field}")))?;
    major_number_to_minor_units(number, currency)
}

fn optional_minor(number: Option<&Number>, currency: &str) -> Result<i64, KillBillClientError> {
    match number {
        Some(number) => major_number_to_minor_units(number, currency),
        None => Ok(0),
    }
}

fn expected_transaction_type(operation: PaymentOperationKind) -> &'static str {
    match operation {
        PaymentOperationKind::Authorize => "AUTHORIZE",
        PaymentOperationKind::AuthCapture => "PURCHASE",
        PaymentOperationKind::Capture => "CAPTURE",
        PaymentOperationKind::Cancel => "VOID",
        PaymentOperationKind::Refund => "REFUND",
    }
}

pub fn account_external_key(merchant_id: &str, currency: &str) -> String {
    deterministic_external_key("pr-acct-v1-", &[merchant_id, currency])
}

pub fn payment_method_external_key(
    merchant_id: &str,
    currency: &str,
    payment_method_token: &str,
) -> String {
    deterministic_external_key(
        "pr-pm-v1-",
        &[merchant_id, currency, payment_method_token],
    )
}

pub fn payment_external_key(merchant_id: &str, merchant_payment_id: &str) -> String {
    deterministic_external_key("pr-pay-v1-", &[merchant_id, merchant_payment_id])
}

pub fn transaction_external_key(merchant_id: &str, operation_id: &str) -> String {
    deterministic_external_key("pr-txn-v1-", &[merchant_id, operation_id])
}

fn deterministic_external_key(prefix: &str, parts: &[&str]) -> String {
    let mut hasher = Sha256::new();
    for (index, part) in parts.iter().enumerate() {
        if index != 0 {
            hasher.update([0]);
        }
        hasher.update(part.as_bytes());
    }
    let digest = hasher.finalize();
    let mut output = String::with_capacity(prefix.len() + digest.len() * 2);
    output.push_str(prefix);
    for byte in digest {
        use fmt::Write as _;
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    output
}

pub fn minor_units_to_major_number(
    amount_minor: i64,
    currency: &str,
) -> Result<Number, KillBillClientError> {
    let scale = currency_scale(currency)?;
    if amount_minor < 0 {
        return Err(KillBillClientError::new(
            KillBillClientErrorKind::InvalidAmount,
            "negative payment amount is not supported",
        ));
    }
    let factor = 10_i128.pow(scale);
    let amount = i128::from(amount_minor);
    let text = if scale == 0 {
        amount.to_string()
    } else {
        let whole = amount / factor;
        let fraction = amount % factor;
        format!("{whole}.{fraction:0width$}", width = scale as usize)
    };
    serde_json::from_str::<Number>(&text).map_err(|error| {
        KillBillClientError::new(
            KillBillClientErrorKind::InvalidAmount,
            format!("unable to encode exact Kill Bill amount {text}: {error}"),
        )
    })
}

pub fn major_number_to_minor_units(
    amount: &Number,
    currency: &str,
) -> Result<i64, KillBillClientError> {
    let scale = currency_scale(currency)?;
    decimal_to_minor(&amount.to_string(), scale)
}

fn decimal_to_minor(value: &str, scale: u32) -> Result<i64, KillBillClientError> {
    if value.contains('e') || value.contains('E') {
        return Err(KillBillClientError::new(
            KillBillClientErrorKind::InvalidAmount,
            format!("exponent-form Kill Bill amount is not accepted: {value}"),
        ));
    }
    let (negative, unsigned) = match value.strip_prefix('-') {
        Some(value) => (true, value),
        None => (false, value),
    };
    let (whole, fraction) = match unsigned.split_once('.') {
        Some(parts) => parts,
        None => (unsigned, ""),
    };
    if whole.is_empty()
        || !whole.bytes().all(|byte| byte.is_ascii_digit())
        || !fraction.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(KillBillClientError::new(
            KillBillClientErrorKind::InvalidAmount,
            format!("invalid Kill Bill decimal amount: {value}"),
        ));
    }

    let scale = scale as usize;
    let (kept_fraction, excess_fraction) = if fraction.len() > scale {
        fraction.split_at(scale)
    } else {
        (fraction, "")
    };
    if excess_fraction.bytes().any(|byte| byte != b'0') {
        return Err(KillBillClientError::new(
            KillBillClientErrorKind::InvalidAmount,
            format!("Kill Bill amount {value} exceeds currency precision"),
        ));
    }

    let whole = whole.parse::<i128>().map_err(|error| {
        KillBillClientError::new(
            KillBillClientErrorKind::InvalidAmount,
            format!("invalid Kill Bill whole amount {value}: {error}"),
        )
    })?;
    let factor = 10_i128.pow(scale as u32);
    let mut fraction_value = if kept_fraction.is_empty() {
        0_i128
    } else {
        kept_fraction.parse::<i128>().map_err(|error| {
            KillBillClientError::new(
                KillBillClientErrorKind::InvalidAmount,
                format!("invalid Kill Bill fractional amount {value}: {error}"),
            )
        })?
    };
    for _ in kept_fraction.len()..scale {
        fraction_value *= 10;
    }
    let absolute = whole
        .checked_mul(factor)
        .and_then(|value| value.checked_add(fraction_value))
        .ok_or_else(|| {
            KillBillClientError::new(
                KillBillClientErrorKind::InvalidAmount,
                "Kill Bill amount overflow",
            )
        })?;
    let signed = if negative { -absolute } else { absolute };
    i64::try_from(signed).map_err(|_| {
        KillBillClientError::new(
            KillBillClientErrorKind::InvalidAmount,
            "Kill Bill amount is outside i64 minor-unit range",
        )
    })
}

fn currency_scale(currency: &str) -> Result<u32, KillBillClientError> {
    if !KILLBILL_FIAT_CURRENCIES.contains(&currency) {
        return Err(KillBillClientError::new(
            KillBillClientErrorKind::UnsupportedCurrency,
            format!("currency {currency:?} is not supported by the pinned Kill Bill client"),
        ));
    }
    Ok(match currency {
        "BIF" | "BYR" | "CLP" | "DJF" | "GNF" | "ISK" | "JPY" | "KMF" | "KRW"
        | "PYG" | "RWF" | "UGX" | "VND" | "VUV" | "XAF" | "XOF" | "XPF" => 0,
        "BHD" | "IQD" | "JOD" | "KWD" | "LYD" | "OMR" | "TND" => 3,
        _ => 2,
    })
}

const KILLBILL_FIAT_CURRENCIES: &[&str] = &[
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD",
    "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL", "BSD", "BTN", "BWP", "BYR",
    "BZD", "CAD", "CDF", "CHF", "CLP", "CNY", "COP", "CRC", "CUC", "CUP", "CVE", "CZK",
    "DJF", "DKK", "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL",
    "GGP", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HRK", "HTG", "HUF",
    "IDR", "ILS", "IMP", "INR", "IQD", "IRR", "ISK", "JEP", "JMD", "JOD", "JPY", "KES",
    "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD",
    "LSL", "LTL", "LVL", "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRO",
    "MUR", "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD",
    "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB",
    "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLL", "SOS", "SPL", "SRD",
    "STD", "SVC", "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TVD",
    "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VEF", "VND", "VUV", "WST", "XAF",
    "XCD", "XDR", "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWD",
];

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    use axum::extract::{Path, Query, State};
    use axum::http::{HeaderMap, StatusCode as AxumStatusCode};
    use axum::response::IntoResponse;
    use axum::routing::{get, post};
    use axum::{Json, Router};
    use serde_json::{json, Value};

    use super::*;

    const PAYMENT_ID: &str = "11111111-1111-1111-1111-111111111111";
    const ACCOUNT_ID: &str = "22222222-2222-2222-2222-222222222222";
    const PAYMENT_METHOD_ID: &str = "33333333-3333-3333-3333-333333333333";
    const TRANSACTION_ID: &str = "44444444-4444-4444-4444-444444444444";

    #[test]
    fn deterministic_external_keys_match_frozen_vectors_and_hide_raw_token() {
        assert_eq!(
            account_external_key("merchant-1", "KRW"),
            "pr-acct-v1-f0ad73afdc22cec96f7b7cd9156ad2570019f43a13b7272da13298998e53cca3"
        );
        let payment_method =
            payment_method_external_key("merchant-1", "KRW", "test-token-1");
        assert_eq!(
            payment_method,
            "pr-pm-v1-9b24926622d73b792538eeda8bc8eb83fdf18993316362af3eb2e99583952a3b"
        );
        assert!(!payment_method.contains("test-token-1"));
        assert_eq!(
            payment_external_key("merchant-1", "payment-1"),
            "pr-pay-v1-2bdee7ba62bc4c4cda402af9eb9a1d9a9ee1c6ec94e1ee2c1474992c53b6f5e2"
        );
        assert_eq!(
            transaction_external_key("merchant-1", "operation-1"),
            "pr-txn-v1-2b267dfef88c7ebe0690096b4ac1ad1bf7c9d90687a1c18485b07b5185baaba6"
        );
    }

    #[test]
    fn minor_major_conversion_is_exact_and_currency_aware() {
        assert_eq!(
            minor_units_to_major_number(1234, "USD").unwrap().to_string(),
            "12.34"
        );
        assert_eq!(
            minor_units_to_major_number(1234, "KRW").unwrap().to_string(),
            "1234"
        );
        assert_eq!(
            minor_units_to_major_number(1234, "BHD").unwrap().to_string(),
            "1.234"
        );

        let usd: Number = serde_json::from_str("12.340").unwrap();
        assert_eq!(major_number_to_minor_units(&usd, "USD").unwrap(), 1234);
        let invalid_precision: Number = serde_json::from_str("12.341").unwrap();
        assert_eq!(
            major_number_to_minor_units(&invalid_precision, "USD")
                .unwrap_err()
                .kind,
            KillBillClientErrorKind::InvalidAmount
        );
        assert_eq!(
            minor_units_to_major_number(100, "ZZZ").unwrap_err().kind,
            KillBillClientErrorKind::UnsupportedCurrency
        );
    }

    #[test]
    fn initial_operation_mapping_is_explicit() {
        assert_eq!(
            KillBillInitialTransactionType::try_from(PaymentOperationKind::Authorize).unwrap(),
            KillBillInitialTransactionType::Authorize
        );
        assert_eq!(
            KillBillInitialTransactionType::try_from(PaymentOperationKind::AuthCapture).unwrap(),
            KillBillInitialTransactionType::Purchase
        );
        assert!(KillBillInitialTransactionType::try_from(PaymentOperationKind::Capture).is_err());
    }

    #[test]
    fn response_mapping_uses_exact_transaction_external_key_not_array_order() {
        let payment = payment_fixture(vec![
            transaction_fixture("old-operation", "AUTHORIZE", "SUCCESS", "10.00"),
            transaction_fixture("expected-operation", "AUTHORIZE", "SUCCESS", "12.34"),
            transaction_fixture("later-operation", "CAPTURE", "PENDING", "1.00"),
        ]);
        let parsed: KillBillPayment = serde_json::from_value(payment).unwrap();

        let mapped = map_payment_outcome(
            &parsed,
            "expected-payment",
            "expected-operation",
            PaymentOperationKind::Authorize,
            Some(1234),
            "USD",
        )
        .unwrap();

        assert_eq!(mapped.operation_state, OperationState::Succeeded);
        assert_eq!(mapped.payment_state, Some(PaymentState::Authorized));
        assert_eq!(
            mapped.killbill_transaction_id,
            Uuid::parse_str(TRANSACTION_ID).unwrap()
        );
        assert_eq!(mapped.provider_payment_id.as_deref(), Some("provider-payment-1"));
        assert_eq!(
            mapped.provider_idempotency_key.as_deref(),
            Some("provider-idem-1")
        );
    }

    #[test]
    fn response_mapping_fails_closed_on_missing_or_duplicate_transaction_identity() {
        let missing: KillBillPayment =
            serde_json::from_value(payment_fixture(vec![])).unwrap();
        assert_eq!(
            map_payment_outcome(
                &missing,
                "expected-payment",
                "expected-operation",
                PaymentOperationKind::Authorize,
                Some(1234),
                "USD"
            )
            .unwrap_err()
            .kind,
            KillBillClientErrorKind::Invariant
        );

        let duplicate: KillBillPayment = serde_json::from_value(payment_fixture(vec![
            transaction_fixture("expected-operation", "AUTHORIZE", "SUCCESS", "12.34"),
            transaction_fixture("expected-operation", "AUTHORIZE", "SUCCESS", "12.34"),
        ]))
        .unwrap();
        assert_eq!(
            map_payment_outcome(
                &duplicate,
                "expected-payment",
                "expected-operation",
                PaymentOperationKind::Authorize,
                Some(1234),
                "USD"
            )
            .unwrap_err()
            .kind,
            KillBillClientErrorKind::Invariant
        );
    }

    #[test]
    fn unknown_transaction_maps_to_payment_unknown() {
        let parsed: KillBillPayment =
            serde_json::from_value(payment_fixture(vec![transaction_fixture(
                "expected-operation",
                "AUTHORIZE",
                "UNKNOWN",
                "12.34",
            )]))
            .unwrap();
        let mapped = map_payment_outcome(
            &parsed,
            "expected-payment",
            "expected-operation",
            PaymentOperationKind::Authorize,
            Some(1234),
            "USD",
        )
        .unwrap();
        assert_eq!(mapped.operation_state, OperationState::Unknown);
        assert_eq!(mapped.payment_state, Some(PaymentState::PaymentUnknown));
    }

    #[test]
    fn partial_capture_and_refund_are_derived_from_cumulative_payment_amounts() {
        let capture: KillBillPayment = serde_json::from_value(json!({
            "accountId": ACCOUNT_ID,
            "paymentId": PAYMENT_ID,
            "paymentExternalKey": "expected-payment",
            "authAmount": number("10.00"),
            "capturedAmount": number("4.00"),
            "purchasedAmount": 0,
            "refundedAmount": 0,
            "currency": "USD",
            "paymentMethodId": PAYMENT_METHOD_ID,
            "transactions": [transaction_fixture("capture-op", "CAPTURE", "SUCCESS", "4.00")]
        }))
        .unwrap();
        assert_eq!(
            map_payment_outcome(
                &capture,
                "expected-payment",
                "capture-op",
                PaymentOperationKind::Capture,
                Some(400),
                "USD"
            )
            .unwrap()
            .payment_state,
            Some(PaymentState::PartiallyCaptured)
        );

        let refund: KillBillPayment = serde_json::from_value(json!({
            "accountId": ACCOUNT_ID,
            "paymentId": PAYMENT_ID,
            "paymentExternalKey": "expected-payment",
            "authAmount": number("10.00"),
            "capturedAmount": number("10.00"),
            "purchasedAmount": 0,
            "refundedAmount": number("3.00"),
            "currency": "USD",
            "paymentMethodId": PAYMENT_METHOD_ID,
            "transactions": [transaction_fixture("refund-op", "REFUND", "SUCCESS", "3.00")]
        }))
        .unwrap();
        assert_eq!(
            map_payment_outcome(
                &refund,
                "expected-payment",
                "refund-op",
                PaymentOperationKind::Refund,
                Some(300),
                "USD"
            )
            .unwrap()
            .payment_state,
            Some(PaymentState::PartiallyRefunded)
        );
    }

    #[derive(Clone, Default)]
    struct CaptureState {
        requests: Arc<Mutex<Vec<CapturedRequest>>>,
    }

    #[derive(Debug, Clone)]
    struct CapturedRequest {
        path: String,
        query: HashMap<String, String>,
        authorization: Option<String>,
        api_key: Option<String>,
        api_secret: Option<String>,
        created_by: Option<String>,
        body: Value,
    }

    #[tokio::test]
    async fn initial_payment_client_sends_auth_exact_decimal_and_follows_same_origin_location() {
        let state = CaptureState::default();
        let app = Router::new()
            .route(
                "/1.0/kb/accounts/{account_id}/payments",
                post(capture_initial_payment),
            )
            .route("/1.0/kb/payments/{payment_id}/", get(return_payment))
            .with_state(state.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });

        let client = client_for(&format!("http://{address}/"));
        let mutation =
            KillBillMutation::new(1234, "USD", "expected-payment", "expected-operation").unwrap();
        let payment = client
            .create_initial_payment(
                Uuid::parse_str(ACCOUNT_ID).unwrap(),
                Uuid::parse_str(PAYMENT_METHOD_ID).unwrap(),
                KillBillInitialTransactionType::Authorize,
                &mutation,
            )
            .await
            .unwrap();

        assert_eq!(payment.payment_id, Uuid::parse_str(PAYMENT_ID).unwrap());
        let requests = state.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        let captured = &requests[0];
        assert_eq!(
            captured.path,
            format!("/1.0/kb/accounts/{ACCOUNT_ID}/payments")
        );
        assert_eq!(
            captured.query.get("paymentMethodId").map(String::as_str),
            Some(PAYMENT_METHOD_ID)
        );
        assert_eq!(
            captured.authorization.as_deref(),
            Some("Basic YWRtaW46cGFzc3dvcmQ=")
        );
        assert_eq!(captured.api_key.as_deref(), Some("bob"));
        assert_eq!(captured.api_secret.as_deref(), Some("lazar"));
        assert_eq!(captured.created_by.as_deref(), Some(CREATED_BY));
        assert_eq!(captured.body["amount"].to_string(), "12.34");
        assert!(captured.body["amount"].is_number());
        assert_eq!(captured.body["transactionType"], "AUTHORIZE");
        assert_eq!(
            captured.body["transactionExternalKey"],
            "expected-operation"
        );

        server.abort();
    }

    #[tokio::test]
    async fn mutation_503_is_inquiry_required_and_preserves_location() {
        let app = Router::new().route(
            "/1.0/kb/accounts/{account_id}/payments",
            post(|| async {
                (
                    AxumStatusCode::SERVICE_UNAVAILABLE,
                    [("Location", format!("/1.0/kb/payments/{PAYMENT_ID}/"))],
                    "unknown",
                )
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });

        let client = client_for(&format!("http://{address}/"));
        let mutation =
            KillBillMutation::new(1234, "USD", "expected-payment", "expected-operation").unwrap();
        let error = client
            .create_initial_payment(
                Uuid::parse_str(ACCOUNT_ID).unwrap(),
                Uuid::parse_str(PAYMENT_METHOD_ID).unwrap(),
                KillBillInitialTransactionType::Authorize,
                &mutation,
            )
            .await
            .unwrap_err();

        assert_eq!(error.kind, KillBillClientErrorKind::Http);
        assert_eq!(error.status, Some(503));
        assert!(error.requires_read_before_retry);
        let expected_location = format!("/1.0/kb/payments/{PAYMENT_ID}/");
        assert_eq!(error.location.as_deref(), Some(expected_location.as_str()));

        server.abort();
    }

    #[test]
    fn cross_origin_location_is_rejected_before_credentials_can_follow_it() {
        let client = client_for("http://127.0.0.1:8080/");
        let error = client
            .resolve_location("https://example.invalid/1.0/kb/payments/1")
            .unwrap_err();
        assert_eq!(error.kind, KillBillClientErrorKind::CrossOriginLocation);
        assert!(error.requires_read_before_retry);
    }

    async fn capture_initial_payment(
        Path(account_id): Path<String>,
        State(state): State<CaptureState>,
        Query(query): Query<HashMap<String, String>>,
        headers: HeaderMap,
        Json(body): Json<Value>,
    ) -> impl IntoResponse {
        assert_eq!(account_id, ACCOUNT_ID);
        state.requests.lock().unwrap().push(CapturedRequest {
            path: format!("/1.0/kb/accounts/{account_id}/payments"),
            query,
            authorization: header(&headers, "authorization"),
            api_key: header(&headers, "x-killbill-apikey"),
            api_secret: header(&headers, "x-killbill-apisecret"),
            created_by: header(&headers, "x-killbill-createdby"),
            body,
        });
        (
            AxumStatusCode::CREATED,
            [("Location", format!("/1.0/kb/payments/{PAYMENT_ID}/"))],
        )
    }

    async fn return_payment(Path(payment_id): Path<String>) -> Json<Value> {
        assert_eq!(payment_id, PAYMENT_ID);
        Json(payment_fixture(vec![transaction_fixture(
            "expected-operation",
            "AUTHORIZE",
            "SUCCESS",
            "12.34",
        )]))
    }

    fn header(headers: &HeaderMap, name: &str) -> Option<String> {
        headers
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    }

    fn client_for(base_url: &str) -> KillBillClient {
        KillBillClient::new(
            KillBillClientConfig::new(base_url, "admin", "password", "bob", "lazar").unwrap(),
        )
        .unwrap()
    }

    fn payment_fixture(transactions: Vec<Value>) -> Value {
        json!({
            "accountId": ACCOUNT_ID,
            "paymentId": PAYMENT_ID,
            "paymentExternalKey": "expected-payment",
            "authAmount": number("12.34"),
            "capturedAmount": 0,
            "purchasedAmount": 0,
            "refundedAmount": 0,
            "currency": "USD",
            "paymentMethodId": PAYMENT_METHOD_ID,
            "transactions": transactions
        })
    }

    fn number(value: &str) -> Number {
        serde_json::from_str(value).unwrap()
    }

    fn transaction_fixture(
        external_key: &str,
        transaction_type: &str,
        status: &str,
        amount: &str,
    ) -> Value {
        json!({
            "transactionId": TRANSACTION_ID,
            "transactionExternalKey": external_key,
            "paymentId": PAYMENT_ID,
            "paymentExternalKey": "expected-payment",
            "transactionType": transaction_type,
            "status": status,
            "amount": number(amount),
            "currency": "USD",
            "processedAmount": number(amount),
            "processedCurrency": "USD",
            "gatewayErrorCode": null,
            "gatewayErrorMsg": null,
            "firstPaymentReferenceId": "provider-payment-1",
            "secondPaymentReferenceId": "provider-idem-1",
            "properties": []
        })
    }
}
