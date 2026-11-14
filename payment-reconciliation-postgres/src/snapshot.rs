use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::json;
use sqlx::{postgres::PgPoolOptions, PgPool, Row};
use tokio::process::Command;

use crate::model::{
    internal_digest, normalize_internal_status, normalize_provider_status, provider_digest,
    validate_currency, InternalSnapshotRow, ProviderSnapshotRow, SnapshotBundle, SnapshotMetadata,
    CONTRACT_VERSION, DEFAULT_POLICY_VERSION,
};
use crate::oracle::{contains_classification, reconcile, Classification};

pub struct AcquireRequest<'a> {
    pub database_url: &'a str,
    pub merchant_database_url: &'a str,
    pub provider_base_url: &'a str,
    pub snapshot_id: &'a str,
    pub business_date: &'a str,
    pub provider_scope: &'a str,
    pub policy_version: Option<&'a str>,
}

pub struct DeriveRequest<'a> {
    pub database_url: &'a str,
    pub base_snapshot_id: &'a str,
    pub snapshot_id: &'a str,
    pub scenario_case: &'a str,
    pub delta_minor: i64,
}

#[derive(Debug, Deserialize)]
struct ProviderPaymentResponse {
    id: String,
    amount: i64,
    currency: String,
    status: String,
    authorized_amount: i64,
    captured_amount: i64,
    refunded_amount: i64,
    version: i64,
}

pub async fn migrate(database_url: &str) -> Result<()> {
    let pool = workload_pool(database_url).await?;
    sqlx::migrate!().run(&pool).await?;
    Ok(())
}

pub async fn acquire_base_snapshot(request: AcquireRequest<'_>) -> Result<SnapshotBundle> {
    validate_snapshot_identity(request.snapshot_id)?;
    validate_business_date(request.business_date)?;
    if request.provider_scope.trim().is_empty() {
        bail!("provider scope must not be blank");
    }

    let workload_pool = workload_pool(request.database_url).await?;
    let merchant_pool = merchant_pool(request.merchant_database_url).await?;

    let internal = load_merchant_rows(
        &merchant_pool,
        request.provider_scope,
        request.business_date,
    )
    .await?;
    if internal.is_empty() {
        bail!(
            "no eligible Merchant payments found for scope={} business_date={}",
            request.provider_scope,
            request.business_date
        );
    }

    let mut provider = Vec::with_capacity(internal.len());
    for row in &internal {
        provider.push(
            fetch_provider_row(request.provider_base_url, &row.provider_payment_id).await?,
        );
    }

    let oracle = reconcile(&internal, &provider)?;
    if oracle.business_state != "CLEAR" {
        bail!(
            "base payment-reference snapshot is not CLEAR: {}",
            serde_json::to_string(&oracle)?
        );
    }

    let metadata = SnapshotMetadata {
        snapshot_id: request.snapshot_id.to_owned(),
        business_date: request.business_date.to_owned(),
        provider_scope: request.provider_scope.to_owned(),
        scenario_case: "CLEAR".into(),
        base_snapshot_id: None,
        internal_snapshot_digest: internal_digest(&internal)?,
        provider_snapshot_digest: provider_digest(&provider)?,
        policy_version: request
            .policy_version
            .unwrap_or(DEFAULT_POLICY_VERSION)
            .to_owned(),
    };

    let bundle = SnapshotBundle {
        metadata,
        internal,
        provider,
    };
    persist_snapshot(
        &workload_pool,
        &bundle,
        &json!({
            "kind": "payment-reference-base",
            "merchant_source": "payment_reference_merchant PostgreSQL",
            "provider_source": "payment-sandbox GET /payments/{provider_payment_id}",
            "business_date_basis": "Merchant created_at projected to UTC date"
        })
        .to_string(),
    )
    .await?;

    Ok(bundle)
}

