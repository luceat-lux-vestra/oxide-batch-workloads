use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use std::str::FromStr;
use std::time::Duration;

use anyhow::{bail, Result};
use serde::Serialize;
use serde_json::json;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions, PgSslMode};
use sqlx::{PgPool, Row};

const OBSERVER_APPLICATION_NAME: &str = "oxide-batch-workload-local-partition-observer";
const FRAMEWORK_APPLICATION_NAME: &str = "oxide-batch";
const BUSINESS_APPLICATION_NAME: &str = "oxide-batch-workload-local-partition";

#[derive(Clone, Debug, Default, Serialize)]
struct SessionPeak {
    total: u64,
    active: u64,
    lock_wait: u64,
}

impl SessionPeak {
    fn observe(&mut self, total: u64, active: u64, lock_wait: u64) {
        self.total = self.total.max(total);
        self.active = self.active.max(active);
        self.lock_wait = self.lock_wait.max(lock_wait);
    }
}

pub async fn observe(
    url: &str,
    output: &Path,
    ready_file: &Path,
    stop_file: &Path,
    interval_ms: u64,
) -> Result<()> {
    if !(1..=1000).contains(&interval_ms) {
        bail!("interval-ms must be 1..=1000")
    }
    if output == ready_file || output == stop_file || ready_file == stop_file {
        bail!("output, ready-file, and stop-file must be distinct paths")
    }
    let pool = observer_pool(url).await?;
    let mut peaks: BTreeMap<String, SessionPeak> = BTreeMap::new();
    sample(&pool, &mut peaks).await?;
    let mut samples = 1_u64;
    let ready_parent = ready_file.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(ready_parent)?;
    fs::write(ready_file, b"ready\n")?;

    while !stop_file.exists() {
        tokio::time::sleep(Duration::from_millis(interval_ms)).await;
        sample(&pool, &mut peaks).await?;
        samples += 1;
    }
    pool.close().await;

    let report = json!({
        "sample_interval_ms": interval_ms,
        "samples": samples,
        "framework_application_name": FRAMEWORK_APPLICATION_NAME,
        "business_application_name": BUSINESS_APPLICATION_NAME,
        "peaks": peaks,
        "note": "peaks are sampled maxima at the configured interval, not exact instantaneous maxima; observer readiness is recorded only after its first successful sample; the observer application_name is excluded, but observation continues until the unchanged run process returns and may therefore include workload-owned post-launch verification activity",
    });
    let parent = output.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let temporary = output.with_extension("tmp");
    fs::write(&temporary, serde_json::to_vec_pretty(&report)?)?;
    fs::rename(temporary, output)?;
    Ok(())
}

async fn sample(pool: &PgPool, peaks: &mut BTreeMap<String, SessionPeak>) -> Result<()> {
    let rows = sqlx::query(
        "SELECT application_name, count(*)::bigint AS total, \
         count(*) FILTER (WHERE state = 'active')::bigint AS active, \
         count(*) FILTER (WHERE wait_event_type = 'Lock')::bigint AS lock_wait \
         FROM pg_stat_activity \
         WHERE datname = current_database() AND application_name IN ($1, $2) \
         GROUP BY application_name",
    )
    .bind(FRAMEWORK_APPLICATION_NAME)
    .bind(BUSINESS_APPLICATION_NAME)
    .fetch_all(pool)
    .await?;

    for row in rows {
        let application_name: String = row.try_get("application_name")?;
        let total = u64::try_from(row.try_get::<i64, _>("total")?)?;
        let active = u64::try_from(row.try_get::<i64, _>("active")?)?;
        let lock_wait = u64::try_from(row.try_get::<i64, _>("lock_wait")?)?;
        peaks
            .entry(application_name)
            .or_default()
            .observe(total, active, lock_wait);
    }
    for application_name in [FRAMEWORK_APPLICATION_NAME, BUSINESS_APPLICATION_NAME] {
        peaks.entry(application_name.to_owned()).or_default();
    }
    Ok(())
}

async fn observer_pool(url: &str) -> Result<PgPool> {
    let options = PgConnectOptions::from_str(url)?
        .application_name(OBSERVER_APPLICATION_NAME)
        .ssl_mode(PgSslMode::Disable);
    Ok(PgPoolOptions::new()
        .max_connections(1)
        .connect_with(options)
        .await?)
}
