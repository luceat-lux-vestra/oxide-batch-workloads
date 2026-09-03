//! Wires the cursor-mode transform job against the real production launch
//! path (`oxide_batch::JobLauncher::launch_chunk`) and against OxideBatch's
//! own PostgreSQL metadata migrator plus this workload's own source/business
//! table migration. Every restart-relevant behavior (checkpointing,
//! same-resource transaction enlistment, instance/resume matching) is the
//! framework's, not ours -- this module only configures it.

use std::sync::Arc;

use oxide_batch::item_components::{
    postgres_cursor_reader, KeysetColumn, PostgresCursorFormat, PostgresRow,
};
use oxide_batch::{
    Checkpoint, ChunkCommitReceipt, ChunkComponentRevisions, ChunkCounts, ChunkDeliveryMode,
    ChunkJob, ChunkRestartContract, ChunkSize, ChunkStep, ComponentRevision,
    ComponentStreamIdentity, DefinitionRevision, ExecutionContext, ExecutionCounts, InFlightPolicy,
    JobLauncher, JobName, JobParameter, JobParameters, NoopChunkCompletion, ParameterName,
    ParameterRole, ParameterValue, PostgresChunkStateError, PostgresChunkStateProvider,
    PostgresChunkTransactionManager, PostgresConfig, PostgresJobRepository, PostgresMigrator,
    ReaderError, SequentialIdGenerator, StateLimits, StateSchemaId, StateSchemaVersion, StepName,
    StopSource, SystemClock, TlsMode,
};

use crate::processor::{CustomerProjector, SourceRow};

const READER_NAMESPACE: &str = "oxide-batch-workload.postgres-postgres.cursor-reader";
const CHECKPOINT_SCHEMA: &str = "oxide-batch-workload.postgres-postgres.checkpoint";
const CONTEXT_SCHEMA: &str = "oxide-batch-workload.postgres-postgres.execution-context";
const WORKLOAD_MIGRATION: &str = include_str!("../migrations/001_init.sql");

pub const DEFAULT_FETCH_SIZE: usize = 200;

fn pg_config(database_url: &str) -> anyhow::Result<PostgresConfig> {
    Ok(PostgresConfig::new(database_url)?.with_tls_mode(TlsMode::Plaintext))
}

/// Runs OxideBatch's own metadata migrations (its `oxide_batch` schema)
/// plus this workload's `app_source`/`app_business` migration. The two
/// never share a schema, and neither migrator touches the other's.
pub async fn migrate(database_url: &str) -> anyhow::Result<()> {
    PostgresMigrator::migrate(&pg_config(database_url)?).await?;

    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(database_url)
        .await?;
    sqlx::raw_sql(WORKLOAD_MIGRATION).execute(&pool).await?;
    pool.close().await;
    Ok(())
}

/// Truncates only the workload-owned tables (never drops them, never
/// touches `oxide_batch`).
pub async fn reset(database_url: &str) -> anyhow::Result<()> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(database_url)
        .await?;
    sqlx::raw_sql(
        "TRUNCATE TABLE app_business.customer_projection; \
         TRUNCATE TABLE app_source.source_customer",
    )
    .execute(&pool)
    .await?;
    pool.close().await;
    Ok(())
}

/// The chunk-level checkpoint/execution-context payload
/// `PostgresChunkTransactionManager` commits alongside business rows and
/// counters -- a generic "how many items committed so far" position,
/// separate from (and complementary to) the cursor reader's own keyset
/// `ItemStream` state registered below.
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

fn component_revisions(
    namespace: ComponentStreamIdentity,
) -> anyhow::Result<ChunkComponentRevisions> {
    // Primary same-resource atomic delivery mode (spec): destination
    // business writes and the framework checkpoint/component state share
    // one transaction boundary. `PostgresBatchWriter` never opens or
    // commits its own transaction (see src/writer.rs), so this is the only
    // delivery mode compatible with this job's writer at all -- there is
    // no cross-resource fallback path being bypassed here.
    let restart = ChunkRestartContract::new(
        StateSchemaId::new(CHECKPOINT_SCHEMA)?,
        StateSchemaVersion::new(1)?,
        StateSchemaId::new(CONTEXT_SCHEMA)?,
        StateSchemaVersion::new(1)?,
        ChunkDeliveryMode::AtomicSameResource,
    )
    .with_in_flight_policy(InFlightPolicy::RollbackChunk);
    Ok(ChunkComponentRevisions::new(
        ComponentRevision::new("postgres-postgres.cursor-reader-v1")?,
        ComponentRevision::new("postgres-postgres.customer-projector-v1")?,
        ComponentRevision::new("postgres-postgres.postgres-batch-writer-v1")?,
        ComponentRevision::new("postgres-postgres.checkpoint-v1")?,
        restart,
    )
    .with_stream_revision(
        namespace,
        ComponentRevision::new("postgres-postgres.cursor-reader-stream-v1")?,
    ))
}

