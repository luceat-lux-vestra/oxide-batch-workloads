use std::num::NonZeroU64;
use std::sync::Arc;

use oxide_batch::item_components::{
    postgres_paging_reader, KeysetColumn, PostgresPagingFormat, PostgresRow,
};
use oxide_batch::{
    BatchStatus, Checkpoint, ChunkCommitReceipt, ChunkComponentRevisions, ChunkCounts,
    ChunkDeliveryMode, ChunkJob, ChunkRestartContract, ChunkSize, ChunkStep, ComponentRevision,
    ComponentStreamIdentity, DefinitionRevision, ExecutionContext, ExecutionCounts, InFlightPolicy,
    JobLauncher, JobName, JobParameter, JobParameters, NoopChunkCompletion, ParameterName,
    ParameterRole, ParameterValue, PostgresChunkStateError, PostgresChunkStateProvider,
    PostgresChunkTransactionManager, PostgresConfig, PostgresJobRepository, PostgresMigrator,
    ReaderError, SequentialIdGenerator, StateLimits, StateSchemaId, StateSchemaVersion, StepName,
    StopSource, SystemClock, TlsMode,
};
use serde::Serialize;
use sqlx::Row;

use crate::processor::{RecoveryProcessor, RecoverySourceRow};
use crate::writer::RecoveryWriter;
use crate::{CONTRACT_VERSION, OXIDE_BATCH_VERSION};

const JOB_NAME: &str = "payment-recovery-postgres-v1";
const DEFINITION_REVISION: &str = "payment-recovery-postgres-paging-v1";
const READER_NAMESPACE: &str = "oxide-batch-workloads.payment-recovery-postgres.paging-reader";
const CHECKPOINT_SCHEMA: &str = "oxide-batch-workloads.payment-recovery-postgres.checkpoint";
const CONTEXT_SCHEMA: &str = "oxide-batch-workloads.payment-recovery-postgres.execution-context";

pub const DEFAULT_CHUNK_SIZE: u32 = 25;
pub const DEFAULT_PAGE_SIZE: usize = 100;

#[derive(Debug, Serialize)]
pub struct RunReport {
    pub run_id: String,
    pub processing_generation: i64,
    pub launched: bool,
    pub retry_later_count: i64,
}

struct RunMetadata {
    run_id: String,
    candidate_set_digest: String,
    policy_version: String,
    processing_generation: i64,
}

pub struct RunRequest<'a> {
    pub database_url: &'a str,
    pub merchant_base_url: &'a str,
    pub provider_base_url: &'a str,
    pub run_id: &'a str,
    pub chunk_size: u32,
    pub page_size: usize,
}

fn pg_config(database_url: &str) -> anyhow::Result<PostgresConfig> {
    Ok(PostgresConfig::new(database_url)?.with_tls_mode(TlsMode::Plaintext))
}

pub async fn migrate(database_url: &str) -> anyhow::Result<()> {
    PostgresMigrator::migrate(&pg_config(database_url)?).await?;
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;
    sqlx::migrate!("./migrations").run(&pool).await?;
    pool.close().await;
    Ok(())
}

fn state_provider() -> Arc<dyn PostgresChunkStateProvider> {
    Arc::new(|committed: ExecutionCounts, chunk: ChunkCounts| {
        let position = committed
            .read()
            .checked_add(chunk.read().get())
            .ok_or_else(PostgresChunkStateError::new)?;
        let checkpoint_bytes = format!(
            r#"{{"format":"oxide-batch.checkpoint","format_version":1,"schema":"{CHECKPOINT_SCHEMA}","schema_version":1,"payload":{{"position":{position}}}}}"#
        );
        let checkpoint = Checkpoint::from_json(checkpoint_bytes.as_bytes(), StateLimits::default())
            .map_err(|_| PostgresChunkStateError::new())?;
        let context_bytes = format!(
            r#"{{"format":"oxide-batch.execution-context","format_version":1,"schema":"{CONTEXT_SCHEMA}","schema_version":1,"payload":{{}}}}"#
        );
        let context = ExecutionContext::from_json(context_bytes.as_bytes(), StateLimits::default())
            .map_err(|_| PostgresChunkStateError::new())?;
        Ok(ChunkCommitReceipt::new(checkpoint, context))
    })
}