pub async fn derive_scenario(request: DeriveRequest<'_>) -> Result<SnapshotBundle> {
    validate_snapshot_identity(request.snapshot_id)?;
    if request.snapshot_id == request.base_snapshot_id {
        bail!("derived snapshot id must differ from base snapshot id");
    }

    let pool = workload_pool(request.database_url).await?;
    let base = load_snapshot(&pool, request.base_snapshot_id).await?;
    validate_bundle_integrity(&base)?;
    if base.metadata.scenario_case != "CLEAR" {
        bail!("derived scenarios require a CLEAR base snapshot");
    }
    let base_oracle = reconcile(&base.internal, &base.provider)?;
    if base_oracle.business_state != "CLEAR" {
        bail!("base snapshot no longer satisfies the independent CLEAR oracle");
    }

    let target = base
        .internal
        .iter()
        .map(|row| row.provider_payment_id.as_str())
        .find(|provider_id| {
            base.provider
                .iter()
                .any(|row| row.provider_payment_id == *provider_id)
        })
        .context("base snapshot contains no paired provider payment")?
        .to_owned();

    let mut internal = base.internal.clone();
    let mut provider = base.provider.clone();
    let recipe = match request.scenario_case {
        "MISSING_PROVIDER" => {
            provider.retain(|row| row.provider_payment_id != target);
            json!({"kind":"MISSING_PROVIDER","target_provider_payment_id":target})
        }
        "MISSING_INTERNAL" => {
            internal.retain(|row| row.provider_payment_id != target);
            json!({"kind":"MISSING_INTERNAL","target_provider_payment_id":target})
        }
        "DUPLICATE_PROVIDER" => {
            let source = provider
                .iter()
                .find(|row| row.provider_payment_id == target)
                .context("target provider row disappeared")?
                .clone();
            let mut duplicate = source;
            duplicate.source_row_id = format!("{}#duplicate-1", duplicate.source_row_id);
            provider.push(duplicate);
            json!({"kind":"DUPLICATE_PROVIDER","target_provider_payment_id":target})
        }
        "AMOUNT_MISMATCH" => {
            if request.delta_minor <= 0 {
                bail!("amount mismatch delta must be a positive minor-unit value");
            }
            let row = provider
                .iter_mut()
                .find(|row| row.provider_payment_id == target)
                .context("target provider row disappeared")?;
            row.authorized_amount_minor = row
                .authorized_amount_minor
                .checked_add(request.delta_minor)
                .context("amount mismatch mutation overflow")?;
            json!({
                "kind":"AMOUNT_MISMATCH",
                "target_provider_payment_id":target,
                "field":"authorized_amount_minor",
                "delta_minor":request.delta_minor
            })
        }
        other => bail!("unsupported deterministic derived scenario: {other}"),
    };

    let oracle = reconcile(&internal, &provider)?;
    let expected = expected_classification(request.scenario_case)?;
    if oracle.business_state != "HELD" || !contains_classification(&oracle, expected) {
        bail!(
            "derived scenario does not produce the expected HELD oracle result: {}",
            serde_json::to_string(&oracle)?
        );
    }

    let metadata = SnapshotMetadata {
        snapshot_id: request.snapshot_id.to_owned(),
        business_date: base.metadata.business_date.clone(),
        provider_scope: base.metadata.provider_scope.clone(),
        scenario_case: request.scenario_case.to_owned(),
        base_snapshot_id: Some(base.metadata.snapshot_id.clone()),
        internal_snapshot_digest: internal_digest(&internal)?,
        provider_snapshot_digest: provider_digest(&provider)?,
        policy_version: base.metadata.policy_version.clone(),
    };
    let bundle = SnapshotBundle {
        metadata,
        internal,
        provider,
    };
    persist_snapshot(&pool, &bundle, &recipe.to_string()).await?;
    Ok(bundle)
}

pub async fn verify_source_snapshot(database_url: &str, snapshot_id: &str) -> Result<serde_json::Value> {
    let pool = workload_pool(database_url).await?;
    let bundle = load_snapshot(&pool, snapshot_id).await?;
    validate_bundle_integrity(&bundle)?;
    let oracle = reconcile(&bundle.internal, &bundle.provider)?;
    validate_scenario_expectation(&bundle.metadata.scenario_case, &oracle)?;

    Ok(json!({
        "schema_version": 1,
        "scope": "source-snapshot-only-v1",
        "snapshot_id": bundle.metadata.snapshot_id,
        "scenario_case": bundle.metadata.scenario_case,
        "internal_snapshot_digest": bundle.metadata.internal_snapshot_digest,
        "provider_snapshot_digest": bundle.metadata.provider_snapshot_digest,
        "oracle": oracle
    }))
}