fn map_source_row(row: &PostgresRow<'_>) -> Result<SourceRow, ReaderError> {
    Ok(SourceRow {
        customer_id: row.i64("customer_id")?,
        full_name: row.text("full_name")?,
        is_active: row.bool("is_active")?,
        balance_cents: row.i64("balance_cents")?,
    })
}

/// Launches (or, against an existing resumable instance, resumes) the
/// cursor-mode transform job through the real production launch path.
///
/// Job identity includes both the user-facing `import_name` and the
/// streaming source content digest (`source_digest::compute`) as
/// *identifying* parameters: the same source content under the same import
/// name resumes the same instance, while a changed source resolves to a
/// distinct `JobInstanceKey` and therefore can never silently resume a
/// stale checkpoint against different content (see
/// `tests/source_identity.rs`).
pub async fn run(
    database_url: &str,
    import_name: &str,
    chunk_size: u32,
    fetch_size: usize,
) -> anyhow::Result<()> {
    let clock = Arc::new(SystemClock);
    let repository = PostgresJobRepository::connect(pg_config(database_url)?, clock).await?;
    let ids = SequentialIdGenerator::new(std::num::NonZeroU64::MIN);
    let launcher = JobLauncher::new(&repository, &SystemClock, &ids);

    let source_pool = sqlx::postgres::PgPoolOptions::new()
        .connect(database_url)
        .await?;
    let source_digest = crate::source_digest::compute(&source_pool).await?;
    source_pool.close().await;
    tracing::info!(
        import_name,
        source_digest,
        chunk_size,
        fetch_size,
        "starting run"
    );

    let namespace = ComponentStreamIdentity::new(READER_NAMESPACE)?;
    let format = PostgresCursorFormat::new().with_fetch_size(fetch_size);
    let (reader, stream, contract) = postgres_cursor_reader(
        pg_config(database_url)?,
        "SELECT customer_id, full_name, is_active, balance_cents FROM app_source.source_customer",
        vec![KeysetColumn::i64("customer_id")],
        format,
        map_source_row,
        namespace.clone(),
    )?;

    let writer = crate::writer::writer()?;
    let processor = CustomerProjector {
        import_name: import_name.to_owned(),
        source_digest: source_digest.clone(),
    };

    let raw_transactions =
        PostgresChunkTransactionManager::new(repository.clone(), state_provider());

    let step = ChunkStep::new(
        StepName::new("transform_customers")?,
        ChunkSize::new(chunk_size)?,
        reader,
        processor,
        writer,
        Arc::new(raw_transactions),
        Arc::new(NoopChunkCompletion),
    )
    .with_item_stream(namespace.clone(), stream, contract);

    let mut chunk_job = ChunkJob::new(
        JobName::new(import_name)?,
        step,
        DefinitionRevision::new("postgres-postgres-transform-v1")?,
        &component_revisions(namespace)?,
    )?;

    let parameters = JobParameters::try_from_iter([
        (
            ParameterName::new("import_name")?,
            JobParameter::new(
                ParameterValue::string(import_name)?,
                ParameterRole::Identifying,
            ),
        ),
        (
            ParameterName::new("source_digest")?,
            JobParameter::new(
                ParameterValue::string(&source_digest)?,
                ParameterRole::Identifying,
            ),
        ),
    ])?;
    let (_stop_source, stop_token) = StopSource::new();

    let report = launcher
        .launch_chunk(&mut chunk_job, &parameters, &stop_token)
        .await?;
    let execution = report.launch().job_execution();
    tracing::info!(
        job_execution_id = %execution.id(),
        status = %execution.metadata().status(),
        "run finished"
    );
    if let Some(chunk_report) = report.chunk() {
        tracing::info!(
            committed_read = chunk_report.committed_counts().read().get(),
            committed_written = chunk_report.committed_counts().written().get(),
            "chunk evidence"
        );
    }
    let status = execution.metadata().status();
    repository.close().await?;
    // launch_chunk returning Ok only means the launcher's own future
    // completed; a component failure is persisted, not surfaced as Err
    // (see JobLauncher::launch's doc comment). A caller relying on process
    // exit code (a shell script, CI, this workload's own tests) needs a
    // nonzero exit for a non-successful terminal job status.
    if status != oxide_batch::BatchStatus::Completed {
        anyhow::bail!("job execution ended with status {status}, not Completed");
    }
    Ok(())
}
