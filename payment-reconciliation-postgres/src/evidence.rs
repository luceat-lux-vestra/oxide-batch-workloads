use anyhow::Result;
use serde::Serialize;
use sqlx::{PgPool, Row};

use crate::model::{InternalSnapshotRow, ProviderSnapshotRow, SnapshotMetadata};
use crate::oracle::{reconcile, OracleDetail, OracleSummary};

#[derive(Debug, Serialize)]
pub struct ProducerEvidence {
    pub schema_version: u32,
    pub campaign_issue: u32,
    pub classification: &'static str,
    pub snapshot: RetainedSnapshot,
    pub oracle: OracleSummary,
    pub actual: ActualResult,
    pub violations: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct RetainedSnapshot {
    pub metadata: SnapshotMetadata,
    pub internal: Vec<InternalSnapshotRow>,
    pub provider: Vec<ProviderSnapshotRow>,
}

#[derive(Debug, Serialize)]
pub struct ActualResult {
    pub run_id: Option<String>,
    pub processing_state: Option<String>,
    pub business_state: Option<String>,
    pub mismatch_count: Option<i64>,
    pub aggregate_delta_minor: Option<i64>,
    pub result_digest: Option<String>,
    pub details: Vec<ActualDetail>,
    pub published_count: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ActualDetail {
    pub provider_payment_id: String,
    pub classification: String,
    pub internal_row_count: i64,
    pub provider_row_count: i64,
    pub internal_currency: Option<String>,
    pub provider_currency: Option<String>,
    pub internal_status: Option<String>,
    pub provider_status: Option<String>,
    pub authorized_delta_minor: Option<i64>,
    pub captured_delta_minor: Option<i64>,
    pub refunded_delta_minor: Option<i64>,
}

pub async fn verify(database_url: &str, snapshot_id: &str) -> Result<ProducerEvidence> {
    let bundle = crate::snapshot::load_integrity_checked_snapshot(database_url, snapshot_id).await?;
    let oracle = reconcile(&bundle.internal, &bundle.provider)?;
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;
    let actual = load_actual(&pool, snapshot_id).await?;
    pool.close().await;

    let mut violations = Vec::new();
    compare_actual(&oracle, &actual, &mut violations);

    Ok(ProducerEvidence {
        schema_version: 1,
        campaign_issue: 148,
        classification: "supplemental exact-artifact v0.6.0 business/reference-system evidence",
        snapshot: RetainedSnapshot {
            metadata: bundle.metadata,
            internal: bundle.internal,
            provider: bundle.provider,
        },
        oracle,
        actual,
        violations,
    })
}

async fn load_actual(pool: &PgPool, snapshot_id: &str) -> Result<ActualResult> {
    let run = sqlx::query(
        "SELECT run_id, processing_state, business_state, mismatch_count,
                aggregate_delta_minor, result_digest
         FROM payment_reconciliation.reconciliation_run
         WHERE snapshot_id = $1",
    )
    .bind(snapshot_id)
    .fetch_optional(pool)
    .await?;

    let Some(run) = run else {
        return Ok(ActualResult {
            run_id: None,
            processing_state: None,
            business_state: None,
            mismatch_count: None,
            aggregate_delta_minor: None,
            result_digest: None,
            details: Vec::new(),
            published_count: 0,
        });
    };

    let run_id: String = run.try_get("run_id")?;
    let rows = sqlx::query(
        "SELECT provider_payment_id, classification,
                internal_row_count::BIGINT AS internal_row_count,
                provider_row_count::BIGINT AS provider_row_count,
                internal_currency, provider_currency, internal_status, provider_status,
                authorized_delta_minor, captured_delta_minor, refunded_delta_minor
         FROM payment_reconciliation.reconciliation_detail
         WHERE run_id = $1
         ORDER BY provider_payment_id",
    )
    .bind(&run_id)
    .fetch_all(pool)
    .await?;

    let details = rows
        .into_iter()
        .map(|row| {
            Ok(ActualDetail {
                provider_payment_id: row.try_get("provider_payment_id")?,
                classification: row.try_get("classification")?,
                internal_row_count: row.try_get("internal_row_count")?,
                provider_row_count: row.try_get("provider_row_count")?,
                internal_currency: row.try_get("internal_currency")?,
                provider_currency: row.try_get("provider_currency")?,
                internal_status: row.try_get("internal_status")?,
                provider_status: row.try_get("provider_status")?,
                authorized_delta_minor: row.try_get("authorized_delta_minor")?,
                captured_delta_minor: row.try_get("captured_delta_minor")?,
                refunded_delta_minor: row.try_get("refunded_delta_minor")?,
            })
        })
        .collect::<std::result::Result<Vec<_>, sqlx::Error>>()?;

    let published_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT
         FROM payment_reconciliation.published_reconciliation
         WHERE run_id = $1",
    )
    .bind(&run_id)
    .fetch_one(pool)
    .await?;