async fn workload_pool(database_url: &str) -> Result<PgPool> {
    PgPoolOptions::new()
        .max_connections(5)
        .connect(database_url)
        .await
        .context("connect workload PostgreSQL")
}

async fn merchant_pool(database_url: &str) -> Result<PgPool> {
    PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await
        .context("connect Merchant PostgreSQL")
}

async fn load_merchant_rows(
    pool: &PgPool,
    provider_scope: &str,
    business_date: &str,
) -> Result<Vec<InternalSnapshotRow>> {
    let mut tx = pool.begin().await?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        .execute(&mut *tx)
        .await?;

    let rows = sqlx::query(
        r#"
        SELECT
            mp.merchant_payment_id,
            epm.provider_payment_id,
            mp.currency,
            mp.payment_state,
            COALESCE(SUM(
                CASE
                    WHEN mpo.operation_state = 'SUCCEEDED'
                     AND mpo.operation_kind IN ('AUTHORIZE', 'AUTH_CAPTURE')
                    THEN mpo.amount_minor
                    ELSE 0
                END
            ), 0)::BIGINT AS authorized_amount_minor,
            COALESCE(SUM(
                CASE
                    WHEN mpo.operation_state = 'SUCCEEDED'
                     AND mpo.operation_kind IN ('AUTH_CAPTURE', 'CAPTURE')
                    THEN mpo.amount_minor
                    ELSE 0
                END
            ), 0)::BIGINT AS captured_amount_minor,
            COALESCE(SUM(
                CASE
                    WHEN mpo.operation_state = 'SUCCEEDED'
                     AND mpo.operation_kind = 'REFUND'
                    THEN mpo.amount_minor
                    ELSE 0
                END
            ), 0)::BIGINT AS refunded_amount_minor,
            COUNT(*) FILTER (
                WHERE mpo.operation_state = 'SUCCEEDED'
                  AND mpo.amount_minor IS NULL
            )::BIGINT AS missing_amount_count
        FROM payment_reference_merchant.merchant_payment mp
        JOIN payment_reference_merchant.orders o
          ON o.merchant_order_id = mp.merchant_order_id
        JOIN payment_reference_merchant.external_payment_mapping epm
          ON epm.merchant_payment_id = mp.merchant_payment_id
        LEFT JOIN payment_reference_merchant.merchant_payment_operation mpo
          ON mpo.merchant_payment_id = mp.merchant_payment_id
        WHERE o.merchant_id = $1
          AND (mp.created_at AT TIME ZONE 'UTC')::date = $2::date
          AND mp.payment_state IN ('AUTHORIZED', 'PAID', 'REFUNDED')
          AND epm.provider_payment_id IS NOT NULL
        GROUP BY
            mp.merchant_payment_id,
            epm.provider_payment_id,
            mp.currency,
            mp.payment_state
        ORDER BY epm.provider_payment_id, mp.merchant_payment_id
        "#,
    )
    .bind(provider_scope)
    .bind(business_date)
    .fetch_all(&mut *tx)
    .await?;

    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        let missing_amount_count: i64 = row.try_get("missing_amount_count")?;
        if missing_amount_count != 0 {
            bail!(
                "Merchant payment {} has SUCCEEDED operations without amount_minor",
                row.try_get::<String, _>("merchant_payment_id")?
            );
        }

        let currency: String = row.try_get("currency")?;
        validate_currency(&currency)?;
        let payment_state: String = row.try_get("payment_state")?;
        let normalized_status = normalize_internal_status(&payment_state)?;

        result.push(InternalSnapshotRow {
            source_row_id: format!(
                "internal:{}",
                row.try_get::<String, _>("merchant_payment_id")?
            ),
            merchant_payment_id: row.try_get("merchant_payment_id")?,
            provider_payment_id: row.try_get("provider_payment_id")?,
            currency,
            normalized_status: normalized_status.into(),
            authorized_amount_minor: row.try_get("authorized_amount_minor")?,
            captured_amount_minor: row.try_get("captured_amount_minor")?,
            refunded_amount_minor: row.try_get("refunded_amount_minor")?,
        });
    }

    tx.commit().await?;
    Ok(result)
}

