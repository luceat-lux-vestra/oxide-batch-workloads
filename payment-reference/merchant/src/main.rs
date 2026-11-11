use std::net::SocketAddr;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use payment_reference_contract::{Currency, Money};
use serde::{Deserialize, Serialize};
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
        .route(
            "/orders/{merchantOrderId}/payments",
            post(prepare_payment),
        )
        .route("/payments/{merchantPaymentId}", get(get_payment))
        .with_state(AppState { pool });

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