fn revisions(namespace: ComponentStreamIdentity) -> anyhow::Result<ChunkComponentRevisions> {
    let restart = ChunkRestartContract::new(
        StateSchemaId::new(CHECKPOINT_SCHEMA)?,
        StateSchemaVersion::new(1)?,
        StateSchemaId::new(CONTEXT_SCHEMA)?,
        StateSchemaVersion::new(1)?,
        ChunkDeliveryMode::AtomicSameResource,
    )
    .with_in_flight_policy(InFlightPolicy::RollbackChunk);

    Ok(ChunkComponentRevisions::new(
        ComponentRevision::new("payment-recovery.candidate-reader-v1")?,
        ComponentRevision::new("payment-recovery.external-replay-processor-v1")?,
        ComponentRevision::new("payment-recovery.audit-result-writer-v1")?,
        ComponentRevision::new("payment-recovery.checkpoint-v1")?,
        restart,
    )
    .with_stream_revision(
        namespace,
        ComponentRevision::new("payment-recovery.candidate-reader-stream-v1")?,
    ))
}

fn identifying(name: &str, value: &str) -> anyhow::Result<(ParameterName, JobParameter)> {
    Ok((
        ParameterName::new(name)?,
        JobParameter::new(ParameterValue::string(value)?, ParameterRole::Identifying),
    ))
}

fn parameters(metadata: &RunMetadata) -> anyhow::Result<JobParameters> {
    Ok(JobParameters::try_from_iter([
        identifying("run_id", &metadata.run_id)?,
        identifying("contract_version", CONTRACT_VERSION)?,
        identifying("oxide_batch_version", OXIDE_BATCH_VERSION)?,
        identifying("candidate_set_digest", &metadata.candidate_set_digest)?,
        identifying("policy_version", &metadata.policy_version)?,
        identifying(
            "processing_generation",
            &metadata.processing_generation.to_string(),
        )?,
    ])?)
}

fn validate_run_id(value: &str) -> anyhow::Result<()> {
    if value.trim().is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        anyhow::bail!("run id contains unsupported characters");
    }
    Ok(())
}

fn source_query(run_id: &str) -> anyhow::Result<String> {
    validate_run_id(run_id)?;
    Ok(format!(
        r#"
        SELECT
            c.candidate_id,
            c.merchant_id,
            c.merchant_order_id,
            c.merchant_payment_id,
            c.operation_id,
            c.operation_kind,
            c.payment_state,
            c.operation_state,
            c.amount_minor,
            c.currency,
            c.idempotency_key,
            c.idempotency_key_digest,
            c.payment_method_token,
            c.payment_method_token_digest,
            COALESCE(c.provider_payment_id, '') AS provider_payment_id,
            COALESCE(c.gateway_operation_id, '') AS gateway_operation_id,
            c.source_updated_at::text AS source_updated_at,
            (
                COALESCE(
                    (
                        SELECT MAX(a.attempt_no)::BIGINT
                        FROM payment_recovery_workload.payment_recovery_attempt a
                        WHERE a.run_id = c.run_id
                          AND a.candidate_id = c.candidate_id
                    ),
                    0::BIGINT
                ) + 1
            )::BIGINT AS attempt_no
        FROM payment_recovery_workload.payment_recovery_candidate c
        LEFT JOIN payment_recovery_workload.payment_recovery_result r
          ON r.run_id = c.run_id
         AND r.candidate_id = c.candidate_id
        WHERE c.run_id = '{run_id}'
          AND (r.classification IS NULL OR r.classification = 'RETRY_LATER')
        "#
    ))
}

