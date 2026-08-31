//! Wires the CSV import job against the real production launch path
//! (`oxide_batch::JobLauncher::launch_chunk`) and against OxideBatch's own
//! PostgreSQL metadata migrator plus this workload's own business-table
//! migration. No re-implementation of the framework's own batch engine:
//! every restart-relevant behavior (checkpointing, transaction enlistment,
//! instance/resume matching) is the framework's, not ours.

use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU32};
use std::sync::Arc;

use oxide_batch::item_components::{DelimitedDialect, DelimitedRecord, delimited_file_reader};
use oxide_batch::{
    Checkpoint, ChunkCommitReceipt, ChunkComponentRevisions, ChunkCounts,
    ChunkDeliveryMode, ChunkJob, ChunkRestartContract, ChunkSize, ChunkStep, ComponentRevision,
    ComponentStreamIdentity, DefinitionRevision, ExecutionContext, ExecutionCounts, FailureCategory,
    FailureId, InFlightPolicy, JobLauncher, JobName, JobParameter, JobParameters, JobRepository,
    NoopChunkCompletion, ParameterName, ParameterRole, ParameterValue, PostgresChunkStateError,
    PostgresChunkStateProvider,
    PostgresChunkTransactionManager, PostgresConfig, PostgresJobRepository, PostgresMigrator,
    RecoveryRequest, SequentialIdGenerator, StateLimits, StateSchemaId, StateSchemaVersion, StepName,
    StopSource, SystemClock, TlsMode,
};
use sha2::{Digest, Sha256};

use crate::failpoint::{FailAt, FailingReader, FailingTransactionManager, FailingWriter, FailureMode};
use crate::processor::CustomerRowProcessor;
use crate::writer::CustomerRowWriter;

const READER_NAMESPACE: &str = "oxide-batch-workload.csv-postgres.delimited-reader";
const CHECKPOINT_SCHEMA: &str = "oxide-batch-workload.csv-postgres.checkpoint";
const CONTEXT_SCHEMA: &str = "oxide-batch-workload.csv-postgres.execution-context";
const BUSINESS_MIGRATION: &str = include_str!("../migrations/001_init.sql");

fn pg_config(database_url: &str) -> anyhow::Result<PostgresConfig> {
    Ok(PostgresConfig::new(database_url)?.with_tls_mode(TlsMode::Plaintext))
}

/// Runs OxideBatch's own metadata migrations (its `oxide_batch` schema)
/// plus this workload's business-table migration (`app_business` schema).
/// The two never share a schema, and neither migrator touches the other's.
pub async fn migrate(database_url: &str) -> anyhow::Result<()> {
    PostgresMigrator::migrate(&pg_config(database_url)?).await?;

    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(database_url)
        .await?;
    sqlx::raw_sql(BUSINESS_MIGRATION).execute(&pool).await?;
    pool.close().await;
    Ok(())
}

/// Drops and recreates only the business table. Never touches the
/// `oxide_batch` schema.
pub async fn reset(database_url: &str) -> anyhow::Result<()> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(database_url)
        .await?;
    sqlx::raw_sql("TRUNCATE TABLE app_business.imported_customer")
        .execute(&pool)
        .await?;
    pool.close().await;
    Ok(())
}

/// The chunk-level checkpoint/execution-context payload
/// `PostgresChunkTransactionManager` commits alongside business rows and
/// counters. This is a generic "how many items committed so far" position,
/// separate from (and complementary to) the CSV reader's own byte/line/
/// record `ItemStream` state registered below.
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