async fn fetch_provider_row(base_url: &str, provider_payment_id: &str) -> Result<ProviderSnapshotRow> {
    validate_provider_id(provider_payment_id)?;
    let base_url = base_url.trim_end_matches('/');
    if !(base_url.starts_with("http://") || base_url.starts_with("https://")) {
        bail!("provider base URL must use http or https");
    }
    let url = format!("{base_url}/payments/{provider_payment_id}");

    let output = Command::new("curl")
        .args([
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "5",
            "--max-time",
            "10",
            &url,
        ])
        .output()
        .await
        .with_context(|| format!("launch curl for provider payment {provider_payment_id}"))?;

    if !output.status.success() {
        bail!(
            "provider GET failed for {}: {}",
            provider_payment_id,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let response: ProviderPaymentResponse = serde_json::from_slice(&output.stdout)
        .with_context(|| format!("decode provider payment {provider_payment_id}"))?;
    if response.id != provider_payment_id {
        bail!(
            "provider response identity mismatch: requested={} observed={}",
            provider_payment_id,
            response.id
        );
    }
    if response.amount <= 0
        || response.authorized_amount < 0
        || response.captured_amount < 0
        || response.refunded_amount < 0
        || response.version < 0
    {
        bail!("provider returned invalid negative/zero financial fields for {provider_payment_id}");
    }
    validate_currency(&response.currency)?;

    Ok(ProviderSnapshotRow {
        source_row_id: format!("provider:{provider_payment_id}"),
        provider_payment_id: provider_payment_id.to_owned(),
        currency: response.currency,
        normalized_status: normalize_provider_status(&response.status)?.into(),
        authorized_amount_minor: response.authorized_amount,
        captured_amount_minor: response.captured_amount,
        refunded_amount_minor: response.refunded_amount,
        provider_version: response.version,
    })
}

async fn persist_snapshot(pool: &PgPool, bundle: &SnapshotBundle, mutation_recipe: &str) -> Result<()> {
    let mut tx = pool.begin().await?;
    sqlx::query(
        r#"
        INSERT INTO payment_reconciliation.source_snapshot (
            snapshot_id,
            contract_version,
            business_date,
            provider_scope,
            scenario_case,
            base_snapshot_id,
            internal_snapshot_digest,
            provider_snapshot_digest,
            policy_version,
            mutation_recipe
        )
        VALUES ($1, $2, $3::date, $4, $5, $6, $7, $8, $9, $10::jsonb)
        "#,
    )
    .bind(&bundle.metadata.snapshot_id)
    .bind(CONTRACT_VERSION)
    .bind(&bundle.metadata.business_date)
    .bind(&bundle.metadata.provider_scope)
    .bind(&bundle.metadata.scenario_case)
    .bind(&bundle.metadata.base_snapshot_id)
    .bind(&bundle.metadata.internal_snapshot_digest)
    .bind(&bundle.metadata.provider_snapshot_digest)
    .bind(&bundle.metadata.policy_version)
    .bind(mutation_recipe)
    .execute(&mut *tx)
    .await?;

    for row in &bundle.internal {
        sqlx::query(
            r#"
            INSERT INTO payment_reconciliation.internal_payment_snapshot (
                snapshot_id,
                source_row_id,
                merchant_payment_id,
                provider_payment_id,
                currency,
                normalized_status,
                authorized_amount_minor,
                captured_amount_minor,
                refunded_amount_minor
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            "#,
        )
        .bind(&bundle.metadata.snapshot_id)
        .bind(&row.source_row_id)
        .bind(&row.merchant_payment_id)
        .bind(&row.provider_payment_id)
        .bind(&row.currency)
        .bind(&row.normalized_status)
        .bind(row.authorized_amount_minor)
        .bind(row.captured_amount_minor)
        .bind(row.refunded_amount_minor)
        .execute(&mut *tx)
        .await?;
    }

    for row in &bundle.provider {
        sqlx::query(
            r#"
            INSERT INTO payment_reconciliation.provider_payment_snapshot (
                snapshot_id,
                source_row_id,
                provider_payment_id,
                currency,
                normalized_status,
                authorized_amount_minor,
                captured_amount_minor,
                refunded_amount_minor,
                provider_version
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            "#,
        )
        .bind(&bundle.metadata.snapshot_id)
        .bind(&row.source_row_id)
        .bind(&row.provider_payment_id)
        .bind(&row.currency)
        .bind(&row.normalized_status)
        .bind(row.authorized_amount_minor)
        .bind(row.captured_amount_minor)
        .bind(row.refunded_amount_minor)
        .bind(row.provider_version)
        .execute(&mut *tx)
        .await?;
    }

    tx.commit().await?;
    Ok(())
}

async fn load_snapshot(pool: &PgPool, snapshot_id: &str) -> Result<SnapshotBundle> {
    let metadata_row = sqlx::query(
        r#"
        SELECT
            snapshot_id,
            business_date::text AS business_date,
            provider_scope,
            scenario_case,
            base_snapshot_id,
            internal_snapshot_digest,
            provider_snapshot_digest,
            policy_version
        FROM payment_reconciliation.source_snapshot
        WHERE snapshot_id = $1
        "#,
    )
    .bind(snapshot_id)
    .fetch_optional(pool)
    .await?
    .with_context(|| format!("snapshot not found: {snapshot_id}"))?;

    let metadata = SnapshotMetadata {
        snapshot_id: metadata_row.try_get("snapshot_id")?,
        business_date: metadata_row.try_get("business_date")?,
        provider_scope: metadata_row.try_get("provider_scope")?,
        scenario_case: metadata_row.try_get("scenario_case")?,
        base_snapshot_id: metadata_row.try_get("base_snapshot_id")?,
        internal_snapshot_digest: metadata_row.try_get("internal_snapshot_digest")?,
        provider_snapshot_digest: metadata_row.try_get("provider_snapshot_digest")?,
        policy_version: metadata_row.try_get("policy_version")?,
    };

    let internal_rows = sqlx::query(
        r#"
        SELECT
            source_row_id,
            merchant_payment_id,
            provider_payment_id,
            currency,
            normalized_status,
            authorized_amount_minor,
            captured_amount_minor,
            refunded_amount_minor
        FROM payment_reconciliation.internal_payment_snapshot
        WHERE snapshot_id = $1
        ORDER BY source_row_id
        "#,
    )
    .bind(snapshot_id)
    .fetch_all(pool)
    .await?;
    let internal = internal_rows
        .into_iter()
        .map(|row| {
            Ok(InternalSnapshotRow {
                source_row_id: row.try_get("source_row_id")?,
                merchant_payment_id: row.try_get("merchant_payment_id")?,
                provider_payment_id: row.try_get("provider_payment_id")?,
                currency: row.try_get("currency")?,
                normalized_status: row.try_get("normalized_status")?,
                authorized_amount_minor: row.try_get("authorized_amount_minor")?,
                captured_amount_minor: row.try_get("captured_amount_minor")?,
                refunded_amount_minor: row.try_get("refunded_amount_minor")?,
            })
        })
        .collect::<std::result::Result<Vec<_>, sqlx::Error>>()?;

    let provider_rows = sqlx::query(
        r#"
        SELECT
            source_row_id,
            provider_payment_id,
            currency,
            normalized_status,
            authorized_amount_minor,
            captured_amount_minor,
            refunded_amount_minor,
            provider_version
        FROM payment_reconciliation.provider_payment_snapshot
        WHERE snapshot_id = $1
        ORDER BY source_row_id
        "#,
    )
    .bind(snapshot_id)
    .fetch_all(pool)
    .await?;
    let provider = provider_rows
        .into_iter()
        .map(|row| {
            Ok(ProviderSnapshotRow {
                source_row_id: row.try_get("source_row_id")?,
                provider_payment_id: row.try_get("provider_payment_id")?,
                currency: row.try_get("currency")?,
                normalized_status: row.try_get("normalized_status")?,
                authorized_amount_minor: row.try_get("authorized_amount_minor")?,
                captured_amount_minor: row.try_get("captured_amount_minor")?,
                refunded_amount_minor: row.try_get("refunded_amount_minor")?,
                provider_version: row.try_get("provider_version")?,
            })
        })
        .collect::<std::result::Result<Vec<_>, sqlx::Error>>()?;

    Ok(SnapshotBundle {
        metadata,
        internal,
        provider,
    })
}

fn validate_bundle_integrity(bundle: &SnapshotBundle) -> Result<()> {
    let internal = internal_digest(&bundle.internal)?;
    let provider = provider_digest(&bundle.provider)?;
    if internal != bundle.metadata.internal_snapshot_digest {
        bail!(
            "internal snapshot digest mismatch: recorded={} recomputed={}",
            bundle.metadata.internal_snapshot_digest,
            internal
        );
    }
    if provider != bundle.metadata.provider_snapshot_digest {
        bail!(
            "provider snapshot digest mismatch: recorded={} recomputed={}",
            bundle.metadata.provider_snapshot_digest,
            provider
        );
    }
    Ok(())
}

fn validate_scenario_expectation(
    scenario_case: &str,
    oracle: &crate::oracle::OracleSummary,
) -> Result<()> {
    if scenario_case == "CLEAR" {
        if oracle.business_state != "CLEAR" {
            bail!("CLEAR scenario does not produce a CLEAR independent oracle result");
        }
        return Ok(());
    }

    let expected = expected_classification(scenario_case)?;
    if oracle.business_state != "HELD" || !contains_classification(oracle, expected) {
        bail!("scenario {scenario_case} does not produce its expected HELD classification");
    }
    Ok(())
}

fn expected_classification(scenario_case: &str) -> Result<Classification> {
    match scenario_case {
        "MISSING_PROVIDER" => Ok(Classification::MissingProvider),
        "MISSING_INTERNAL" => Ok(Classification::MissingInternal),
        "DUPLICATE_PROVIDER" => Ok(Classification::DuplicateProvider),
        "AMOUNT_MISMATCH" => Ok(Classification::AmountMismatch),
        "DUPLICATE_INTERNAL" => Ok(Classification::DuplicateInternal),
        "ATTRIBUTE_MISMATCH" => Ok(Classification::AttributeMismatch),
        other => bail!("unsupported scenario case for oracle validation: {other}"),
    }
}

fn validate_snapshot_identity(snapshot_id: &str) -> Result<()> {
    if snapshot_id.trim().is_empty()
        || !snapshot_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        bail!("snapshot id must contain only ASCII alphanumeric, '-', '_' or '.'");
    }
    Ok(())
}

fn validate_provider_id(provider_payment_id: &str) -> Result<()> {
    if provider_payment_id.is_empty()
        || !provider_payment_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        bail!("provider payment id is not a safe path segment: {provider_payment_id}");
    }
    Ok(())
}

fn validate_business_date(value: &str) -> Result<()> {
    let bytes = value.as_bytes();
    if bytes.len() != 10
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || !bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 4 | 7) || byte.is_ascii_digit())
    {
        bail!("business date must use YYYY-MM-DD");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_reject_path_separators() {
        assert!(validate_snapshot_identity("snapshot-1").is_ok());
        assert!(validate_snapshot_identity("../snapshot").is_err());
        assert!(validate_provider_id("provider_1").is_ok());
        assert!(validate_provider_id("provider/1").is_err());
    }

    #[test]
    fn business_date_shape_is_fail_closed() {
        assert!(validate_business_date("2026-11-14").is_ok());
        assert!(validate_business_date("2026-1-14").is_err());
        assert!(validate_business_date("2026/11/14").is_err());
    }
}
