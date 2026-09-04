//! Wires the transform job against the real production launch path
//! (`oxide_batch::JobLauncher::launch_chunk`) and against OxideBatch's own
//! PostgreSQL metadata migrator plus this workload's own source/business
//! table migration. Every restart-relevant behavior (checkpointing,
//! same-resource transaction enlistment, instance/resume matching) is the
//! framework's, not ours -- this module only configures it.
//!
//! Two reader modes are supported, selected explicitly by the caller (see
//! [`ReaderMode`], and `main.rs`'s required `--reader` flag): the released
//! `postgres_cursor_reader` (a real streamed server-side cursor) and the
//! released `postgres_paging_reader` (independent, bounded keyset pages, no
//! server-side resource held between pages). Both key off the same strict
//! total order (`customer_id`), read the same base query, and share every
//! job-launch concern that is not reader-shaped: source-stability guard,
//! source digest, processor, first-party writer, transaction manager,
//! `ChunkStep`/`ChunkJob` construction, `JobLauncher`, and terminal status
//! handling all go through the single [`launch_and_finish`] generic helper
//! below. Only reader construction, stream namespace, and component
//! revisions differ per mode -- see [`run`].

use std::sync::Arc;

use oxide_batch::item_components::{
    postgres_cursor_reader, postgres_paging_reader, KeysetColumn, PostgresCursorFormat,
    PostgresPagingFormat, PostgresRow,
};
use oxide_batch::{
    Checkpoint, ChunkCommitReceipt, ChunkComponentRevisions, ChunkCounts, ChunkDeliveryMode,
    ChunkJob, ChunkRestartContract, ChunkSize, ChunkStep, ComponentRevision,
    ComponentStreamIdentity, DefinitionRevision, ExecutionContext, ExecutionCounts, InFlightPolicy,
    ItemReader, ItemStream, JobLauncher, JobName, JobParameter, JobParameters, NoopChunkCompletion,
    ParameterName, ParameterRole, ParameterValue, PostgresChunkStateError,
    PostgresChunkStateProvider, PostgresChunkTransactionManager, PostgresConfig,
    PostgresJobRepository, PostgresMigrator, ReaderError, SequentialIdGenerator, StateLimits,
    StateSchemaId, StateSchemaVersion, StepName, StopSource, StreamStateContract, SystemClock,
    TlsMode,
};

use crate::processor::{CustomerProjector, ProjectedRow, SourceRow};

const CURSOR_READER_NAMESPACE: &str = "oxide-batch-workload.postgres-postgres.cursor-reader";
const PAGING_READER_NAMESPACE: &str = "oxide-batch-workload.postgres-postgres.paging-reader";
const CHECKPOINT_SCHEMA: &str = "oxide-batch-workload.postgres-postgres.checkpoint";
const CONTEXT_SCHEMA: &str = "oxide-batch-workload.postgres-postgres.execution-context";
const WORKLOAD_MIGRATION: &str = include_str!("../migrations/001_init.sql");

/// The whole job definition/identity contract, bumped from PR 1's single
/// `postgres-postgres-transform-v1` to a v2 generation: PR 2 adds a new
/// identifying parameter (`reader_mode`) and a second reader/stream pair,
/// which is itself a definition/identity contract change, independent of
/// whether any single reader's own behavior changed. The
/// checkpoint/execution-context schemas are unrelated and unchanged -- see
/// [`state_provider`]'s doc comment.
///
/// One revision string *per mode*, not one shared string: OxideBatch's own
/// repository ties `(job_name, definition_revision)` to exactly one fixed
/// manifest forever (`DefinitionDrift` is a hard error otherwise -- see
/// `oxide_batch_repository`), and cursor/paging declare genuinely different
/// manifests (different reader component revisions, see
/// [`component_revisions`]). Reusing one revision string for both modes
/// would make the *same* `import_name` unable to ever run both modes --
/// exactly the scenario `tests/reader_mode_identity.rs` requires -- without
/// hitting that drift error. Each mode's own string is still a "v2"
/// generation, satisfying the required v1 -> v2 bump.
const fn definition_revision(mode: ReaderMode) -> &'static str {
    match mode {
        ReaderMode::Cursor => "postgres-postgres-transform-cursor-v2",
        ReaderMode::Paging => "postgres-postgres-transform-paging-v2",
    }
}