fn component_revisions(namespace: ComponentStreamIdentity) -> anyhow::Result<ChunkComponentRevisions> {
    let restart = ChunkRestartContract::new(
        StateSchemaId::new(CHECKPOINT_SCHEMA)?,
        StateSchemaVersion::new(1)?,
        StateSchemaId::new(CONTEXT_SCHEMA)?,
        StateSchemaVersion::new(1)?,
        ChunkDeliveryMode::AtomicSameResource,
    )
    .with_in_flight_policy(InFlightPolicy::RollbackChunk);
    Ok(ChunkComponentRevisions::new(
        ComponentRevision::new("csv-postgres.delimited-reader-v1")?,
        ComponentRevision::new("csv-postgres.customer-row-processor-v1")?,
        ComponentRevision::new("csv-postgres.postgres-batch-writer-v1")?,
        ComponentRevision::new("csv-postgres.checkpoint-v1")?,
        restart,
    )
    .with_stream_revision(namespace, ComponentRevision::new("csv-postgres.delimited-reader-stream-v1")?))
}

fn sha256_hex(bytes: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher.finalize().into()
}

/// Launches (or, against an existing resumable instance, resumes) the CSV
/// import job through the real production launch path. Job identity
/// includes the input's SHA-256 as an *identifying* parameter (spec ss15):
/// the same file resumes the same instance; a same-named file whose content
/// changed hashes differently and therefore addresses a *different*
/// instance, so the framework itself refuses to resume a mutated input
/// against a stale checkpoint (proven in `tests/input_identity.rs`, not
/// assumed here).
#[allow(clippy::too_many_arguments)]
pub async fn run(
    database_url: &str,
    input: &Path,
    import_name: &str,
    chunk_size: u32,
    fail_at: Option<FailAt>,
    failure_mode: FailureMode,
    hard_crash: bool,
    idempotent_writes: bool,
) -> anyhow::Result<()> {
    let clock = Arc::new(SystemClock);
    let repository = PostgresJobRepository::connect(pg_config(database_url)?, clock).await?;
    let ids = SequentialIdGenerator::new(std::num::NonZeroU64::MIN);
    let launcher = JobLauncher::new(&repository, &SystemClock, &ids);

    let input_sha256 = crate::generator::sha256_of_file(input)?;
    tracing::info!(import_name, input = %input.display(), input_sha256, chunk_size, "starting run");

    let namespace = ComponentStreamIdentity::new(READER_NAMESPACE)?;
    let (raw_reader, stream, contract) =
        delimited_file_reader::<DelimitedRecord>(input, DelimitedDialect::csv(), namespace.clone())?;

    let (fail_at_row, fail_at_chunk) = match fail_at {
        Some(FailAt::Row(n)) => (n, 0),
        Some(FailAt::Chunk(n)) => (0, n),
        None => (0, 0),
    };
    let fire_after_commit = fail_at_chunk != 0 && failure_mode == FailureMode::AfterBusinessCommit;
    let fired = Arc::new(AtomicBool::new(false));
    let chunk_ordinal = Arc::new(AtomicU32::new(0));

    let reader = FailingReader::new(raw_reader, fail_at_row, hard_crash, Arc::clone(&fired));

    let conflict_clause = idempotent_writes.then_some("ON CONFLICT (customer_id) DO NOTHING");
    let raw_writer = CustomerRowWriter::new(conflict_clause);
    let writer = FailingWriter::new(
        raw_writer,
        Arc::clone(&chunk_ordinal),
        fail_at_chunk,
        failure_mode,
        hard_crash,
        Arc::clone(&fired),
    );

    let raw_transactions = PostgresChunkTransactionManager::new(repository.clone(), state_provider());
    let transactions = Arc::new(FailingTransactionManager::new(
        raw_transactions,
        Arc::clone(&chunk_ordinal),
        fail_at_chunk,
        fire_after_commit,
        hard_crash,
        Arc::clone(&fired),
    ));

    let step = ChunkStep::new(
        StepName::new("import_customers")?,
        ChunkSize::new(chunk_size)?,
        reader,
        CustomerRowProcessor,
        writer,
        transactions,
        Arc::new(NoopChunkCompletion),
    )
    .with_item_stream(namespace.clone(), stream, contract);

    let mut chunk_job = ChunkJob::new(
        JobName::new(import_name)?,
        step,
        DefinitionRevision::new("csv-postgres-import-v1")?,
        &component_revisions(namespace)?,
    )?;

    let parameters = JobParameters::try_from_iter([
        (
            ParameterName::new("import_name")?,
            JobParameter::new(ParameterValue::string(import_name)?, ParameterRole::Identifying),
        ),
        (
            ParameterName::new("input_sha256")?,
            JobParameter::new(ParameterValue::string(&input_sha256)?, ParameterRole::Identifying),
        ),
    ])?;
    let (_stop_source, stop_token) = StopSource::new();

    let report = launcher.launch_chunk(&mut chunk_job, &parameters, &stop_token).await?;
    let execution = report.launch().job_execution();
    tracing::info!(
        job_execution_id = %execution.id(),
        status = %execution.metadata().status(),
        fault_injected = fired.load(std::sync::atomic::Ordering::SeqCst),
        "run finished"
    );
    if let Some(chunk_report) = report.chunk() {
        tracing::info!(
            committed_read = chunk_report.committed_counts().read().get(),
            committed_written = chunk_report.committed_counts().written().get(),
            "chunk evidence"
        );
    }
    repository.close().await?;
    Ok(())
}

