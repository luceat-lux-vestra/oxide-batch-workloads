use std::net::SocketAddr;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use payment_reference_contract::{
    CanonicalMutation, CanonicalPaymentView, ExternalPaymentMapping, OperationState,
    PaymentMutationRequest, PaymentOperationKind, PaymentState,
};
use serde::Serialize;
use sqlx::{postgres::PgPoolOptions, PgPool, Row};

#[derive(Clone)]
struct AppState {
    pool: PgPool,
}

#[derive(Debug)]
enum ApiError {
    BadRequest(String),
    NotFound(String),
    Conflict(String),
    Internal(String),
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            Self::BadRequest(message) => (StatusCode::BAD_REQUEST, message),
            Self::NotFound(message) => (StatusCode::NOT_FOUND, message),
            Self::Conflict(message) => (StatusCode::CONFLICT, message),
            Self::Internal(message) => (StatusCode::INTERNAL_SERVER_ERROR, message),
        };
        (status, Json(ErrorBody { error: message })).into_response()
    }
}

#[derive(Serialize)]
struct ErrorBody {
    error: String,
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

    let database_url = std::env::var("GATEWAY_DATABASE_URL")?;
    let bind: SocketAddr = std::env::var("GATEWAY_BIND")
        .unwrap_or_else(|_| "127.0.0.1:8080".to_owned())
        .parse()?;

    let pool = PgPoolOptions::new()
        .max_connections(8)
        .connect(&database_url)
        .await?;
    sqlx::migrate!().run(&pool).await?;

    let app = Router::new()
        .route("/health", get(health))
        .route("/payments/authorize", post(authorize))
        .route("/payments/auth-capture", post(auth_capture))
        .route("/payments/{paymentId}/capture", post(capture))
        .route("/payments/{paymentId}/cancel", post(cancel))
        .route("/payments/{paymentId}/refund", post(refund))
        .route("/payments/{paymentId}", get(get_payment))
        .with_state(AppState { pool });