const BASE_QUERY: &str =
    "SELECT customer_id, full_name, is_active, balance_cents FROM app_source.source_customer";

pub const DEFAULT_FETCH_SIZE: usize = 200;
/// Independent of [`DEFAULT_FETCH_SIZE`]: paging's bounded page size and
/// cursor's bounded `FETCH` batch size are different released components'
/// different configuration knobs (`PostgresPagingFormat::with_page_size` vs
/// `PostgresCursorFormat::with_fetch_size`), not the same value under two
/// names.
pub const DEFAULT_PAGE_SIZE: usize = 250;

/// Explicit reader-mode selection (`--reader cursor|paging`; see
/// `main.rs`). Deliberately not defaulted anywhere in this crate: which
/// reader a run uses is restart-load-bearing (see [`parameters`]), so
/// silently picking one is not acceptable.
#[derive(Clone, Copy, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum ReaderMode {
    /// The released `postgres_cursor_reader`: a real streamed server-side
    /// `DECLARE CURSOR` / bounded `FETCH` session held on one dedicated
    /// connection for the run's duration.
    Cursor,
    /// The released `postgres_paging_reader`: independent, bounded
    /// `WHERE (customer_id) > (last) ORDER BY customer_id LIMIT page_size`
    /// pages, no server-side resource held between pages, never `OFFSET`.
    Paging,
}

impl ReaderMode {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Cursor => "cursor",
            Self::Paging => "paging",
        }
    }
}

impl std::fmt::Display for ReaderMode {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

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
/// separate from (and complementary to) whichever reader's own `ItemStream`
/// state is registered on the step below. Shared, unchanged schema for both
/// reader modes: which reader produced a position says nothing about the
/// shape of "how many items have committed so far," so there is no semantic
/// reason to fork or bump `CHECKPOINT_SCHEMA`/`CONTEXT_SCHEMA` just because
/// a second reader mode now exists.
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

/// Builds this job's component revisions for `mode`, namespaced under
/// `namespace`. Processor, writer, and checkpoint revisions are fixed and
/// shared by both modes -- neither changed in this PR -- and so is the
/// `AtomicSameResource` restart contract (see the module-level doc comment
/// on why that is the only delivery mode `PostgresBatchWriter` is compatible
/// with). Only the reader and stream revisions differ, and they must:
/// resuming a paging instance's state through a cursor-revisioned reader (or
/// vice versa) would be a silent, incorrect reinterpretation of persisted
/// keyset-position bytes across two different released readers.
fn component_revisions(
    mode: ReaderMode,
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
    let (reader_revision, stream_revision) = match mode {
        ReaderMode::Cursor => (
            "postgres-postgres.cursor-reader-v1",
            "postgres-postgres.cursor-reader-stream-v1",
        ),
        ReaderMode::Paging => (
            "postgres-postgres.paging-reader-v1",
            "postgres-postgres.paging-reader-stream-v1",
        ),
    };
    Ok(ChunkComponentRevisions::new(
        ComponentRevision::new(reader_revision)?,
        ComponentRevision::new("postgres-postgres.customer-projector-v1")?,
        ComponentRevision::new("postgres-postgres.postgres-batch-writer-v1")?,
        ComponentRevision::new("postgres-postgres.checkpoint-v1")?,
        restart,
    )
    .with_stream_revision(namespace, ComponentRevision::new(stream_revision)?))
}

fn key_columns() -> Vec<KeysetColumn> {
    vec![KeysetColumn::i64("customer_id")]
}

fn map_source_row(row: &PostgresRow<'_>) -> Result<SourceRow, ReaderError> {
    Ok(SourceRow {
        customer_id: row.i64("customer_id")?,
        full_name: row.text("full_name")?,
        is_active: row.bool("is_active")?,
        balance_cents: row.i64("balance_cents")?,
    })
}

/// The job's identifying parameters: `import_name`, `source_digest`
/// (unchanged from PR 1 -- see `src/source_digest.rs`), and now
/// `reader_mode`. `reader_mode` is identifying, not incidental: cursor and
/// paging are two different released components with two different
/// `ItemStream` state shapes, so the same `import_name` and the exact same
/// source content under different reader modes must resolve to different
/// `JobInstanceKey`s -- resuming (or worse, silently reinterpreting) one
/// mode's persisted keyset/cursor state through the other reader is not a
/// resume, it is data corruption. This intentionally means a PR 1 cursor
/// `JobInstance` (which predates this parameter entirely) is not resumed by
/// the PR 2 identity scheme; see `tests/reader_mode_identity.rs` and the
/// crate README's compatibility note. No framework metadata is mutated to
/// migrate historical instances -- that is out of scope for this
/// campaign-local compatibility transition.
fn parameters(
    import_name: &str,
    source_digest: &str,
    mode: ReaderMode,
) -> anyhow::Result<JobParameters> {
    Ok(JobParameters::try_from_iter([
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
                ParameterValue::string(source_digest)?,
                ParameterRole::Identifying,
            ),
        ),
        (
            ParameterName::new("reader_mode")?,
            JobParameter::new(
                ParameterValue::string(mode.as_str())?,
                ParameterRole::Identifying,
            ),
        ),
    ])?)
}

