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
use payment_reference_gateway::killbill::{
    account_external_key, map_payment_outcome, payment_external_key,
    payment_method_external_key, transaction_external_key, KillBillAccount, KillBillClient,
    KillBillClientConfig, KillBillClientError, KillBillClientErrorKind, KillBillCreateAccountRequest,
    KillBillInitialTransactionType, KillBillMappedOutcome, KillBillMutation,
    KillBillPaymentMethod, PAYMENT_PLUGIN_NAME,
};
use serde::Serialize;
use sqlx::{
    postgres::{PgPoolOptions, PgRow},
    PgConnection, PgPool, Postgres, Row, Transaction,
};

#[derive(Clone)]
struct AppState {
    pool: PgPool,
    killbill: KillBillClient,
}

#[derive(Debug)]
enum ApiError {
    BadRequest(String),
    NotFound(String),
    Conflict(String),
    Upstream(String),
    Internal(String),
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            Self::BadRequest(message) => (StatusCode::BAD_REQUEST, message),
            Self::NotFound(message) => (StatusCode::NOT_FOUND, message),
            Self::Conflict(message) => (StatusCode::CONFLICT, message),
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

    let killbill_base_url = std::env::var("KILLBILL_BASE_URL")?;
    let killbill_username = std::env::var("KILLBILL_USERNAME")?;
    let killbill_password = std::env::var("KILLBILL_PASSWORD")?;
    let killbill_api_key = std::env::var("KILLBILL_API_KEY")?;
    let killbill_api_secret = std::env::var("KILLBILL_API_SECRET")?;
    let killbill = KillBillClient::new(KillBillClientConfig::new(
        &killbill_base_url,
        killbill_username,
        killbill_password,
        killbill_api_key,
        killbill_api_secret,
    )?)?;

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
        .with_state(AppState { pool, killbill });

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

    state
        .killbill
        .get_account_by_external_key("pr-healthcheck-v1-never-provision")
        .await
        .map_err(|error| upstream("Kill Bill tenant authentication/readiness failed", &error))?;

    Ok(StatusCode::NO_CONTENT)
}

async fn authorize(
    State(state): State<AppState>,
    Json(request): Json<PaymentMutationRequest>,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    process_initial_mutation(&state, request, PaymentOperationKind::Authorize).await
}