    let listener = tokio::net::TcpListener::bind(bind).await?;
    tracing::info!(%bind, "reference gateway listening");
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

async fn authorize(
    State(state): State<AppState>,
    Json(request): Json<PaymentMutationRequest>,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    persist_mutation(&state.pool, request, PaymentOperationKind::Authorize, None).await
}

async fn auth_capture(
    State(state): State<AppState>,
    Json(request): Json<PaymentMutationRequest>,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    persist_mutation(
        &state.pool,
        request,
        PaymentOperationKind::AuthCapture,
        None,
    )
    .await
}

async fn capture(
    Path(payment_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PaymentMutationRequest>,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    persist_mutation(
        &state.pool,
        request,
        PaymentOperationKind::Capture,
        Some(&payment_id),
    )
    .await
}

async fn cancel(
    Path(payment_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PaymentMutationRequest>,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    persist_mutation(
        &state.pool,
        request,
        PaymentOperationKind::Cancel,
        Some(&payment_id),
    )
    .await
}

async fn refund(
    Path(payment_id): Path<String>,
    State(state): State<AppState>,
    Json(request): Json<PaymentMutationRequest>,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    persist_mutation(
        &state.pool,
        request,
        PaymentOperationKind::Refund,
        Some(&payment_id),
    )
    .await
}

async fn persist_mutation(
    pool: &PgPool,
    request: PaymentMutationRequest,
    operation: PaymentOperationKind,
    path_payment_id: Option<&str>,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    let mutation = request
        .into_canonical(operation)
        .map_err(|error| ApiError::BadRequest(error.to_string()))?;

    if let Some(path_payment_id) = path_payment_id {
        if path_payment_id != mutation.merchant_payment_id {
            return Err(ApiError::BadRequest(
                "path paymentId must match merchantPaymentId".into(),
            ));
        }
    }

    let inserted = sqlx::query(
        "INSERT INTO payment_reference_gateway.gateway_operation
         (
             operation_id,
             idempotency_key,
             merchant_id,
             merchant_order_id,
             merchant_payment_id,
             operation_kind,
             amount_minor,
             currency,
             payment_method_token,
             requested_at
         )
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
         ON CONFLICT DO NOTHING",
    )
    .bind(&mutation.operation_id)
    .bind(&mutation.idempotency_key)
    .bind(&mutation.merchant_id)
    .bind(&mutation.merchant_order_id)
    .bind(&mutation.merchant_payment_id)
    .bind(mutation.operation.as_str())
    .bind(mutation.money.amount_minor)
    .bind(mutation.money.currency.as_str())
    .bind(&mutation.payment_method_token)
    .bind(&mutation.requested_at)
    .execute(pool)
    .await
    .map_err(internal)?;

    let row = sqlx::query(
        "SELECT
             operation_id,
             idempotency_key,
             merchant_id,
             merchant_order_id,
             merchant_payment_id,
             operation_kind,
             amount_minor,
             currency,
             payment_method_token,
             killbill_payment_id,
             killbill_transaction_id,
             hyperswitch_payment_id,
             hyperswitch_attempt_id,
             provider_payment_id,
             provider_operation_id,
             provider_idempotency_key
         FROM payment_reference_gateway.gateway_operation
         WHERE idempotency_key = $1",
    )
    .bind(&mutation.idempotency_key)
    .fetch_optional(pool)
    .await
    .map_err(internal)?
    .ok_or_else(|| {
        ApiError::Conflict("operationId is already bound to a different idempotency key".into())
    })?;

    if !stored_row_matches(&row, &mutation)? {
        return Err(ApiError::Conflict(
            "idempotency key is already bound to a conflicting mutation".into(),
        ));
    }

    let status = if inserted.rows_affected() == 1 {
        StatusCode::ACCEPTED
    } else {
        StatusCode::OK
    };

    Ok((status, Json(view_from_row(&row)?)))
}

async fn get_payment(
    Path(payment_id): Path<String>,
    State(state): State<AppState>,
) -> Result<Json<CanonicalPaymentView>, ApiError> {
    let row = sqlx::query(
        "SELECT
             operation_id,
             idempotency_key,
             merchant_id,
             merchant_order_id,
             merchant_payment_id,
             operation_kind,
             amount_minor,
             currency,
             payment_method_token,
             killbill_payment_id,
             killbill_transaction_id,
             hyperswitch_payment_id,
             hyperswitch_attempt_id,
             provider_payment_id,
             provider_operation_id,
             provider_idempotency_key
         FROM payment_reference_gateway.gateway_operation
         WHERE merchant_payment_id = $1
         ORDER BY created_at DESC
         LIMIT 1",
    )
    .bind(&payment_id)
    .fetch_optional(&state.pool)
    .await
    .map_err(internal)?
    .ok_or_else(|| ApiError::NotFound("payment not found".into()))?;

    Ok(Json(view_from_row(&row)?))
}

fn stored_row_matches(
    row: &sqlx::postgres::PgRow,
    mutation: &CanonicalMutation,
) -> Result<bool, ApiError> {
    let operation_id: String = row.try_get("operation_id").map_err(internal)?;
    let merchant_id: String = row.try_get("merchant_id").map_err(internal)?;
    let merchant_order_id: String = row.try_get("merchant_order_id").map_err(internal)?;
    let merchant_payment_id: String = row.try_get("merchant_payment_id").map_err(internal)?;
    let operation_kind: String = row.try_get("operation_kind").map_err(internal)?;
    let amount_minor: i64 = row.try_get("amount_minor").map_err(internal)?;
    let currency: String = row.try_get("currency").map_err(internal)?;
    let payment_method_token: Option<String> =
        row.try_get("payment_method_token").map_err(internal)?;

    Ok(operation_id == mutation.operation_id.as_str()
        && merchant_id == mutation.merchant_id.as_str()
        && merchant_order_id == mutation.merchant_order_id.as_str()
        && merchant_payment_id == mutation.merchant_payment_id.as_str()
        && operation_kind == mutation.operation.as_str()
        && amount_minor == mutation.money.amount_minor
        && currency == mutation.money.currency.as_str()
        && payment_method_token.as_deref() == mutation.payment_method_token.as_deref())
}

fn view_from_row(row: &sqlx::postgres::PgRow) -> Result<CanonicalPaymentView, ApiError> {
    Ok(CanonicalPaymentView {
        merchant_payment_id: row.try_get("merchant_payment_id").map_err(internal)?,
        payment_state: PaymentState::PaymentPending,
        last_operation_state: OperationState::Pending,
        mapping: ExternalPaymentMapping {
            merchant_payment_id: row.try_get("merchant_payment_id").map_err(internal)?,
            gateway_operation_id: row.try_get("operation_id").map_err(internal)?,
            killbill_payment_id: row.try_get("killbill_payment_id").map_err(internal)?,
            killbill_transaction_id: row.try_get("killbill_transaction_id").map_err(internal)?,
            hyperswitch_payment_id: row.try_get("hyperswitch_payment_id").map_err(internal)?,
            hyperswitch_attempt_id: row.try_get("hyperswitch_attempt_id").map_err(internal)?,
            provider_payment_id: row.try_get("provider_payment_id").map_err(internal)?,
            provider_operation_id: row.try_get("provider_operation_id").map_err(internal)?,
            provider_idempotency_key: row.try_get("provider_idempotency_key").map_err(internal)?,
        },
    })
}

fn internal(error: impl std::fmt::Display) -> ApiError {
    tracing::error!(error = %error, "gateway persistence failure");
    ApiError::Internal("gateway persistence failure".into())
}
