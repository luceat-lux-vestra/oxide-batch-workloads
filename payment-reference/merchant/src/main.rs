mod gateway;

use std::net::SocketAddr;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use gateway::{GatewayClient, GatewayOutcome};
use payment_reference_contract::{
    CanonicalPaymentView, Currency, ExternalPaymentMapping, Money, OperationState,
    PaymentMutationRequest, PaymentOperationKind, PaymentState,
};
use serde::{Deserialize, Serialize};
use sqlx::{postgres::PgPoolOptions, PgPool, Row};

#[derive(Clone)]
struct AppState {
    pool: PgPool,
    gateway: GatewayClient,
}

#[derive(Debug)]
enum ApiError {
    BadRequest(String),
    NotFound(String),
    Conflict(String),
    Gateway(StatusCode, String),
    Upstream(String),
    Internal(String),
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            Self::BadRequest(message) => (StatusCode::BAD_REQUEST, message),
            Self::NotFound(message) => (StatusCode::NOT_FOUND, message),
            Self::Conflict(message) => (StatusCode::CONFLICT, message),
            Self::Gateway(status, message) => (status, message),
            Self::Upstream(message) => (StatusCode::BAD_GATEWAY, message),
            Self::Internal(message) => (StatusCode::INTERNAL_SERVER_ERROR, message),
        };
        (status, Json(ErrorBody { error: message })).into_response()
    }
}