async fn auth_capture(
    State(state): State<AppState>,
    Json(request): Json<PaymentMutationRequest>,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    process_initial_mutation(&state, request, PaymentOperationKind::AuthCapture).await
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

async fn process_initial_mutation(
    state: &AppState,
    request: PaymentMutationRequest,
    operation: PaymentOperationKind,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    let mutation = request
        .into_canonical(operation)
        .map_err(|error| ApiError::BadRequest(error.to_string()))?;

    insert_mutation(&state.pool, &mutation).await?;

    // The canonical row is committed before any external side effect. The row
    // lock then serializes exact concurrent replays while an external request is
    // in flight. A process crash after Kill Bill commits therefore leaves a
    // durable PENDING row that the next replay must inquire before dispatching.
    let mut tx = state.pool.begin().await.map_err(internal)?;
    let row = load_operation_for_update(&mut tx, &mutation.idempotency_key).await?;

    if !stored_row_matches(&row, &mutation)? {
        return Err(ApiError::Conflict(
            "idempotency key is already bound to a conflicting mutation".into(),
        ));
    }

    let current_view = view_from_row(&row)?;
    if is_terminal_operation(current_view.last_operation_state) {
        validate_terminal_initial_mapping(&current_view)?;
        let status = response_status(current_view.last_operation_state);
        tx.commit().await.map_err(internal)?;
        return Ok((status, Json(current_view)));
    }

    let payment_key = payment_external_key(&mutation.merchant_id, &mutation.merchant_payment_id);
    let transaction_key = transaction_external_key(&mutation.merchant_id, &mutation.operation_id);

    // Different operation IDs for the same canonical payment use different DB
    // rows. Serialize their initial side effects on the deterministic Kill Bill
    // payment key so they cannot both observe absence and dispatch concurrently.
    lock_initial_payment(&mut tx, &payment_key).await?;

    // Always inquire before the initial mutation, including the request that
    // first inserted the canonical row. This closes the crash/concurrency gap
    // where another exact replay could have held the row lock, committed a Kill
    // Bill side effect, and crashed before persisting the Kill Bill IDs.
    match inquiry_initial_outcome(
        &state.killbill,
        &payment_key,
        &transaction_key,
        &mutation,
    )
    .await
    {
        Ok(Some(outcome)) => {
            persist_initial_outcome(&mut tx, &mutation.operation_id, &outcome).await?;
            return commit_initial_view(tx, &mutation.operation_id).await;
        }
        Ok(None) => {
            // Absence was observed while this request owns the canonical row
            // lock. Exactly one deterministic mutation may now be dispatched.
        }
        Err(error) if error.kind == KillBillClientErrorKind::Invariant => {
            log_killbill_error("initial pre-dispatch identity conflict", &error);
            persist_initial_state(
                &mut tx,
                &mutation.operation_id,
                PaymentState::PaymentUnknown,
                OperationState::ManualReview,
            )
            .await?;
            tx.commit().await.map_err(internal)?;
            return Err(ApiError::Conflict(
                "Kill Bill payment identity conflicts with the canonical operation".into(),
            ));
        }
        Err(error) => {
            log_killbill_error("initial pre-dispatch inquiry failed", &error);
            persist_initial_state(
                &mut tx,
                &mutation.operation_id,
                PaymentState::PaymentUnknown,
                OperationState::Unknown,
            )
            .await?;
            tx.commit().await.map_err(internal)?;
            return Err(ApiError::Upstream(
                "Kill Bill inquiry failed; retry with the same idempotency key".into(),
            ));
        }
    }

    let account_id = match resolve_account(&state.killbill, &mutation).await {
        Ok(account_id) => account_id,
        Err(error) => {
            log_killbill_error("Kill Bill account resolution failed", &error);
            persist_initial_state(
                &mut tx,
                &mutation.operation_id,
                PaymentState::PaymentPending,
                OperationState::RetryPending,
            )
            .await?;
            tx.commit().await.map_err(internal)?;
            return Err(ApiError::Upstream(
                "Kill Bill account resolution failed; retry with the same idempotency key".into(),
            ));
        }
    };

    let payment_method_id = match resolve_payment_method(&state.killbill, account_id, &mutation).await
    {
        Ok(payment_method_id) => payment_method_id,
        Err(error) => {
            log_killbill_error("Kill Bill payment-method resolution failed", &error);
            persist_initial_state(
                &mut tx,
                &mutation.operation_id,
                PaymentState::PaymentPending,
                OperationState::RetryPending,
            )
            .await?;
            tx.commit().await.map_err(internal)?;
            return Err(ApiError::Upstream(
                "Kill Bill payment-method resolution failed; retry with the same idempotency key"
                    .into(),
            ));
        }
    };

    let transaction_type =
        KillBillInitialTransactionType::try_from(mutation.operation).map_err(|error| {
            ApiError::Internal(format!("invalid initial payment operation: {error}"))
        })?;
    let killbill_mutation = KillBillMutation::new(
        mutation.money.amount_minor,
        mutation.money.currency.as_str(),
        payment_key.clone(),
        transaction_key.clone(),
    )
    .map_err(|error| ApiError::BadRequest(error.to_string()))?;

    match state
        .killbill
        .create_initial_payment(
            account_id,
            payment_method_id,
            transaction_type,
            &killbill_mutation,
        )
        .await
    {
        Ok(payment) => {
            match map_payment_outcome(
                &payment,
                &payment_key,
                &transaction_key,
                mutation.operation,
                Some(mutation.money.amount_minor),
                mutation.money.currency.as_str(),
            ) {
                Ok(outcome) => {
                    persist_initial_outcome(&mut tx, &mutation.operation_id, &outcome).await?;
                    commit_initial_view(tx, &mutation.operation_id).await
                }
                Err(error) => {
                    log_killbill_error("Kill Bill mutation readback violated identity contract", &error);
                    recover_after_ambiguous_effect(
                        tx,
                        state,
                        &mutation,
                        &payment_key,
                        &transaction_key,
                    )
                    .await
                }
            }
        }
        Err(error) if error.requires_read_before_retry => {
            log_killbill_error("Kill Bill mutation outcome is ambiguous", &error);
            recover_after_ambiguous_effect(
                tx,
                state,
                &mutation,
                &payment_key,
                &transaction_key,
            )
            .await
        }
        Err(error) => {
            log_killbill_error("Kill Bill initial mutation failed before an ambiguous effect", &error);
            persist_initial_state(
                &mut tx,
                &mutation.operation_id,
                PaymentState::PaymentPending,
                OperationState::RetryPending,
            )
            .await?;
            tx.commit().await.map_err(internal)?;
            Err(ApiError::Upstream(
                "Kill Bill mutation failed; retry with the same idempotency key".into(),
            ))
        }
    }
}

async fn recover_after_ambiguous_effect(
    mut tx: Transaction<'_, Postgres>,
    state: &AppState,
    mutation: &CanonicalMutation,
    payment_key: &str,
    transaction_key: &str,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    match inquiry_initial_outcome(
        &state.killbill,
        payment_key,
        transaction_key,
        mutation,
    )
    .await
    {
        Ok(Some(outcome)) => {
            persist_initial_outcome(&mut tx, &mutation.operation_id, &outcome).await?;
        }
        Ok(None) => {
            // One read after the ambiguous mutation did not find the payment.
            // Do not dispatch again in this request. A future exact replay will
            // perform another inquiry before it is allowed to retry.
            persist_initial_state(
                &mut tx,
                &mutation.operation_id,
                PaymentState::PaymentUnknown,
                OperationState::Unknown,
            )
            .await?;
        }
        Err(error) if error.kind == KillBillClientErrorKind::Invariant => {
            log_killbill_error("Kill Bill ambiguous-effect identity conflict", &error);
            persist_initial_state(
                &mut tx,
                &mutation.operation_id,
                PaymentState::PaymentUnknown,
                OperationState::ManualReview,
            )
            .await?;
        }
        Err(error) => {
            log_killbill_error("Kill Bill ambiguous-effect inquiry failed", &error);
            persist_initial_state(
                &mut tx,
                &mutation.operation_id,
                PaymentState::PaymentUnknown,
                OperationState::Unknown,
            )
            .await?;
        }
    }

    commit_initial_view(tx, &mutation.operation_id).await
}

async fn inquiry_initial_outcome(
    killbill: &KillBillClient,
    payment_key: &str,
    transaction_key: &str,
    mutation: &CanonicalMutation,
) -> Result<Option<KillBillMappedOutcome>, KillBillClientError> {
    let payment = match killbill.get_payment_by_external_key(payment_key).await? {
        Some(payment) => payment,
        None => return Ok(None),
    };

    map_payment_outcome(
        &payment,
        payment_key,
        transaction_key,
        mutation.operation,
        Some(mutation.money.amount_minor),
        mutation.money.currency.as_str(),
    )
    .map(Some)
}

async fn resolve_account(
    killbill: &KillBillClient,
    mutation: &CanonicalMutation,
) -> Result<uuid::Uuid, KillBillClientError> {
    let external_key = account_external_key(&mutation.merchant_id, mutation.money.currency.as_str());

    if let Some(account) = killbill.get_account_by_external_key(&external_key).await? {
        return verify_account(account, &external_key, mutation.money.currency.as_str());
    }

    let request =
        KillBillCreateAccountRequest::reference(external_key.clone(), mutation.money.currency.as_str())?;

    match killbill.create_account(&request).await {
        Ok(account) => verify_account(account, &external_key, mutation.money.currency.as_str()),
        Err(create_error) => {
            // Concurrent create or lost create response: converge only by the
            // same deterministic account key and immutable currency.
            match killbill.get_account_by_external_key(&external_key).await {
                Ok(Some(account)) => {
                    verify_account(account, &external_key, mutation.money.currency.as_str())
                }
                Ok(None) | Err(_) => Err(create_error),
            }
        }
    }
}

fn verify_account(
    account: KillBillAccount,
    expected_external_key: &str,
    expected_currency: &str,
) -> Result<uuid::Uuid, KillBillClientError> {
    if account.external_key != expected_external_key {
        return Err(KillBillClientError::invariant(
            "Kill Bill account external-key identity mismatch",
        ));
    }
    if account.currency != expected_currency {
        return Err(KillBillClientError::invariant(format!(
            "Kill Bill account currency mismatch: expected {expected_currency}, got {}",
            account.currency
        )));
    }
    Ok(account.account_id)
}

async fn resolve_payment_method(
    killbill: &KillBillClient,
    account_id: uuid::Uuid,
    mutation: &CanonicalMutation,
) -> Result<uuid::Uuid, KillBillClientError> {
    let token = mutation.payment_method_token.as_deref().ok_or_else(|| {
        KillBillClientError::invariant("initial payment omitted payment_method_token")
    })?;
    let external_key = payment_method_external_key(
        &mutation.merchant_id,
        mutation.money.currency.as_str(),
        token,
    );

    if let Some(payment_method_id) =
        find_payment_method(killbill.list_payment_methods(account_id).await?, account_id, &external_key)?
    {
        return Ok(payment_method_id);
    }

    match killbill
        .create_payment_method(account_id, external_key.clone())
        .await
    {
        Ok(payment_method) => verify_payment_method(payment_method, account_id, &external_key),
        Err(create_error) => {
            // Concurrent create or a lost create response is resolved by
            // re-listing and matching the deterministic synthetic PM key.
            match killbill.list_payment_methods(account_id).await {
                Ok(methods) => match find_payment_method(methods, account_id, &external_key)? {
                    Some(payment_method_id) => Ok(payment_method_id),
                    None => Err(create_error),
                },
                Err(_) => Err(create_error),
            }
        }
    }
}

fn find_payment_method(
    methods: Vec<KillBillPaymentMethod>,
    account_id: uuid::Uuid,
    expected_external_key: &str,
) -> Result<Option<uuid::Uuid>, KillBillClientError> {
    let matching: Vec<KillBillPaymentMethod> = methods
        .into_iter()
        .filter(|method| method.external_key == expected_external_key)
        .collect();

    match matching.as_slice() {
        [] => Ok(None),
        [method] => verify_payment_method((*method).clone(), account_id, expected_external_key).map(Some),
        _ => Err(KillBillClientError::invariant(
            "duplicate Kill Bill payment methods for deterministic external key",
        )),
    }
}

fn verify_payment_method(
    payment_method: KillBillPaymentMethod,
    account_id: uuid::Uuid,
    expected_external_key: &str,
) -> Result<uuid::Uuid, KillBillClientError> {
    if payment_method.account_id != account_id {
        return Err(KillBillClientError::invariant(
            "Kill Bill payment method belongs to a different account",
        ));
    }
    if payment_method.external_key != expected_external_key {
        return Err(KillBillClientError::invariant(
            "Kill Bill payment-method external-key identity mismatch",
        ));
    }
    if payment_method.plugin_name != PAYMENT_PLUGIN_NAME {
        return Err(KillBillClientError::invariant(format!(
            "Kill Bill payment method uses unexpected plugin {}",
            payment_method.plugin_name
        )));
    }
    Ok(payment_method.payment_method_id)
}

async fn insert_mutation(pool: &PgPool, mutation: &CanonicalMutation) -> Result<bool, ApiError> {
    let result = sqlx::query(
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

    Ok(result.rows_affected() == 1)
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

    let inserted = insert_mutation(pool, &mutation).await?;
    let row = load_operation_by_idempotency(pool, &mutation.idempotency_key).await?;

    if !stored_row_matches(&row, &mutation)? {
        return Err(ApiError::Conflict(
            "idempotency key is already bound to a conflicting mutation".into(),
        ));
    }

    let status = if inserted {
        StatusCode::ACCEPTED
    } else {
        StatusCode::OK
    };

    Ok((status, Json(view_from_row(&row)?)))
}

async fn load_operation_by_idempotency(
    pool: &PgPool,
    idempotency_key: &str,
) -> Result<PgRow, ApiError> {
    sqlx::query(
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
             payment_state,
             operation_state,
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
    .bind(idempotency_key)
    .fetch_optional(pool)
    .await
    .map_err(internal)?
    .ok_or_else(|| {
        ApiError::Conflict("operationId is already bound to a different idempotency key".into())
    })
}

async fn lock_initial_payment(
    tx: &mut Transaction<'_, Postgres>,
    payment_key: &str,
) -> Result<(), ApiError> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(payment_key)
        .execute(&mut **tx)
        .await
        .map_err(internal)?;
    Ok(())
}

async fn load_operation_for_update(
    tx: &mut Transaction<'_, Postgres>,
    idempotency_key: &str,
) -> Result<PgRow, ApiError> {
    sqlx::query(
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
             payment_state,
             operation_state,
             killbill_payment_id,
             killbill_transaction_id,
             hyperswitch_payment_id,
             hyperswitch_attempt_id,
             provider_payment_id,
             provider_operation_id,
             provider_idempotency_key
         FROM payment_reference_gateway.gateway_operation
         WHERE idempotency_key = $1
         FOR UPDATE",
    )
    .bind(idempotency_key)
    .fetch_optional(&mut **tx)
    .await
    .map_err(internal)?
    .ok_or_else(|| {
        ApiError::Conflict("operationId is already bound to a different idempotency key".into())
    })
}

async fn load_operation_by_id(
    connection: &mut PgConnection,
    operation_id: &str,
) -> Result<PgRow, ApiError> {
    sqlx::query(
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
             payment_state,
             operation_state,
             killbill_payment_id,
             killbill_transaction_id,
             hyperswitch_payment_id,
             hyperswitch_attempt_id,
             provider_payment_id,
             provider_operation_id,
             provider_idempotency_key
         FROM payment_reference_gateway.gateway_operation
         WHERE operation_id = $1",
    )
    .bind(operation_id)
    .fetch_one(connection)
    .await
    .map_err(internal)
}

async fn persist_initial_outcome(
    tx: &mut Transaction<'_, Postgres>,
    operation_id: &str,
    outcome: &KillBillMappedOutcome,
) -> Result<(), ApiError> {
    let payment_state = outcome.payment_state.unwrap_or(PaymentState::PaymentPending);

    sqlx::query(
        "UPDATE payment_reference_gateway.gateway_operation
         SET payment_state = $2,
             operation_state = $3,
             killbill_payment_id = $4,
             killbill_transaction_id = $5,
             provider_payment_id = COALESCE($6, provider_payment_id),
             provider_operation_id = COALESCE($7, provider_operation_id),
             provider_idempotency_key = COALESCE($8, provider_idempotency_key),
             updated_at = NOW()
         WHERE operation_id = $1",
    )
    .bind(operation_id)
    .bind(payment_state_name(payment_state))
    .bind(operation_state_name(outcome.operation_state))
    .bind(outcome.killbill_payment_id.to_string())
    .bind(outcome.killbill_transaction_id.to_string())
    .bind(outcome.provider_payment_id.as_deref())
    .bind(outcome.provider_operation_id.as_deref())
    .bind(outcome.provider_idempotency_key.as_deref())
    .execute(&mut **tx)
    .await
    .map_err(internal)?;

    Ok(())
}

async fn persist_initial_state(
    tx: &mut Transaction<'_, Postgres>,
    operation_id: &str,
    payment_state: PaymentState,
    operation_state: OperationState,
) -> Result<(), ApiError> {
    sqlx::query(
        "UPDATE payment_reference_gateway.gateway_operation
         SET payment_state = $2,
             operation_state = $3,
             updated_at = NOW()
         WHERE operation_id = $1",
    )
    .bind(operation_id)
    .bind(payment_state_name(payment_state))
    .bind(operation_state_name(operation_state))
    .execute(&mut **tx)
    .await
    .map_err(internal)?;

    Ok(())
}

async fn commit_initial_view(
    mut tx: Transaction<'_, Postgres>,
    operation_id: &str,
) -> Result<(StatusCode, Json<CanonicalPaymentView>), ApiError> {
    let row = load_operation_by_id(&mut *tx, operation_id).await?;
    let view = view_from_row(&row)?;
    let status = response_status(view.last_operation_state);
    tx.commit().await.map_err(internal)?;
    Ok((status, Json(view)))
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
             payment_state,
             operation_state,
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

fn stored_row_matches(row: &PgRow, mutation: &CanonicalMutation) -> Result<bool, ApiError> {
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

fn view_from_row(row: &PgRow) -> Result<CanonicalPaymentView, ApiError> {
    let payment_state: String = row.try_get("payment_state").map_err(internal)?;
    let operation_state: String = row.try_get("operation_state").map_err(internal)?;

    Ok(CanonicalPaymentView {
        merchant_payment_id: row.try_get("merchant_payment_id").map_err(internal)?,
        payment_state: parse_payment_state(&payment_state)?,
        last_operation_state: parse_operation_state(&operation_state)?,
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

fn validate_terminal_initial_mapping(view: &CanonicalPaymentView) -> Result<(), ApiError> {
    if view.last_operation_state == OperationState::Succeeded
        && (view.mapping.killbill_payment_id.is_none()
            || view.mapping.killbill_transaction_id.is_none())
    {
        return Err(ApiError::Internal(
            "completed initial payment is missing Kill Bill identity".into(),
        ));
    }
    Ok(())
}

fn is_terminal_operation(state: OperationState) -> bool {
    matches!(
        state,
        OperationState::Succeeded | OperationState::Failed | OperationState::ManualReview
    )
}

fn response_status(state: OperationState) -> StatusCode {
    match state {
        OperationState::Succeeded | OperationState::Failed => StatusCode::OK,
        OperationState::ManualReview => StatusCode::CONFLICT,
        OperationState::Pending | OperationState::Unknown | OperationState::RetryPending => {
            StatusCode::ACCEPTED
        }
    }
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

fn parse_payment_state(value: &str) -> Result<PaymentState, ApiError> {
    match value {
        "CREATED" => Ok(PaymentState::Created),
        "PAYMENT_PENDING" => Ok(PaymentState::PaymentPending),
        "AUTHORIZED" => Ok(PaymentState::Authorized),
        "PARTIALLY_CAPTURED" => Ok(PaymentState::PartiallyCaptured),
        "PAID" => Ok(PaymentState::Paid),
        "CANCELLED" => Ok(PaymentState::Cancelled),
        "PARTIALLY_REFUNDED" => Ok(PaymentState::PartiallyRefunded),
        "REFUNDED" => Ok(PaymentState::Refunded),
        "PAYMENT_FAILED" => Ok(PaymentState::PaymentFailed),
        "PAYMENT_UNKNOWN" => Ok(PaymentState::PaymentUnknown),
        _ => Err(ApiError::Internal(format!(
            "unknown persisted payment state: {value}"
        ))),
    }
}

fn parse_operation_state(value: &str) -> Result<OperationState, ApiError> {
    match value {
        "PENDING" => Ok(OperationState::Pending),
        "SUCCEEDED" => Ok(OperationState::Succeeded),
        "FAILED" => Ok(OperationState::Failed),
        "UNKNOWN" => Ok(OperationState::Unknown),
        "RETRY_PENDING" => Ok(OperationState::RetryPending),
        "MANUAL_REVIEW" => Ok(OperationState::ManualReview),
        _ => Err(ApiError::Internal(format!(
            "unknown persisted operation state: {value}"
        ))),
    }
}

fn log_killbill_error(context: &str, error: &KillBillClientError) {
    tracing::warn!(
        context,
        kind = ?error.kind,
        status = ?error.status,
        requires_read_before_retry = error.requires_read_before_retry,
        "Kill Bill request failed"
    );
}

fn upstream(context: &str, error: &KillBillClientError) -> ApiError {
    log_killbill_error(context, error);
    ApiError::Upstream(context.to_owned())
}

fn internal(error: impl std::fmt::Display) -> ApiError {
    tracing::error!(error = %error, "gateway persistence failure");
    ApiError::Internal("gateway persistence failure".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn persisted_state_names_round_trip() {
        for state in [
            PaymentState::Created,
            PaymentState::PaymentPending,
            PaymentState::Authorized,
            PaymentState::PartiallyCaptured,
            PaymentState::Paid,
            PaymentState::Cancelled,
            PaymentState::PartiallyRefunded,
            PaymentState::Refunded,
            PaymentState::PaymentFailed,
            PaymentState::PaymentUnknown,
        ] {
            assert_eq!(parse_payment_state(payment_state_name(state)).unwrap(), state);
        }

        for state in [
            OperationState::Pending,
            OperationState::Succeeded,
            OperationState::Failed,
            OperationState::Unknown,
            OperationState::RetryPending,
            OperationState::ManualReview,
        ] {
            assert_eq!(
                parse_operation_state(operation_state_name(state)).unwrap(),
                state
            );
        }
    }

    #[test]
    fn only_resolved_initial_operations_return_http_200() {
        assert_eq!(response_status(OperationState::Succeeded), StatusCode::OK);
        assert_eq!(response_status(OperationState::Failed), StatusCode::OK);
        assert_eq!(
            response_status(OperationState::Pending),
            StatusCode::ACCEPTED
        );
        assert_eq!(
            response_status(OperationState::Unknown),
            StatusCode::ACCEPTED
        );
        assert_eq!(
            response_status(OperationState::RetryPending),
            StatusCode::ACCEPTED
        );
        assert_eq!(
            response_status(OperationState::ManualReview),
            StatusCode::CONFLICT
        );
    }

    #[test]
    fn manual_review_is_terminal_and_never_auto_replayed() {
        assert!(is_terminal_operation(OperationState::ManualReview));
        assert!(!is_terminal_operation(OperationState::Unknown));
        assert!(!is_terminal_operation(OperationState::RetryPending));
    }
}