    let details = details
        .into_iter()
        .map(normalize_actual_detail)
        .collect::<Vec<_>>();

    Ok(ActualResult {
        run_id: Some(run_id),
        processing_state: Some(run.try_get("processing_state")?),
        business_state: run.try_get("business_state")?,
        mismatch_count: run.try_get("mismatch_count")?,
        aggregate_delta_minor: run.try_get("aggregate_delta_minor")?,
        result_digest: run.try_get("result_digest")?,
        details,
        published_count,
    })
}

fn normalize_actual_detail(mut detail: ActualDetail) -> ActualDetail {
    let internal_single = detail.internal_row_count == 1;
    let provider_single = detail.provider_row_count == 1;
    let paired = internal_single && provider_single;

    if !internal_single {
        detail.internal_currency = None;
        detail.internal_status = None;
    }
    if !provider_single {
        detail.provider_currency = None;
        detail.provider_status = None;
    }
    if !paired {
        detail.authorized_delta_minor = None;
        detail.captured_delta_minor = None;
        detail.refunded_delta_minor = None;
    }
    detail
}

fn compare_actual(oracle: &OracleSummary, actual: &ActualResult, violations: &mut Vec<String>) {
    if actual.processing_state.as_deref() != Some("PROCESSED") {
        violations.push("processing_state must be PROCESSED".into());
    }
    if actual.business_state.as_deref() != Some(oracle.business_state) {
        violations.push("business_state disagrees with independent oracle".into());
    }
    if actual.mismatch_count != Some(oracle.mismatch_count as i64) {
        violations.push("mismatch_count disagrees with independent oracle".into());
    }
    if actual.aggregate_delta_minor != Some(oracle.aggregate_delta_minor) {
        violations.push("aggregate_delta_minor disagrees with independent oracle".into());
    }
    if actual.result_digest.as_deref() != Some(oracle.result_digest.as_str()) {
        violations.push("result_digest disagrees with independent oracle".into());
    }

    let expected = oracle
        .details
        .iter()
        .map(expected_detail)
        .collect::<Vec<_>>();
    if actual.details != expected {
        violations.push("reconciliation detail rows disagree with independent oracle".into());
    }

    match oracle.business_state {
        "CLEAR" if actual.published_count != 1 => {
            violations.push("CLEAR must retain exactly one publication".into())
        }
        "HELD" if actual.published_count != 0 => {
            violations.push("HELD must retain zero publications".into())
        }
        _ => {}
    }
}

fn expected_detail(detail: &OracleDetail) -> ActualDetail {
    ActualDetail {
        provider_payment_id: detail.provider_payment_id.clone(),
        classification: serde_json::to_value(detail.classification)
            .ok()
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_else(|| "INVALID".into()),
        internal_row_count: detail.internal_row_count as i64,
        provider_row_count: detail.provider_row_count as i64,
        internal_currency: detail.internal_currency.clone(),
        provider_currency: detail.provider_currency.clone(),
        internal_status: detail.internal_status.clone(),
        provider_status: detail.provider_status.clone(),
        authorized_delta_minor: detail.authorized_delta_minor,
        captured_delta_minor: detail.captured_delta_minor,
        refunded_delta_minor: detail.refunded_delta_minor,
    }
}