#[derive(Serialize)]
struct ErrorBody {
    error: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CreateMerchantRequest {
    merchant_id: String,
    display_name: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CreateCustomerRequest {
    customer_id: String,
    display_name: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CreateOrderRequest {
    merchant_order_id: String,
    merchant_id: String,
    customer_id: String,
    amount_minor: i64,
    currency: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PreparePaymentRequest {
    merchant_payment_id: String,
    amount_minor: i64,
    currency: String,
    payment_method_token: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct MerchantPaymentView {
    merchant_payment_id: String,
    merchant_order_id: String,
    amount_minor: i64,
    currency: String,
    payment_state: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PaymentOperationRequest {
    operation_id: String,
    amount_minor: i64,
    idempotency_key: String,
    requested_at: Option<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct MerchantPaymentOperationView {
    merchant_payment_id: String,
    merchant_order_id: String,
    payment_state: PaymentState,
    operation_id: String,
    operation_state: OperationState,
    mapping: ExternalPaymentMapping,
}

struct PreparedPaymentContext {
    merchant_id: String,
    merchant_order_id: String,
    amount_minor: i64,
    currency: String,
    payment_method_token: String,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_ansi(false)
        .init();

    let database_url = std::env::var("MERCHANT_DATABASE_URL")?;
    let gateway_base_url = std::env::var("MERCHANT_GATEWAY_BASE_URL")?;
    let gateway = GatewayClient::new(&gateway_base_url)?;
    let bind: SocketAddr = std::env::var("MERCHANT_BIND")
        .unwrap_or_else(|_| "127.0.0.1:8081".to_owned())
        .parse()?;

    let pool = PgPoolOptions::new()
        .max_connections(8)
        .connect(&database_url)
        .await?;
    sqlx::migrate!().run(&pool).await?;

    let app = Router::new()
        .route("/health", get(health))
        .route("/merchants", post(create_merchant))
        .route("/customers", post(create_customer))
        .route("/orders", post(create_order))
        .route("/orders/{merchantOrderId}/payments", post(prepare_payment))
        .route(
            "/payments/{merchantPaymentId}/authorize",
            post(authorize_payment),
        )
        .route(
            "/payments/{merchantPaymentId}/auth-capture",
            post(auth_capture_payment),
        )
        .route(
            "/payments/{merchantPaymentId}/capture",
            post(capture_payment),
        )
        .route(
            "/payments/{merchantPaymentId}/cancel",
            post(cancel_payment),
        )
        .route(
            "/payments/{merchantPaymentId}/refund",
            post(refund_payment),
        )
        .route("/payments/{merchantPaymentId}", get(get_payment))
        .with_state(AppState { pool, gateway });

    let listener = tokio::net::TcpListener::bind(bind).await?;
    tracing::info!(%bind, "reference merchant listening");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn health(State(state): State<AppState>) -> Result<StatusCode, ApiError> {
    sqlx::query("SELECT 1")
        .execute(&state.pool)
        .await
        .map_err(internal)?;
    Ok(StatusCode::NO_CONTENT)
}

async fn create_merchant(
    State(state): State<AppState>,
    Json(request): Json<CreateMerchantRequest>,
) -> Result<StatusCode, ApiError> {
    require_non_blank("merchantId", &request.merchant_id)?;
    require_non_blank("displayName", &request.display_name)?;

    let result = sqlx::query(
        "INSERT INTO payment_reference_merchant.merchant (merchant_id, display_name)
         VALUES ($1, $2)
         ON CONFLICT DO NOTHING",
    )
    .bind(&request.merchant_id)
    .bind(&request.display_name)
    .execute(&state.pool)
    .await
    .map_err(internal)?;

    if result.rows_affected() == 0 {
        return Err(ApiError::Conflict("merchant already exists".into()));
    }
    Ok(StatusCode::CREATED)
}

async fn create_customer(
    State(state): State<AppState>,
    Json(request): Json<CreateCustomerRequest>,
) -> Result<StatusCode, ApiError> {
    require_non_blank("customerId", &request.customer_id)?;
    require_non_blank("displayName", &request.display_name)?;

    let result = sqlx::query(
        "INSERT INTO payment_reference_merchant.customer (customer_id, display_name)
         VALUES ($1, $2)
         ON CONFLICT DO NOTHING",
    )
    .bind(&request.customer_id)
    .bind(&request.display_name)
    .execute(&state.pool)
    .await
    .map_err(internal)?;

    if result.rows_affected() == 0 {
        return Err(ApiError::Conflict("customer already exists".into()));
    }
    Ok(StatusCode::CREATED)
}

async fn create_order(
    State(state): State<AppState>,
    Json(request): Json<CreateOrderRequest>,
) -> Result<StatusCode, ApiError> {
    require_non_blank("merchantOrderId", &request.merchant_order_id)?;
    require_non_blank("merchantId", &request.merchant_id)?;
    require_non_blank("customerId", &request.customer_id)?;
    validate_money(request.amount_minor, &request.currency)?;

    let result = sqlx::query(
        "INSERT INTO payment_reference_merchant.orders
         (merchant_order_id, merchant_id, customer_id, amount_minor, currency)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT DO NOTHING",
    )
    .bind(&request.merchant_order_id)
    .bind(&request.merchant_id)
    .bind(&request.customer_id)
    .bind(request.amount_minor)
    .bind(&request.currency)
    .execute(&state.pool)
    .await
    .map_err(internal)?;

    if result.rows_affected() == 0 {
        return Err(ApiError::Conflict("order already exists".into()));
    }
    Ok(StatusCode::CREATED)
}

async fn prepare_payment(
    Path(merchant_order_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PreparePaymentRequest>,
) -> Result<(StatusCode, Json<MerchantPaymentView>), ApiError> {
    require_non_blank("merchantPaymentId", &request.merchant_payment_id)?;
    require_non_blank("paymentMethodToken", &request.payment_method_token)?;
    validate_money(request.amount_minor, &request.currency)?;

    let order = sqlx::query(
        "SELECT amount_minor, currency
         FROM payment_reference_merchant.orders
         WHERE merchant_order_id = $1",
    )
    .bind(&merchant_order_id)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal)?
    .ok_or_else(|| ApiError::NotFound("order not found".into()))?;

    let order_amount: i64 = order.try_get("amount_minor").map_err(internal)?;
    let order_currency: String = order.try_get("currency").map_err(internal)?;

    if order_currency != request.currency {
        return Err(ApiError::BadRequest(
            "payment currency must match order currency".into(),
        ));
    }
    if request.amount_minor > order_amount {
        return Err(ApiError::BadRequest(
            "payment amount cannot exceed order amount".into(),
        ));
    }

    let result = sqlx::query(
        "INSERT INTO payment_reference_merchant.merchant_payment
         (merchant_payment_id, merchant_order_id, amount_minor, currency, payment_method_token)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT DO NOTHING",
    )
    .bind(&request.merchant_payment_id)
    .bind(&merchant_order_id)
    .bind(request.amount_minor)
    .bind(&request.currency)
    .bind(&request.payment_method_token)
    .execute(&state.pool)
    .await
    .map_err(internal)?;

    if result.rows_affected() == 0 {
        return Err(ApiError::Conflict("merchant payment already exists".into()));
    }

    sqlx::query(
        "INSERT INTO payment_reference_merchant.business_event
         (aggregate_type, aggregate_id, event_type, payload)
         VALUES ('PAYMENT', $1, 'PAYMENT_PREPARED', $2::jsonb)",
    )
    .bind(&request.merchant_payment_id)
    .bind(
        serde_json::json!({
            "merchantOrderId": merchant_order_id,
            "amountMinor": request.amount_minor,
            "currency": request.currency,
        })
        .to_string(),
    )
    .execute(&state.pool)
    .await
    .map_err(internal)?;

    let view = MerchantPaymentView {
        merchant_payment_id: request.merchant_payment_id,
        merchant_order_id,
        amount_minor: request.amount_minor,
        currency: request.currency,
        payment_state: "CREATED".into(),
    };
    Ok((StatusCode::CREATED, Json(view)))
}

async fn authorize_payment(
    Path(merchant_payment_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PaymentOperationRequest>,
) -> Result<(StatusCode, Json<MerchantPaymentOperationView>), ApiError> {
    process_payment_operation(
        &state,
        &merchant_payment_id,
        request,
        PaymentOperationKind::Authorize,
    )
    .await
}

async fn auth_capture_payment(
    Path(merchant_payment_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PaymentOperationRequest>,
) -> Result<(StatusCode, Json<MerchantPaymentOperationView>), ApiError> {
    process_payment_operation(
        &state,
        &merchant_payment_id,
        request,
        PaymentOperationKind::AuthCapture,
    )
    .await
}

async fn capture_payment(
    Path(merchant_payment_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PaymentOperationRequest>,
) -> Result<(StatusCode, Json<MerchantPaymentOperationView>), ApiError> {
    process_payment_operation(
        &state,
        &merchant_payment_id,
        request,
        PaymentOperationKind::Capture,
    )
    .await
}

async fn cancel_payment(
    Path(merchant_payment_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PaymentOperationRequest>,
) -> Result<(StatusCode, Json<MerchantPaymentOperationView>), ApiError> {
    process_payment_operation(
        &state,
        &merchant_payment_id,
        request,
        PaymentOperationKind::Cancel,
    )
    .await
}

async fn refund_payment(
    Path(merchant_payment_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PaymentOperationRequest>,
) -> Result<(StatusCode, Json<MerchantPaymentOperationView>), ApiError> {
    process_payment_operation(
        &state,
        &merchant_payment_id,
        request,
        PaymentOperationKind::Refund,
    )
    .await
}

async fn process_payment_operation(
    state: &AppState,
    merchant_payment_id: &str,
    request: PaymentOperationRequest,
    operation: PaymentOperationKind,
) -> Result<(StatusCode, Json<MerchantPaymentOperationView>), ApiError> {
    require_non_blank("merchantPaymentId", merchant_payment_id)?;
    require_non_blank("operationId", &request.operation_id)?;
    require_non_blank("idempotencyKey", &request.idempotency_key)?;

    let context = load_prepared_payment(&state.pool, merchant_payment_id).await?;
    validate_money(request.amount_minor, &context.currency)?;
    if request.amount_minor > context.amount_minor {
        return Err(ApiError::BadRequest(
            "operation amount cannot exceed prepared payment amount".into(),
        ));
    }

    persist_operation_identity(
        &state.pool,
        merchant_payment_id,
        &request.operation_id,
        operation,
        request.amount_minor,
        &request.idempotency_key,
    )
    .await?;

    let gateway_request = PaymentMutationRequest {
        merchant_id: context.merchant_id,
        merchant_order_id: context.merchant_order_id.clone(),
        merchant_payment_id: merchant_payment_id.to_owned(),
        operation_id: request.operation_id.clone(),
        amount_minor: request.amount_minor,
        currency: context.currency,
        payment_method_token: match operation {
            PaymentOperationKind::Authorize | PaymentOperationKind::AuthCapture => {
                Some(context.payment_method_token)
            }
            PaymentOperationKind::Capture
            | PaymentOperationKind::Cancel
            | PaymentOperationKind::Refund => None,
        },
        idempotency_key: request.idempotency_key,
        requested_at: request.requested_at,
    };

    let outcome = match state
        .gateway
        .mutate(operation, merchant_payment_id, &gateway_request)
        .await
    {
        Ok(outcome) => outcome,
        Err(error) => {
            tracing::warn!(
                operation = operation.as_str(),
                merchant_payment_id,
                error = %error,
                "Merchant -> Gateway transport outcome is unknown"
            );
            persist_unresolved_gateway_outcome(
                &state.pool,
                merchant_payment_id,
                &request.operation_id,
                OperationState::Unknown,
                "GATEWAY_TRANSPORT_UNKNOWN",
            )
            .await?;
            return Err(ApiError::Upstream(
                "Gateway transport outcome is unknown; retry the same operation identity".into(),
            ));
        }
    };

    match outcome {
        GatewayOutcome::View { status, view } => {
            if let Err(message) =
                validate_gateway_view_identity(merchant_payment_id, &request.operation_id, &view)
            {
                persist_unresolved_gateway_outcome(
                    &state.pool,
                    merchant_payment_id,
                    &request.operation_id,
                    OperationState::ManualReview,
                    "GATEWAY_IDENTITY_CONFLICT",
                )
                .await?;
                return Err(ApiError::Upstream(message.to_owned()));
            }
            persist_gateway_view(
                &state.pool,
                merchant_payment_id,
                &request.operation_id,
                &view,
            )
            .await?;

            let status = StatusCode::from_u16(status)
                .map_err(|error| ApiError::Internal(error.to_string()))?;
            Ok((
                status,
                Json(MerchantPaymentOperationView {
                    merchant_payment_id: merchant_payment_id.to_owned(),
                    merchant_order_id: context.merchant_order_id,
                    payment_state: view.payment_state,
                    operation_id: request.operation_id,
                    operation_state: view.last_operation_state,
                    mapping: view.mapping,
                }),
            ))
        }
        GatewayOutcome::Rejected { status, message } => {
            let status_code = StatusCode::from_u16(status)
                .map_err(|error| ApiError::Internal(error.to_string()))?;
            if is_retryable_gateway_status(status) {
                persist_unresolved_gateway_outcome(
                    &state.pool,
                    merchant_payment_id,
                    &request.operation_id,
                    OperationState::RetryPending,
                    "GATEWAY_RETRY_PENDING",
                )
                .await?;
            } else {
                persist_rejected_gateway_operation(
                    &state.pool,
                    merchant_payment_id,
                    &request.operation_id,
                    status,
                    &message,
                )
                .await?;
            }
            Err(ApiError::Gateway(status_code, message))
        }
    }
}

async fn load_prepared_payment(
    pool: &PgPool,
    merchant_payment_id: &str,
) -> Result<PreparedPaymentContext, ApiError> {
    let row = sqlx::query(
        "SELECT
             o.merchant_id,
             mp.merchant_order_id,
             mp.amount_minor,
             mp.currency,
             mp.payment_method_token
         FROM payment_reference_merchant.merchant_payment mp
         JOIN payment_reference_merchant.orders o
           ON o.merchant_order_id = mp.merchant_order_id
         WHERE mp.merchant_payment_id = $1",
    )
    .bind(merchant_payment_id)
    .fetch_optional(pool)
    .await
    .map_err(internal)?
    .ok_or_else(|| ApiError::NotFound("merchant payment not found".into()))?;

    Ok(PreparedPaymentContext {
        merchant_id: row.try_get("merchant_id").map_err(internal)?,
        merchant_order_id: row.try_get("merchant_order_id").map_err(internal)?,
        amount_minor: row.try_get("amount_minor").map_err(internal)?,
        currency: row.try_get("currency").map_err(internal)?,
        payment_method_token: row.try_get("payment_method_token").map_err(internal)?,
    })
}

async fn persist_operation_identity(
    pool: &PgPool,
    merchant_payment_id: &str,
    operation_id: &str,
    operation: PaymentOperationKind,
    amount_minor: i64,
    idempotency_key: &str,
) -> Result<(), ApiError> {
    let result = sqlx::query(
        "INSERT INTO payment_reference_merchant.merchant_payment_operation
         (operation_id, merchant_payment_id, operation_kind, operation_state, idempotency_key, amount_minor)
         VALUES ($1, $2, $3, 'PENDING', $4, $5)
         ON CONFLICT DO NOTHING",
    )
    .bind(operation_id)
    .bind(merchant_payment_id)
    .bind(operation.as_str())
    .bind(idempotency_key)
    .bind(amount_minor)
    .execute(pool)
    .await
    .map_err(internal)?;

    if result.rows_affected() == 1 {
        return Ok(());
    }

    let rows = sqlx::query(
        "SELECT
             operation_id,
             merchant_payment_id,
             operation_kind,
             idempotency_key,
             amount_minor
         FROM payment_reference_merchant.merchant_payment_operation
         WHERE operation_id = $1 OR idempotency_key = $2",
    )
    .bind(operation_id)
    .bind(idempotency_key)
    .fetch_all(pool)
    .await
    .map_err(internal)?;

    if rows.len() != 1 || !stored_operation_matches(
        &rows[0],
        merchant_payment_id,
        operation_id,
        operation,
        amount_minor,
        idempotency_key,
    )? {
        return Err(ApiError::Conflict(
            "operationId or idempotencyKey conflicts with a prior Merchant mutation".into(),
        ));
    }

    Ok(())
}

fn stored_operation_matches(
    row: &sqlx::postgres::PgRow,
    merchant_payment_id: &str,
    operation_id: &str,
    operation: PaymentOperationKind,
    amount_minor: i64,
    idempotency_key: &str,
) -> Result<bool, ApiError> {
    let stored_amount: Option<i64> = row.try_get("amount_minor").map_err(internal)?;
    Ok(row.try_get::<String, _>("operation_id").map_err(internal)? == operation_id
        && row
            .try_get::<String, _>("merchant_payment_id")
            .map_err(internal)?
            == merchant_payment_id
        && row
            .try_get::<String, _>("operation_kind")
            .map_err(internal)?
            == operation.as_str()
        && row
            .try_get::<String, _>("idempotency_key")
            .map_err(internal)?
            == idempotency_key
        && stored_amount == Some(amount_minor))
}

fn validate_gateway_view_identity(
    merchant_payment_id: &str,
    operation_id: &str,
    view: &CanonicalPaymentView,
) -> Result<(), &'static str> {
    if view.merchant_payment_id != merchant_payment_id
        || view.mapping.merchant_payment_id != merchant_payment_id
        || view.mapping.gateway_operation_id != operation_id
    {
        return Err("Gateway response identity conflicts with the Merchant operation");
    }
    Ok(())
}

async fn persist_gateway_view(
    pool: &PgPool,
    merchant_payment_id: &str,
    operation_id: &str,
    view: &CanonicalPaymentView,
) -> Result<(), ApiError> {
    let mut tx = pool.begin().await.map_err(internal)?;

    sqlx::query(
        "UPDATE payment_reference_merchant.merchant_payment
         SET payment_state = $2, updated_at = NOW()
         WHERE merchant_payment_id = $1",
    )
    .bind(merchant_payment_id)
    .bind(payment_state_name(view.payment_state))
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    sqlx::query(
        "UPDATE payment_reference_merchant.merchant_payment_operation
         SET operation_state = $2, updated_at = NOW()
         WHERE operation_id = $1",
    )
    .bind(operation_id)
    .bind(operation_state_name(view.last_operation_state))
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    sqlx::query(
        "INSERT INTO payment_reference_merchant.external_payment_mapping
         (
             merchant_payment_id,
             gateway_operation_id,
             killbill_payment_id,
             killbill_transaction_id,
             hyperswitch_payment_id,
             hyperswitch_attempt_id,
             provider_payment_id,
             provider_operation_id,
             provider_idempotency_key
         )
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
         ON CONFLICT (merchant_payment_id) DO UPDATE
         SET gateway_operation_id = EXCLUDED.gateway_operation_id,
             killbill_payment_id = COALESCE(
                 EXCLUDED.killbill_payment_id,
                 payment_reference_merchant.external_payment_mapping.killbill_payment_id
             ),
             killbill_transaction_id = COALESCE(
                 EXCLUDED.killbill_transaction_id,
                 payment_reference_merchant.external_payment_mapping.killbill_transaction_id
             ),
             hyperswitch_payment_id = COALESCE(
                 EXCLUDED.hyperswitch_payment_id,
                 payment_reference_merchant.external_payment_mapping.hyperswitch_payment_id
             ),
             hyperswitch_attempt_id = COALESCE(
                 EXCLUDED.hyperswitch_attempt_id,
                 payment_reference_merchant.external_payment_mapping.hyperswitch_attempt_id
             ),
             provider_payment_id = COALESCE(
                 EXCLUDED.provider_payment_id,
                 payment_reference_merchant.external_payment_mapping.provider_payment_id
             ),
             provider_operation_id = COALESCE(
                 EXCLUDED.provider_operation_id,
                 payment_reference_merchant.external_payment_mapping.provider_operation_id
             ),
             provider_idempotency_key = COALESCE(
                 EXCLUDED.provider_idempotency_key,
                 payment_reference_merchant.external_payment_mapping.provider_idempotency_key
             ),
             updated_at = NOW()",
    )
    .bind(merchant_payment_id)
    .bind(&view.mapping.gateway_operation_id)
    .bind(&view.mapping.killbill_payment_id)
    .bind(&view.mapping.killbill_transaction_id)
    .bind(&view.mapping.hyperswitch_payment_id)
    .bind(&view.mapping.hyperswitch_attempt_id)
    .bind(&view.mapping.provider_payment_id)
    .bind(&view.mapping.provider_operation_id)
    .bind(&view.mapping.provider_idempotency_key)
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    let payload = serde_json::to_string(view).map_err(internal)?;
    sqlx::query(
        "INSERT INTO payment_reference_merchant.business_event
         (aggregate_type, aggregate_id, event_type, payload)
         VALUES ('PAYMENT', $1, 'GATEWAY_PAYMENT_OUTCOME', $2::jsonb)",
    )
    .bind(merchant_payment_id)
    .bind(payload)
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    tx.commit().await.map_err(internal)?;
    Ok(())
}

async fn persist_unresolved_gateway_outcome(
    pool: &PgPool,
    merchant_payment_id: &str,
    operation_id: &str,
    operation_state: OperationState,
    event_type: &str,
) -> Result<(), ApiError> {
    let mut tx = pool.begin().await.map_err(internal)?;
    sqlx::query(
        "UPDATE payment_reference_merchant.merchant_payment
         SET payment_state = 'PAYMENT_UNKNOWN', updated_at = NOW()
         WHERE merchant_payment_id = $1",
    )
    .bind(merchant_payment_id)
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    sqlx::query(
        "UPDATE payment_reference_merchant.merchant_payment_operation
         SET operation_state = $2, updated_at = NOW()
         WHERE operation_id = $1",
    )
    .bind(operation_id)
    .bind(operation_state_name(operation_state))
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    sqlx::query(
        "INSERT INTO payment_reference_merchant.business_event
         (aggregate_type, aggregate_id, event_type, payload)
         VALUES ('PAYMENT', $1, $2, $3::jsonb)",
    )
    .bind(merchant_payment_id)
    .bind(event_type)
    .bind(
        serde_json::json!({
            "operationId": operation_id,
            "operationState": operation_state_name(operation_state),
        })
        .to_string(),
    )
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    tx.commit().await.map_err(internal)?;
    Ok(())
}

async fn persist_rejected_gateway_operation(
    pool: &PgPool,
    merchant_payment_id: &str,
    operation_id: &str,
    status: u16,
    message: &str,
) -> Result<(), ApiError> {
    let mut tx = pool.begin().await.map_err(internal)?;
    sqlx::query(
        "UPDATE payment_reference_merchant.merchant_payment_operation
         SET operation_state = 'FAILED', updated_at = NOW()
         WHERE operation_id = $1",
    )
    .bind(operation_id)
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    sqlx::query(
        "INSERT INTO payment_reference_merchant.business_event
         (aggregate_type, aggregate_id, event_type, payload)
         VALUES ('PAYMENT', $1, 'GATEWAY_PAYMENT_REJECTED', $2::jsonb)",
    )
    .bind(merchant_payment_id)
    .bind(
        serde_json::json!({
            "operationId": operation_id,
            "status": status,
            "error": message,
        })
        .to_string(),
    )
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    tx.commit().await.map_err(internal)?;
    Ok(())
}

fn is_retryable_gateway_status(status: u16) -> bool {
    matches!(status, 408 | 425 | 429 | 500..=599)
}

fn payment_state_name(state: PaymentState) -> &'static str {
    match state {
        PaymentState::Created => "CREATED",
        PaymentState::PaymentPending => "PAYMENT_PENDING",
        PaymentState::Authorized => "AUTHORIZED",
        PaymentState::PartiallyCaptured => "PARTIALLY_CAPTURED",
        PaymentState::Paid => "PAID",
        PaymentState::Cancelled => "CANCELLED",
        PaymentState::PartiallyRefunded => "PARTIALLY_REFUNDED",
        PaymentState::Refunded => "REFUNDED",
        PaymentState::PaymentFailed => "PAYMENT_FAILED",
        PaymentState::PaymentUnknown => "PAYMENT_UNKNOWN",
    }
}

fn operation_state_name(state: OperationState) -> &'static str {
    match state {
        OperationState::Pending => "PENDING",
        OperationState::Succeeded => "SUCCEEDED",
        OperationState::Failed => "FAILED",
        OperationState::Unknown => "UNKNOWN",
        OperationState::RetryPending => "RETRY_PENDING",
        OperationState::ManualReview => "MANUAL_REVIEW",
    }
}

async fn get_payment(
    Path(merchant_payment_id): Path<String>,
    State(state): State<AppState>,
) -> Result<Json<MerchantPaymentView>, ApiError> {
    let row = sqlx::query(
        "SELECT merchant_payment_id, merchant_order_id, amount_minor, currency, payment_state
         FROM payment_reference_merchant.merchant_payment
         WHERE merchant_payment_id = $1",
    )
    .bind(&merchant_payment_id)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal)?
    .ok_or_else(|| ApiError::NotFound("merchant payment not found".into()))?;

    Ok(Json(MerchantPaymentView {
        merchant_payment_id: row.try_get("merchant_payment_id").map_err(internal)?,
        merchant_order_id: row.try_get("merchant_order_id").map_err(internal)?,
        amount_minor: row.try_get("amount_minor").map_err(internal)?,
        currency: row.try_get("currency").map_err(internal)?,
        payment_state: row.try_get("payment_state").map_err(internal)?,
    }))
}

fn validate_money(amount_minor: i64, currency: &str) -> Result<(), ApiError> {
    let currency = Currency::parse(currency.to_owned())
        .map_err(|error| ApiError::BadRequest(error.to_string()))?;
    Money::new(amount_minor, currency)
        .map(|_| ())
        .map_err(|error| ApiError::BadRequest(error.to_string()))
}

fn require_non_blank(field: &str, value: &str) -> Result<(), ApiError> {
    if value.trim().is_empty() {
        return Err(ApiError::BadRequest(format!("{field} must not be blank")));
    }
    Ok(())
}

fn internal(error: impl std::fmt::Display) -> ApiError {
    tracing::error!(error = %error, "merchant persistence failure");
    ApiError::Internal("merchant persistence failure".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retryable_gateway_statuses_never_become_terminal_merchant_failures() {
        for status in [408, 425, 429, 500, 502, 503, 504, 599] {
            assert!(is_retryable_gateway_status(status));
        }
        for status in [400, 404, 409, 422] {
            assert!(!is_retryable_gateway_status(status));
        }
    }

    #[test]
    fn gateway_identity_must_match_merchant_payment_and_operation() {
        let view = CanonicalPaymentView {
            merchant_payment_id: "payment-1".into(),
            payment_state: PaymentState::Authorized,
            last_operation_state: OperationState::Succeeded,
            mapping: ExternalPaymentMapping {
                merchant_payment_id: "payment-1".into(),
                gateway_operation_id: "operation-1".into(),
                killbill_payment_id: Some("kb-payment".into()),
                killbill_transaction_id: Some("kb-transaction".into()),
                hyperswitch_payment_id: None,
                hyperswitch_attempt_id: None,
                provider_payment_id: Some("provider-payment".into()),
                provider_operation_id: None,
                provider_idempotency_key: Some("provider-key".into()),
            },
        };

        assert!(validate_gateway_view_identity("payment-1", "operation-1", &view).is_ok());
        assert!(validate_gateway_view_identity("payment-2", "operation-1", &view).is_err());
        assert!(validate_gateway_view_identity("payment-1", "operation-2", &view).is_err());
    }
}
