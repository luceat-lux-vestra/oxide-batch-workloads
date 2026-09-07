use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::str::FromStr;
use std::sync::Arc;

use anyhow::{bail, Result};
use clap::{Parser, Subcommand};
use futures_util::TryStreamExt;
use oxide_batch::{
    BatchStatus, FailureCategory, FailureId, JobInstanceKey, JobName, JobParameters, JobRepository,
    PostgresConfig, PostgresJobRepository, RecoveryRequest, SystemClock, TlsMode,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions, PgSslMode};
use sqlx::{PgPool, Row};

const JOB_PREFIX: &str = "postgres-local-partition";
const APP_APPLICATION_NAME: &str = "oxide-batch-workload-local-partition-recoveryctl";

#[derive(Parser)]
#[command(name = "postgres-local-partition-recoveryctl")]
#[command(about = "Qualification-only recovery/inspection tool for campaign #93")]
struct Cli {
    #[arg(long, env = "DATABASE_URL")]
    database_url: String,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Inspect {
        #[arg(long)]
        run_name: String,
    },
    Recover {
        #[arg(long)]
        run_name: String,
        #[arg(long)]
        rows: u64,
        #[arg(long)]
        partitions: u16,
    },
    MutateSource {
        #[arg(long)]
        source_id: i64,
        #[arg(long, allow_hyphen_values = true)]
        delta: i64,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct PartitionPayload {
    partition_index: u16,
    range_start: u64,
    range_end: u64,
    source_digest: String,
}

#[derive(Debug, Deserialize)]
struct ContextEnvelope {
    payload: PartitionPayload,
}

#[derive(Debug, Serialize)]
struct ExecutionObservation {
    id: String,
    status: String,
}

#[derive(Debug, Serialize)]
struct PartitionObservation {
    key: String,
    status: String,
    worker_step_execution_id: Option<String>,
    partition_index: u16,
    range_start: u64,
    range_end: u64,
    recorded_source_digest: String,
    business_rows: u64,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Inspect { run_name } => inspect(&cli.database_url, &run_name).await?,
        Command::Recover {
            run_name,
            rows,
            partitions,
        } => recover(&cli.database_url, &run_name, rows, partitions).await?,
        Command::MutateSource { source_id, delta } => {
            mutate_source(&cli.database_url, source_id, delta).await?
        }
    }
    Ok(())
}

async fn inspect(url: &str, run_name: &str) -> Result<()> {
    let repository =
        PostgresJobRepository::connect(framework_config(url, 2)?, Arc::new(SystemClock)).await?;
    let key = JobInstanceKey::new(job_name(run_name)?, &JobParameters::new());
    let mut unit = repository.begin().await?;
    let instance = unit
        .find_job_instance(&key)
        .await?
        .ok_or_else(|| anyhow::anyhow!("durable job instance is missing"))?;
    let executions = unit.job_executions(instance.id()).await?;
    let latest = executions
        .last()
        .ok_or_else(|| anyhow::anyhow!("durable job execution is missing"))?;
    let nonterminal_execution_count = executions
        .iter()
        .filter(|execution| {
            matches!(
                execution.metadata().status(),
                BatchStatus::Starting
                    | BatchStatus::Started
                    | BatchStatus::Stopping
                    | BatchStatus::Unknown
            )
        })
        .count();
    let steps = unit.step_executions(latest.id()).await?;
    let parent = steps
        .iter()
        .find(|step| step.step_name().as_str() == "partitioned")
        .ok_or_else(|| anyhow::anyhow!("partition manager step is missing"))?;
    let mut durable = unit.step_partition_plan(parent.id()).await?;
    let latest_execution_id = latest.id().to_string();
    let latest_execution_status = latest.metadata().status().to_string();
    let latest_parent_status = parent.metadata().status().to_string();
    let execution_observations = executions
        .iter()
        .map(|execution| ExecutionObservation {
            id: execution.id().to_string(),
            status: execution.metadata().status().to_string(),
        })
        .collect::<Vec<_>>();
    unit.rollback().await?;
    repository.close().await?;

    durable.sort_by(|left, right| left.key().as_str().cmp(right.key().as_str()));
    let pool = app_pool(url, 2).await?;
    let (source_rows, source_digest) = source_identity(&pool).await?;
    let business_counts = business_counts(&pool, run_name).await?;
    let mut partitions = Vec::with_capacity(durable.len());
    for partition in durable {
        let context = partition.context().to_json()?;
        let envelope: ContextEnvelope = serde_json::from_slice(&context)?;
        let key = partition.key().as_str().to_owned();
        partitions.push(PartitionObservation {
            business_rows: business_counts.get(&key).copied().unwrap_or(0),
            key,
            status: partition.status().to_string(),
            worker_step_execution_id: partition
                .worker_step_execution_id()
                .map(|id| id.to_string()),
            partition_index: envelope.payload.partition_index,
            range_start: envelope.payload.range_start,
            range_end: envelope.payload.range_end,
            recorded_source_digest: envelope.payload.source_digest,
        });
    }
    pool.close().await;

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "run_name": run_name,
            "source_rows": source_rows,
            "live_source_digest": source_digest,
            "executions": execution_observations,
            "latest_execution_id": latest_execution_id,
            "latest_execution_status": latest_execution_status,
            "latest_parent_status": latest_parent_status,
            "nonterminal_execution_count": nonterminal_execution_count,
            "partitions": partitions,
        }))?
    );
    Ok(())
}