/// Common tail shared by both reader modes: builds the transaction manager,
/// `ChunkStep`/`ChunkJob`, launches through the real production
/// `JobLauncher::launch_chunk` path, closes the source-stability window,
/// and translates a non-`Completed` terminal status into a process-visible
/// error. Generic only in the reader type `R` -- the one thing that
/// legitimately differs in concrete type between cursor and paging (see the
/// module-level doc comment); processor, writer, and everything else here
/// is the exact same concrete type regardless of `mode`.
///
/// `source_guard`/`guard_pool` are threaded through (not reacquired here)
/// because the source-stability guard must span from *before* the digest
/// was computed in [`run`] until *after* this function's own
/// `launch_chunk().await` returns -- see `src/source_digest.rs`'s module
/// documentation. This is exactly as true for paging as it is for cursor:
/// both readers open their own source connection/pool after the digest is
/// computed, so both need the guard held across the entire span.
#[allow(clippy::too_many_arguments)]
async fn launch_and_finish<R>(
    launcher: &JobLauncher<'_>,
    repository: &PostgresJobRepository,
    source_guard: sqlx::Transaction<'static, sqlx::Postgres>,
    guard_pool: sqlx::PgPool,
    import_name: &str,
    chunk_size: u32,
    mode: ReaderMode,
    reader: R,
    stream: impl ItemStream + 'static,
    contract: StreamStateContract,
    namespace: ComponentStreamIdentity,
    revisions: ChunkComponentRevisions,
    processor: CustomerProjector,
    writer: oxide_batch::item_components::PostgresBatchWriter<ProjectedRow>,
    parameters: JobParameters,
) -> anyhow::Result<()>
where
    R: ItemReader<SourceRow> + Send + 'static,
{
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
    .with_item_stream(namespace, stream, contract);

    let mut chunk_job = ChunkJob::new(
        JobName::new(import_name)?,
        step,
        DefinitionRevision::new(definition_revision(mode))?,
        &revisions,
    )?;

    let (_stop_source, stop_token) = StopSource::new();

    let report = launcher
        .launch_chunk(&mut chunk_job, &parameters, &stop_token)
        .await?;

    // The source-stability window closes here: the reader has finished
    // reading (launch_chunk has returned), so the digest computed in `run`
    // is now provably a description of what this run actually processed.
    // Only after this does any other session's write to
    // app_source.source_customer unblock. True identically for both reader
    // modes -- see this function's own doc comment.
    source_guard.commit().await?;
    guard_pool.close().await;

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

/// Launches (or, against an existing resumable instance, resumes) the
/// transform job through the real production launch path, in the reader
/// mode `reader_mode` explicitly selects.
///
/// Job identity includes `import_name`, the streaming source content digest
/// (`source_digest::compute`), and now `reader_mode`, all as *identifying*
/// parameters (see [`parameters`]).
///
/// `fetch_size` is cursor-only configuration; `page_size` is paging-only.
/// Supplying the mode-incompatible option is a configuration error, not a
/// silently ignored no-op -- checked first, before any database connection
/// is opened, so a misconfigured invocation fails fast and is never
/// reported as a completed run (see `tests/reader_config.rs`).
///
/// The digest and the actual source read whichever reader performs later in
/// this same call must describe the *same* content, or `source_digest`
/// would identify content this run never actually processed. A digest
/// computed on one connection/snapshot and a reader that opens its own,
/// separate connection afterward is a real time-of-check-to-time-of-use gap
/// otherwise: `source_digest::lock_source_for_stable_read` closes it by
/// holding a database-enforced `LOCK TABLE ... IN SHARE MODE` on
/// `app_source.source_customer` from before the digest is computed until
/// after [`launch_and_finish`]'s call into `launch_chunk` returns, so no
/// other session's write to that table can land in between -- for both
/// reader modes (see `src/source_digest.rs`'s module documentation, and
/// `tests/source_stability.rs` for tests that actually attack this window
/// rather than assuming it is closed).
pub async fn run(
    database_url: &str,
    import_name: &str,
    chunk_size: u32,
    reader_mode: ReaderMode,
    fetch_size: Option<usize>,
    page_size: Option<usize>,
) -> anyhow::Result<()> {
    match reader_mode {
        ReaderMode::Cursor if page_size.is_some() => {
            anyhow::bail!(
                "--page-size is only valid with --reader paging; this run selected --reader cursor \
                 (use --fetch-size for cursor mode instead)"
            );
        }
        ReaderMode::Paging if fetch_size.is_some() => {
            anyhow::bail!(
                "--fetch-size is only valid with --reader cursor; this run selected --reader paging \
                 (use --page-size for paging mode instead)"
            );
        }
        _ => {}
    }
    let fetch_size = fetch_size.unwrap_or(DEFAULT_FETCH_SIZE);
    let page_size = page_size.unwrap_or(DEFAULT_PAGE_SIZE);

    let clock = Arc::new(SystemClock);
    let repository = PostgresJobRepository::connect(pg_config(database_url)?, clock).await?;
    let ids = SequentialIdGenerator::new(std::num::NonZeroU64::MIN);
    let launcher = JobLauncher::new(&repository, &SystemClock, &ids);

    let guard_pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(database_url)
        .await?;
    let mut source_guard = crate::source_digest::lock_source_for_stable_read(&guard_pool).await?;
    let source_digest = crate::source_digest::compute(&mut *source_guard).await?;
    tracing::info!(
        import_name,
        source_digest,
        chunk_size,
        reader_mode = reader_mode.as_str(),
        "starting run"
    );

    let writer = crate::writer::writer()?;
    let processor = CustomerProjector {
        import_name: import_name.to_owned(),
        source_digest: source_digest.clone(),
    };
    let job_parameters = parameters(import_name, &source_digest, reader_mode)?;

    match reader_mode {
        ReaderMode::Cursor => {
            tracing::info!(fetch_size, "cursor mode configuration");
            let namespace = ComponentStreamIdentity::new(CURSOR_READER_NAMESPACE)?;
            let format = PostgresCursorFormat::new().with_fetch_size(fetch_size);
            let (reader, stream, contract) = postgres_cursor_reader(
                pg_config(database_url)?,
                BASE_QUERY,
                key_columns(),
                format,
                map_source_row,
                namespace.clone(),
            )?;
            let revisions = component_revisions(reader_mode, namespace.clone())?;
            launch_and_finish(
                &launcher,
                &repository,
                source_guard,
                guard_pool,
                import_name,
                chunk_size,
                reader_mode,
                reader,
                stream,
                contract,
                namespace,
                revisions,
                processor,
                writer,
                job_parameters,
            )
            .await
        }
        ReaderMode::Paging => {
            tracing::info!(page_size, "paging mode configuration");
            let namespace = ComponentStreamIdentity::new(PAGING_READER_NAMESPACE)?;
            let format = PostgresPagingFormat::new().with_page_size(page_size);
            let (reader, stream, contract) = postgres_paging_reader(
                pg_config(database_url)?,
                BASE_QUERY,
                key_columns(),
                format,
                map_source_row,
                namespace.clone(),
            )?;
            let revisions = component_revisions(reader_mode, namespace.clone())?;
            launch_and_finish(
                &launcher,
                &repository,
                source_guard,
                guard_pool,
                import_name,
                chunk_size,
                reader_mode,
                reader,
                stream,
                contract,
                namespace,
                revisions,
                processor,
                writer,
                job_parameters,
            )
            .await
        }
    }
}
