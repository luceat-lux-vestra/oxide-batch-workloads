use std::collections::BTreeMap;
use std::fmt::Write as _;

use axum::{extract::State, http::StatusCode, Json};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::Row;

use super::{internal, require_non_blank, ApiError, AppState};

const STATEMENT_CONTRACT_VERSION: &str = "provider-settlement-statement-v1";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct SettlementCloseRequest {
    statement_id: String,
    statement_digest: String,
    provider_account_id: String,
    settlement_date: String,
    currency: String,
    line_count: i64,
    captured_total_minor: i64,
    refunded_total_minor: i64,
    net_settlement_total_minor: i64,
}

#[derive(Debug, Serialize)]
pub(super) struct SettlementCloseView {
    statement_id: String,
    statement_digest: String,
    provider_account_id: String,
    settlement_date: String,
    currency: String,
    line_count: i64,
    captured_total_minor: i64,
    refunded_total_minor: i64,
    net_settlement_total_minor: i64,
    settlement_state: String,
    replayed: bool,
}

pub(super) async fn close_settlement(
    State(state): State<AppState>,
    Json(request): Json<SettlementCloseRequest>,
) -> Result<(StatusCode, Json<SettlementCloseView>), ApiError> {
    validate_request(&request)?;

    let mut tx = state.pool.begin().await.map_err(internal)?;
    let inserted = sqlx::query(
        "INSERT INTO payment_reference_merchant.settlement_close
         (
             statement_id,
             statement_digest,
             provider_account_id,
             settlement_date,
             currency,
             line_count,
             captured_total_minor,
             refunded_total_minor,
             net_settlement_total_minor,
             settlement_state,
             closed_at
         )
         VALUES ($1, $2, $3, $4::text::date, $5, $6, $7, $8, $9, 'CLOSED', NOW())
         ON CONFLICT DO NOTHING",
    )
    .bind(&request.statement_id)
    .bind(&request.statement_digest)
    .bind(&request.provider_account_id)
    .bind(&request.settlement_date)
    .bind(&request.currency)
    .bind(request.line_count)
    .bind(request.captured_total_minor)
    .bind(request.refunded_total_minor)
    .bind(request.net_settlement_total_minor)
    .execute(&mut *tx)
    .await
    .map_err(internal)?;

    if inserted.rows_affected() == 1 {
        sqlx::query(
            "INSERT INTO payment_reference_merchant.business_event
             (aggregate_type, aggregate_id, event_type, payload)
             VALUES ('SETTLEMENT', $1, 'SETTLEMENT_CLOSED', $2::jsonb)",
        )
        .bind(&request.statement_id)
        .bind(
            serde_json::json!({
                "statement_id": request.statement_id,
                "statement_digest": request.statement_digest,
                "provider_account_id": request.provider_account_id,
                "settlement_date": request.settlement_date,
                "currency": request.currency,
                "line_count": request.line_count,
                "captured_total_minor": request.captured_total_minor,
                "refunded_total_minor": request.refunded_total_minor,
                "net_settlement_total_minor": request.net_settlement_total_minor,
            })
            .to_string(),
        )
        .execute(&mut *tx)
        .await
        .map_err(internal)?;

        tx.commit().await.map_err(internal)?;
        return Ok((StatusCode::CREATED, Json(view_from_request(request, false))));
    }

    let rows = sqlx::query(
        "SELECT
             statement_id,
             statement_digest,
             provider_account_id,
             settlement_date::text AS settlement_date,
             currency,
             line_count,
             captured_total_minor,
             refunded_total_minor,
             net_settlement_total_minor,
             settlement_state
         FROM payment_reference_merchant.settlement_close
         WHERE statement_id = $1
            OR (
                provider_account_id = $2
                AND settlement_date = $3::text::date
                AND currency = $4
            )
         FOR UPDATE",
    )
    .bind(&request.statement_id)
    .bind(&request.provider_account_id)
    .bind(&request.settlement_date)
    .bind(&request.currency)
    .fetch_all(&mut *tx)
    .await
    .map_err(internal)?;

    if rows.len() != 1 {
        return Err(ApiError::Conflict(
            "settlement identity conflicts with persisted Merchant settlement state".into(),
        ));
    }

    let row = &rows[0];
    let settlement_state: String = row.try_get("settlement_state").map_err(internal)?;
    if settlement_state == "HELD" {
        return Err(ApiError::Conflict(
            "settlement is HELD and cannot be closed".into(),
        ));
    }
    if settlement_state != "CLOSED" {
        return Err(ApiError::Internal(format!(
            "unknown persisted settlement state: {settlement_state}"
        )));
    }

    if !stored_close_matches(row, &request)? {
        return Err(ApiError::Conflict(
            "statement identity already exists with different digest or totals".into(),
        ));
    }

    tx.commit().await.map_err(internal)?;
    Ok((StatusCode::OK, Json(view_from_request(request, true))))
}