async fn recover(url: &str, run_name: &str, rows: u64, partitions: u16) -> Result<()> {
    validate_shape(rows, partitions)?;
    let identity_pool = app_pool(url, 1).await?;
    let (source_rows, source_digest) = source_identity(&identity_pool).await?;
    identity_pool.close().await;
    if source_rows != rows {
        bail!("source contains {source_rows} rows, expected {rows}");
    }

    let repository =
        PostgresJobRepository::connect(framework_config(url, 2)?, Arc::new(SystemClock)).await?;
    let key = JobInstanceKey::new(job_name(run_name)?, &JobParameters::new());
    let mut inspect = repository.begin().await?;
    let instance = inspect
        .find_job_instance(&key)
        .await?
        .ok_or_else(|| anyhow::anyhow!("durable job instance is missing"))?;
    let execution = inspect
        .job_executions(instance.id())
        .await?
        .into_iter()
        .last()
        .ok_or_else(|| anyhow::anyhow!("durable job execution is missing"))?;
    let steps = inspect.step_executions(execution.id()).await?;
    let parent = steps
        .iter()
        .find(|step| step.step_name().as_str() == "partitioned")
        .ok_or_else(|| anyhow::anyhow!("partition manager step is missing"))?;
    let mut durable = inspect.step_partition_plan(parent.id()).await?;
    inspect.rollback().await?;

    if !matches!(
        execution.metadata().status(),
        BatchStatus::Starting | BatchStatus::Started | BatchStatus::Stopping | BatchStatus::Unknown
    ) {
        bail!(
            "latest execution status is {}, which does not require crash recovery",
            execution.metadata().status()
        );
    }

    durable.sort_by(|left, right| left.key().as_str().cmp(right.key().as_str()));
    if durable.len() != usize::from(partitions) {
        bail!(
            "durable partition count is {}, expected {partitions}",
            durable.len()
        );
    }
    for partition in &durable {
        let context = partition.context().to_json()?;
        let envelope: ContextEnvelope = serde_json::from_slice(&context)?;
        let payload = envelope.payload;
        let (expected_key, expected_start, expected_end) =
            expected_assignment(rows, partitions, payload.partition_index)?;
        if partition.key().as_str() != expected_key
            || payload.range_start != expected_start
            || payload.range_end != expected_end
            || payload.source_digest != source_digest
        {
            bail!(
                "durable partition source identity/assignment mismatch for {}: recovery refused",
                partition.key()
            );
        }
    }

    let mut hasher = Sha256::new();
    hasher.update(format!("campaign93-local-partition-recovery:{run_name}").as_bytes());
    let evidence_digest: [u8; 32] = hasher.finalize().into();
    let request = RecoveryRequest::mark_failed(
        execution.version(),
        "CAMPAIGN93_EXTERNAL_SIGKILL_INSPECTED",
        "postgres-local-partition-recoveryctl",
        evidence_digest,
        FailureCategory::PermanentInfrastructure,
        FailureId::new(93)?,
    )?;
    let mut recovery = repository.begin().await?;
    let recovered = recovery
        .recover_job_execution(execution.id(), &request)
        .await?;
    let resulting_status = recovered.decision().resulting_status().to_string();
    recovery.commit().await?;
    repository.close().await?;

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "run_name": run_name,
            "recovered_execution_id": execution.id().to_string(),
            "resulting_status": resulting_status,
            "live_source_digest": source_digest,
        }))?
    );
    Ok(())
}