fn map_source(row: &PostgresRow<'_>) -> Result<RecoverySourceRow, ReaderError> {
    Ok(RecoverySourceRow {
        candidate_id: row.text("candidate_id")?,
        merchant_id: row.text("merchant_id")?,
        merchant_order_id: row.text("merchant_order_id")?,
        merchant_payment_id: row.text("merchant_payment_id")?,
        operation_id: row.text("operation_id")?,
        operation_kind: row.text("operation_kind")?,
        payment_state: row.text("payment_state")?,
        operation_state: row.text("operation_state")?,
        amount_minor: row.i64("amount_minor")?,
        currency: row.text("currency")?,
        idempotency_key: row.text("idempotency_key")?,
        idempotency_key_digest: row.text("idempotency_key_digest")?,
        payment_method_token: row.text("payment_method_token")?,
        payment_method_token_digest: row.text("payment_method_token_digest")?,
        provider_payment_id: nonempty(row.text("provider_payment_id")?),
        gateway_operation_id: nonempty(row.text("gateway_operation_id")?),
        source_updated_at: row.text("source_updated_at")?,
        attempt_no: row.i64("attempt_no")?,
    })
}

fn nonempty(value: String) -> Option<String> {
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}

pub async fn run(request: RunRequest<'_>) -> anyhow::Result<RunReport> {
    let RunRequest {
        database_url,
        merchant_base_url,
        provider_base_url,
        run_id,
        chunk_size,
        page_size,
    } = request;

    validate_run_id(run_id)?;
    let (metadata, should_launch) = prepare_processing_cycle(database_url, run_id).await?;
    if !should_launch {
        return Ok(RunReport {
            run_id: metadata.run_id,
            processing_generation: metadata.processing_generation,
            launched: false,
            retry_later_count: retry_later_count(database_url, run_id).await?,
        });
    }

    let clock = Arc::new(SystemClock);
    let repository = PostgresJobRepository::connect(pg_config(database_url)?, clock).await?;
    let ids = SequentialIdGenerator::new(NonZeroU64::MIN);
    let launcher = JobLauncher::new(&repository, &SystemClock, &ids);

    let namespace = ComponentStreamIdentity::new(READER_NAMESPACE)?;
    let (reader, stream, contract) = postgres_paging_reader(
        pg_config(database_url)?,
        source_query(run_id)?,
        vec![KeysetColumn::text("candidate_id")],
        PostgresPagingFormat::new().with_page_size(page_size),
        map_source,
        namespace.clone(),
    )?;

    let processor = RecoveryProcessor::new(
        metadata.run_id.clone(),
        metadata.processing_generation,
        merchant_base_url,
        provider_base_url,
    )?;
    let transactions =
        PostgresChunkTransactionManager::new(repository.clone(), state_provider());
    let step = ChunkStep::new(
        StepName::new("recover_frozen_payment_candidates")?,
        ChunkSize::new(chunk_size)?,
        reader,
        processor,
        RecoveryWriter,
        Arc::new(transactions),
        Arc::new(NoopChunkCompletion),
    )
    .with_item_stream(namespace.clone(), stream, contract);

    let component_revisions = revisions(namespace)?;
    let mut chunk_job = ChunkJob::new(
        JobName::new(JOB_NAME)?,
        step,
        DefinitionRevision::new(DEFINITION_REVISION)?,
        &component_revisions,
    )?;
    let job_parameters = parameters(&metadata)?;
    let (_stop_source, stop_token) = StopSource::new();
    let report = launcher
        .launch_chunk(&mut chunk_job, &job_parameters, &stop_token)
        .await?;

    let status = report.launch().job_execution().metadata().status();
    repository.close().await?;
    if status != BatchStatus::Completed {
        anyhow::bail!("payment recovery execution ended with status {status}, not Completed");
    }

    mark_cycle_processed(
        database_url,
        &metadata.run_id,
        metadata.processing_generation,
    )
    .await?;

    Ok(RunReport {
        run_id: metadata.run_id.clone(),
        processing_generation: metadata.processing_generation,
        launched: true,
        retry_later_count: retry_later_count(database_url, &metadata.run_id).await?,
    })
}

