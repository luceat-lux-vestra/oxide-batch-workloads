use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FinalizationReport {
    pub run_id: String,
    pub business_state: String,
    pub mismatch_count: i64,
    pub aggregate_delta_minor: i64,
    pub result_digest: String,
    pub published_count: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct FinalizationDetail {
    provider_payment_id: String,
    classification: String,
    internal_row_count: i64,
    provider_row_count: i64,
    internal_currency: String,
    provider_currency: String,
    internal_status: String,
    provider_status: String,
    authorized_delta_minor: i64,
    captured_delta_minor: i64,
    refunded_delta_minor: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct CanonicalDetail {
    provider_payment_id: String,
    classification: String,
    internal_row_count: i64,
    provider_row_count: i64,
    internal_currency: Option<String>,
    provider_currency: Option<String>,
    internal_status: Option<String>,
    provider_status: Option<String>,
    authorized_delta_minor: Option<i64>,
    captured_delta_minor: Option<i64>,
    refunded_delta_minor: Option<i64>,
}

pub async fn finalize(
    database_url: &str,
    snapshot_id: &str,
    crash_point: Option<crate::finalize_failpoint::FinalizeCrashPoint>,
    pause_for_kill: Option<PathBuf>,
) -> Result<FinalizationReport> {
    let bundle =
        crate::snapshot::load_integrity_checked_snapshot(database_url, snapshot_id).await?;
    let run_id = crate::job::run_id(&bundle);
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;
    let crash = match (crash_point, pause_for_kill) {
        (None, None) => None,
        (Some(point), Some(marker)) => Some((point, marker)),
        (Some(_), None) => bail!("--finalize-crash-point requires --pause-for-kill"),
        (None, Some(_)) => bail!("--pause-for-kill requires --finalize-crash-point"),
    };
    let report = finalize_with_pool(&pool, &run_id, crash).await?;
    pool.close().await;
    Ok(report)
}

async fn finalize_with_pool(
    pool: &PgPool,
    run_id: &str,
    crash: Option<(crate::finalize_failpoint::FinalizeCrashPoint, PathBuf)>,
) -> Result<FinalizationReport> {
    let mut tx = pool.begin().await?;
    let run = sqlx::query(
        "SELECT processing_state, business_state, mismatch_count,
                aggregate_delta_minor, result_digest
         FROM payment_reconciliation.reconciliation_run
         WHERE run_id = $1
         FOR UPDATE",
    )
    .bind(run_id)
    .fetch_optional(&mut *tx)
    .await?
    .with_context(|| format!("reconciliation run not found: {run_id}"))?;

    let processing_state: String = run.try_get("processing_state")?;
    if processing_state != "PROCESSED" {
        bail!("run {run_id} is not ready for finalization: {processing_state}");
    }

    let rows = sqlx::query(
        "SELECT provider_payment_id, classification,
                internal_row_count::BIGINT AS internal_row_count,
                provider_row_count::BIGINT AS provider_row_count,
                COALESCE(internal_currency, '') AS internal_currency,
                COALESCE(provider_currency, '') AS provider_currency,
                COALESCE(internal_status, '') AS internal_status,
                COALESCE(provider_status, '') AS provider_status,
                COALESCE(authorized_delta_minor, 0)::BIGINT AS authorized_delta_minor,
                COALESCE(captured_delta_minor, 0)::BIGINT AS captured_delta_minor,
                COALESCE(refunded_delta_minor, 0)::BIGINT AS refunded_delta_minor
         FROM payment_reconciliation.reconciliation_detail
         WHERE run_id = $1
         ORDER BY provider_payment_id",
    )
    .bind(run_id)
    .fetch_all(&mut *tx)
    .await?;

    if rows.is_empty() {
        bail!("run {run_id} has no committed reconciliation detail");
    }

    let details = rows
        .into_iter()
        .map(|row| {
            Ok(FinalizationDetail {
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

    let (business_state, mismatch_count, aggregate_delta_minor, result_digest) =
        derive_finalization(&details)?;

    let stored_business_state: Option<String> = run.try_get("business_state")?;
    if let Some(stored_state) = stored_business_state {
        let stored_mismatch: Option<i64> = run.try_get("mismatch_count")?;
        let stored_delta: Option<i64> = run.try_get("aggregate_delta_minor")?;
        let stored_digest: Option<String> = run.try_get("result_digest")?;
        if stored_state != business_state
            || stored_mismatch != Some(mismatch_count)
            || stored_delta != Some(aggregate_delta_minor)
            || stored_digest.as_deref() != Some(result_digest.as_str())
        {
            bail!("stored finalization disagrees with committed reconciliation detail");
        }
    } else {
        sqlx::query(
            "UPDATE payment_reconciliation.reconciliation_run
             SET business_state = $2,
                 mismatch_count = $3,
                 aggregate_delta_minor = $4,
                 result_digest = $5,
                 finalized_at = NOW(),
                 updated_at = NOW()
             WHERE run_id = $1 AND business_state IS NULL",
        )
        .bind(run_id)
        .bind(&business_state)
        .bind(mismatch_count)
        .bind(aggregate_delta_minor)
        .bind(&result_digest)
        .execute(&mut *tx)
        .await?;

        if business_state == "CLEAR" {
            sqlx::query(
                "INSERT INTO payment_reconciliation.published_reconciliation
                 (run_id, result_digest)
                 VALUES ($1, $2)
                 ON CONFLICT (run_id) DO NOTHING",
            )
            .bind(run_id)
            .bind(&result_digest)
            .execute(&mut *tx)
            .await?;
        }
    }

    let published_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT
         FROM payment_reconciliation.published_reconciliation
         WHERE run_id = $1",
    )
    .bind(run_id)
    .fetch_one(&mut *tx)
    .await?;

    match business_state.as_str() {
        "CLEAR" if published_count != 1 => {
            bail!("CLEAR finalization must have exactly one publication row")
        }
        "HELD" if published_count != 0 => {
            bail!("HELD finalization must have zero publication rows")
        }
        _ => {}
    }

    if let Some((crate::finalize_failpoint::FinalizeCrashPoint::BeforeCommit, marker)) =
        crash.as_ref()
    {
        crate::finalize_failpoint::pause(
            marker,
            crate::finalize_failpoint::FinalizeCrashPoint::BeforeCommit,
        )
        .await;
    }

    tx.commit().await?;

    if let Some((crate::finalize_failpoint::FinalizeCrashPoint::AfterCommit, marker)) =
        crash.as_ref()
    {
        crate::finalize_failpoint::pause(
            marker,
            crate::finalize_failpoint::FinalizeCrashPoint::AfterCommit,
        )
        .await;
    }

    Ok(FinalizationReport {
        run_id: run_id.to_owned(),
        business_state,
        mismatch_count,
        aggregate_delta_minor,
        result_digest,
        published_count,
    })
}

fn canonical_detail(detail: &FinalizationDetail) -> CanonicalDetail {
    let internal_single = detail.internal_row_count == 1;
    let provider_single = detail.provider_row_count == 1;
    let paired = internal_single && provider_single;

    CanonicalDetail {
        provider_payment_id: detail.provider_payment_id.clone(),
        classification: detail.classification.clone(),
        internal_row_count: detail.internal_row_count,
        provider_row_count: detail.provider_row_count,
        internal_currency: internal_single.then(|| detail.internal_currency.clone()),
        provider_currency: provider_single.then(|| detail.provider_currency.clone()),
        internal_status: internal_single.then(|| detail.internal_status.clone()),
        provider_status: provider_single.then(|| detail.provider_status.clone()),
        authorized_delta_minor: paired.then_some(detail.authorized_delta_minor),
        captured_delta_minor: paired.then_some(detail.captured_delta_minor),
        refunded_delta_minor: paired.then_some(detail.refunded_delta_minor),
    }
}

fn derive_finalization(details: &[FinalizationDetail]) -> Result<(String, i64, i64, String)> {
    let mismatch_count = details
        .iter()
        .filter(|detail| detail.classification != "MATCH")
        .count() as i64;

    let aggregate_delta_minor = details.iter().try_fold(0_i64, |acc, detail| {
        acc.checked_add(detail.authorized_delta_minor)
            .and_then(|value| value.checked_add(detail.captured_delta_minor))
            .and_then(|value| value.checked_add(detail.refunded_delta_minor))
            .ok_or_else(|| anyhow::anyhow!("finalization aggregate delta overflow"))
    })?;

    let business_state = if mismatch_count == 0 && aggregate_delta_minor == 0 {
        "CLEAR"
    } else {
        "HELD"
    }
    .to_owned();

    let canonical = details.iter().map(canonical_detail).collect::<Vec<_>>();

    let mut hasher = Sha256::new();
    hasher.update(serde_json::to_vec(&canonical)?);
    hasher.update(b"\n");
    hasher.update(aggregate_delta_minor.to_string().as_bytes());
    hasher.update(b"\n");
    hasher.update(business_state.as_bytes());

    Ok((
        business_state,
        mismatch_count,
        aggregate_delta_minor,
        format!("{:x}", hasher.finalize()),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn detail(classification: &str, delta: i64) -> FinalizationDetail {
        FinalizationDetail {
            provider_payment_id: "provider-1".into(),
            classification: classification.into(),
            internal_row_count: 1,
            provider_row_count: 1,
            internal_currency: "USD".into(),
            provider_currency: "USD".into(),
            internal_status: "CAPTURED".into(),
            provider_status: "CAPTURED".into(),
            authorized_delta_minor: delta,
            captured_delta_minor: 0,
            refunded_delta_minor: 0,
        }
    }

    #[test]
    fn clear_requires_no_mismatch_and_zero_delta() {
        let (state, mismatch, delta, _) = derive_finalization(&[detail("MATCH", 0)]).unwrap();
        assert_eq!(state, "CLEAR");
        assert_eq!(mismatch, 0);
        assert_eq!(delta, 0);
    }

    #[test]
    fn any_business_mismatch_holds_publication() {
        let (state, mismatch, _, _) =
            derive_finalization(&[detail("ATTRIBUTE_MISMATCH", 0)]).unwrap();
        assert_eq!(state, "HELD");
        assert_eq!(mismatch, 1);
    }

    #[test]
    fn a_nonzero_financial_delta_holds_even_if_classification_is_corruptly_match() {
        let (state, _, delta, _) = derive_finalization(&[detail("MATCH", 1)]).unwrap();
        assert_eq!(state, "HELD");
        assert_eq!(delta, 1);
    }
}