async fn mutate_source(url: &str, source_id: i64, delta: i64) -> Result<()> {
    if delta == 0 {
        bail!("delta must be nonzero");
    }
    let pool = app_pool(url, 1).await?;
    let changed = sqlx::query(
        "UPDATE app_source.local_partition_source \
         SET payload_value = payload_value + $2 WHERE source_id = $1",
    )
    .bind(source_id)
    .bind(delta)
    .execute(&pool)
    .await?;
    pool.close().await;
    if changed.rows_affected() != 1 {
        bail!(
            "source mutation expected exactly one row, changed {}",
            changed.rows_affected()
        );
    }
    Ok(())
}

fn expected_assignment(rows: u64, partitions: u16, index: u16) -> Result<(String, u64, u64)> {
    let rows_per_partition = validate_shape(rows, partitions)?;
    if index >= partitions {
        bail!("partition index {index} exceeds configured partition count {partitions}");
    }
    let start = u64::from(index)
        .checked_mul(rows_per_partition)
        .and_then(|value| value.checked_add(1))
        .ok_or_else(|| anyhow::anyhow!("partition range start overflowed"))?;
    let end = start
        .checked_add(rows_per_partition - 1)
        .ok_or_else(|| anyhow::anyhow!("partition range end overflowed"))?;
    Ok((format!("partition-{index:04}"), start, end))
}

fn validate_shape(rows: u64, partitions: u16) -> Result<u64> {
    if rows == 0 || partitions == 0 {
        bail!("rows and partitions must be nonzero");
    }
    if !rows.is_multiple_of(u64::from(partitions)) {
        bail!("rows must be exactly divisible by partitions");
    }
    let rows_per_partition = rows / u64::from(partitions);
    if rows_per_partition == 0 {
        bail!("every partition must own at least one row");
    }
    Ok(rows_per_partition)
}

fn job_name(run_name: &str) -> Result<JobName> {
    Ok(JobName::new(format!("{JOB_PREFIX}-{run_name}"))?)
}

fn framework_config(url: &str, pool_size: u32) -> Result<PostgresConfig> {
    Ok(PostgresConfig::new(url.to_owned())?
        .with_tls_mode(TlsMode::Plaintext)
        .with_pool_size(pool_size)?)
}

async fn app_pool(url: &str, max_connections: u32) -> Result<PgPool> {
    let options = PgConnectOptions::from_str(url)?
        .application_name(APP_APPLICATION_NAME)
        .ssl_mode(PgSslMode::Disable);
    Ok(PgPoolOptions::new()
        .max_connections(max_connections)
        .connect_with(options)
        .await?)
}

async fn source_identity(pool: &PgPool) -> Result<(u64, String)> {
    let mut stream = sqlx::query(
        "SELECT source_id, payload_value FROM app_source.local_partition_source ORDER BY source_id",
    )
    .fetch(pool);
    let mut count = 0_u64;
    let mut hasher = Sha256::new();
    while let Some(row) = stream.try_next().await? {
        let source_id: i64 = row.try_get("source_id")?;
        let payload_value: i64 = row.try_get("payload_value")?;
        hasher.update(format!("{source_id}|{payload_value}\n").as_bytes());
        count += 1;
    }
    Ok((count, finish_digest(hasher)))
}

async fn business_counts(pool: &PgPool, run_name: &str) -> Result<BTreeMap<String, u64>> {
    let rows = sqlx::query(
        "SELECT partition_key, count(*)::bigint AS row_count \
         FROM app_business.local_partition_projection \
         WHERE run_name = $1 GROUP BY partition_key ORDER BY partition_key",
    )
    .bind(run_name)
    .fetch_all(pool)
    .await?;
    let mut counts = BTreeMap::new();
    for row in rows {
        let key: String = row.try_get("partition_key")?;
        let count: i64 = row.try_get("row_count")?;
        counts.insert(key, u64::try_from(count)?);
    }
    Ok(counts)
}

fn finish_digest(hasher: Sha256) -> String {
    let bytes = hasher.finalize();
    bytes
        .iter()
        .fold(String::with_capacity(64), |mut output, byte| {
            let _ = write!(&mut output, "{byte:02x}");
            output
        })
}