async fn prepare_processing_cycle(
    database_url: &str,
    run_id: &str,
) -> anyhow::Result<(RunMetadata, bool)> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;
    let mut tx = pool.begin().await?;

    let row = sqlx::query(
        "SELECT run_state, processing_generation, candidate_set_digest, policy_version
         FROM payment_recovery_workload.payment_recovery_run
         WHERE run_id = $1
         FOR UPDATE",
    )
    .bind(run_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| anyhow::anyhow!("payment recovery run not found: {run_id}"))?;

    let run_state: String = row.try_get("run_state")?;
    let current_generation: i64 = row.try_get("processing_generation")?;
    let candidate_set_digest: String = row.try_get("candidate_set_digest")?;
    let policy_version: String = row.try_get("policy_version")?;

    let retry_later: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT
         FROM payment_recovery_workload.payment_recovery_result
         WHERE run_id = $1 AND classification = 'RETRY_LATER'",
    )
    .bind(run_id)
    .fetch_one(&mut *tx)
    .await?;

    let (processing_generation, should_launch) = match run_state.as_str() {
        "SNAPSHOTTED" => (
            current_generation
                .checked_add(1)
                .ok_or_else(|| anyhow::anyhow!("processing generation overflow"))?,
            true,
        ),
        "PROCESSED" if retry_later > 0 => (
            current_generation
                .checked_add(1)
                .ok_or_else(|| anyhow::anyhow!("processing generation overflow"))?,
            true,
        ),
        "PROCESSED" | "VERIFIED" => (current_generation, false),
        "PROCESSING" | "RECOVERING" => {
            anyhow::bail!(
                "payment recovery run is {run_state}; checkpoint 6 recovery is required before another processing cycle"
            )
        }
        other => anyhow::bail!("unsupported payment recovery run state: {other}"),
    };

    if should_launch {
        let updated = sqlx::query(
            "UPDATE payment_recovery_workload.payment_recovery_run
             SET run_state = 'PROCESSING',
                 processing_generation = $2,
                 updated_at = NOW()
             WHERE run_id = $1",
        )
        .bind(run_id)
        .bind(processing_generation)
        .execute(&mut *tx)
        .await?;
        if updated.rows_affected() != 1 {
            anyhow::bail!("processing cycle activation did not update exactly one run");
        }
    }

    tx.commit().await?;
    pool.close().await;

    Ok((
        RunMetadata {
            run_id: run_id.to_owned(),
            candidate_set_digest,
            policy_version,
            processing_generation,
        },
        should_launch,
    ))
}

async fn mark_cycle_processed(
    database_url: &str,
    run_id: &str,
    processing_generation: i64,
) -> anyhow::Result<()> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;
    let updated = sqlx::query(
        "UPDATE payment_recovery_workload.payment_recovery_run
         SET run_state = 'PROCESSED', updated_at = NOW()
         WHERE run_id = $1
           AND run_state = 'PROCESSING'
           AND processing_generation = $2",
    )
    .bind(run_id)
    .bind(processing_generation)
    .execute(&pool)
    .await?;
    pool.close().await;
    if updated.rows_affected() != 1 {
        anyhow::bail!("processing cycle completion did not update exactly one active run");
    }
    Ok(())
}

async fn retry_later_count(database_url: &str, run_id: &str) -> anyhow::Result<i64> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(database_url)
        .await?;
    let count = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT
         FROM payment_recovery_workload.payment_recovery_result
         WHERE run_id = $1 AND classification = 'RETRY_LATER'",
    )
    .bind(run_id)
    .fetch_one(&pool)
    .await?;
    pool.close().await;
    Ok(count)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_query_is_bound_to_safe_run_identity_and_unresolved_results() {
        let sql = source_query("run-1").unwrap();
        assert!(sql.contains("c.run_id = 'run-1'"));
        assert!(sql.contains("r.classification = 'RETRY_LATER'"));
        assert!(sql.contains("MAX(a.attempt_no)"));
        assert!(source_query("run' OR TRUE --").is_err());
    }

    #[test]
    fn processing_generation_is_identifying() {
        let metadata = RunMetadata {
            run_id: "run-1".into(),
            candidate_set_digest: "a".repeat(64),
            policy_version: "policy-v1".into(),
            processing_generation: 2,
        };
        let params = parameters(&metadata).unwrap();
        let key = oxide_batch::JobInstanceKey::new(JobName::new(JOB_NAME).unwrap(), &params);
        let metadata2 = RunMetadata {
            processing_generation: 3,
            ..metadata
        };
        let params2 = parameters(&metadata2).unwrap();
        let key2 = oxide_batch::JobInstanceKey::new(JobName::new(JOB_NAME).unwrap(), &params2);
        assert!(key != key2);
    }
}