fn stored_close_matches(
    row: &sqlx::postgres::PgRow,
    request: &SettlementCloseRequest,
) -> Result<bool, ApiError> {
    Ok(
        row.try_get::<String, _>("statement_id").map_err(internal)? == request.statement_id
            && row
                .try_get::<String, _>("statement_digest")
                .map_err(internal)?
                == request.statement_digest
            && row
                .try_get::<String, _>("provider_account_id")
                .map_err(internal)?
                == request.provider_account_id
            && row
                .try_get::<String, _>("settlement_date")
                .map_err(internal)?
                == request.settlement_date
            && row.try_get::<String, _>("currency").map_err(internal)? == request.currency
            && row.try_get::<i64, _>("line_count").map_err(internal)? == request.line_count
            && row
                .try_get::<i64, _>("captured_total_minor")
                .map_err(internal)?
                == request.captured_total_minor
            && row
                .try_get::<i64, _>("refunded_total_minor")
                .map_err(internal)?
                == request.refunded_total_minor
            && row
                .try_get::<i64, _>("net_settlement_total_minor")
                .map_err(internal)?
                == request.net_settlement_total_minor,
    )
}

fn view_from_request(request: SettlementCloseRequest, replayed: bool) -> SettlementCloseView {
    SettlementCloseView {
        statement_id: request.statement_id,
        statement_digest: request.statement_digest,
        provider_account_id: request.provider_account_id,
        settlement_date: request.settlement_date,
        currency: request.currency,
        line_count: request.line_count,
        captured_total_minor: request.captured_total_minor,
        refunded_total_minor: request.refunded_total_minor,
        net_settlement_total_minor: request.net_settlement_total_minor,
        settlement_state: "CLOSED".into(),
        replayed,
    }
}

fn validate_request(request: &SettlementCloseRequest) -> Result<(), ApiError> {
    require_non_blank("statement_id", &request.statement_id)?;
    require_non_blank("statement_digest", &request.statement_digest)?;
    require_non_blank("provider_account_id", &request.provider_account_id)?;

    if !has_prefixed_lower_hex(&request.statement_id, "statement-", 64) {
        return Err(ApiError::BadRequest(
            "statement_id must be statement-<64 lowercase hex>".into(),
        ));
    }
    if !has_prefixed_lower_hex(&request.statement_digest, "sha256:", 64) {
        return Err(ApiError::BadRequest(
            "statement_digest must be sha256:<64 lowercase hex>".into(),
        ));
    }
    if !is_iso_date(&request.settlement_date) {
        return Err(ApiError::BadRequest(
            "settlement_date must be a valid YYYY-MM-DD date".into(),
        ));
    }
    if request.currency.len() != 3
        || !request
            .currency
            .bytes()
            .all(|byte| byte.is_ascii_uppercase())
    {
        return Err(ApiError::BadRequest(
            "currency must be an uppercase three-letter code".into(),
        ));
    }
    if request.line_count <= 0 {
        return Err(ApiError::BadRequest("line_count must be positive".into()));
    }
    if request.captured_total_minor < 0
        || request.refunded_total_minor < 0
        || request.net_settlement_total_minor < 0
    {
        return Err(ApiError::BadRequest(
            "settlement totals must be nonnegative".into(),
        ));
    }
    if request.refunded_total_minor > request.captured_total_minor {
        return Err(ApiError::BadRequest(
            "refunded_total_minor cannot exceed captured_total_minor".into(),
        ));
    }
    let expected_net = request
        .captured_total_minor
        .checked_sub(request.refunded_total_minor)
        .ok_or_else(|| ApiError::BadRequest("settlement total arithmetic overflow".into()))?;
    if request.net_settlement_total_minor != expected_net {
        return Err(ApiError::BadRequest(
            "net_settlement_total_minor must equal captured minus refunded".into(),
        ));
    }

    let expected_statement_id = deterministic_statement_id(request)?;
    if request.statement_id != expected_statement_id {
        return Err(ApiError::BadRequest(
            "statement_id does not match the canonical settlement identity".into(),
        ));
    }
    Ok(())
}

fn deterministic_statement_id(request: &SettlementCloseRequest) -> Result<String, ApiError> {
    let mut identity = BTreeMap::new();
    identity.insert("currency", request.currency.clone());
    identity.insert("provider_account_id", request.provider_account_id.clone());
    identity.insert("settlement_date", request.settlement_date.clone());
    identity.insert(
        "statement_contract_version",
        STATEMENT_CONTRACT_VERSION.to_owned(),
    );
    let canonical =
        serde_json::to_vec(&identity).map_err(|error| ApiError::Internal(error.to_string()))?;
    Ok(format!("statement-{}", sha256_hex(&canonical)))
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut output = String::with_capacity(64);
    for byte in digest {
        write!(&mut output, "{byte:02x}").expect("writing a SHA-256 digest to String cannot fail");
    }
    output
}

fn has_prefixed_lower_hex(value: &str, prefix: &str, digits: usize) -> bool {
    value.strip_prefix(prefix).is_some_and(|suffix| {
        suffix.len() == digits
            && suffix
                .bytes()
                .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    })
}

fn is_iso_date(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 10
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes
            .iter()
            .enumerate()
            .any(|(index, byte)| index != 4 && index != 7 && !byte.is_ascii_digit())
    {
        return false;
    }

    let year = value[0..4].parse::<u32>().ok();
    let month = value[5..7].parse::<u32>().ok();
    let day = value[8..10].parse::<u32>().ok();
    let (Some(year), Some(month), Some(day)) = (year, month, day) else {
        return false;
    };
    if year == 0 || !(1..=12).contains(&month) {
        return false;
    }
    let max_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        _ => return false,
    };
    (1..=max_day).contains(&day)
}

fn is_leap_year(year: u32) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}