/// Marks a `Starting/Started/Stopping/Unknown` execution left behind by a
/// hard crash as recoverable, so a subsequent `run` can resume it. Mirrors
/// `oxide-batch`'s own `postgres_local_partition.rs` example.
pub async fn recover(database_url: &str, import_name: &str, input: &Path) -> anyhow::Result<()> {
    let clock = Arc::new(SystemClock);
    let repository = PostgresJobRepository::connect(pg_config(database_url)?, clock).await?;
    let input_sha256 = crate::generator::sha256_of_file(input)?;
    let parameters = JobParameters::try_from_iter([
        (
            ParameterName::new("import_name")?,
            JobParameter::new(ParameterValue::string(import_name)?, ParameterRole::Identifying),
        ),
        (
            ParameterName::new("input_sha256")?,
            JobParameter::new(ParameterValue::string(&input_sha256)?, ParameterRole::Identifying),
        ),
    ])?;
    let key = oxide_batch::JobInstanceKey::new(JobName::new(import_name)?, &parameters);

    let mut unit = repository.begin().await?;
    let instance = unit
        .find_job_instance(&key)
        .await?
        .ok_or_else(|| anyhow::anyhow!("no job instance found for this import_name/input"))?;
    let execution = unit
        .job_executions(instance.id())
        .await?
        .into_iter()
        .last()
        .ok_or_else(|| anyhow::anyhow!("job instance has no executions"))?;
    unit.rollback().await?;

    use oxide_batch::BatchStatus;
    if !matches!(
        execution.metadata().status(),
        BatchStatus::Starting | BatchStatus::Started | BatchStatus::Stopping | BatchStatus::Unknown
    ) {
        anyhow::bail!(
            "latest execution status is {}, which does not require recovery",
            execution.metadata().status()
        );
    }

    let evidence_digest = sha256_hex(format!("csv-postgres-operator-recovery:{import_name}").as_bytes());
    let request = RecoveryRequest::mark_failed(
        execution.version(),
        "CSV_POSTGRES_WORKLOAD_HARD_CRASH_RECOVERY",
        "csv-postgres-cli",
        evidence_digest,
        FailureCategory::PermanentInfrastructure,
        FailureId::new(1)?,
    )?;
    let mut unit = repository.begin().await?;
    let recovered = unit.recover_job_execution(execution.id(), &request).await?;
    unit.commit().await?;
    tracing::info!(
        job_execution_id = %execution.id(),
        resulting_status = %recovered.decision().resulting_status(),
        "marked crashed execution recoverable"
    );
    repository.close().await?;
    Ok(())
}
