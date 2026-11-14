use std::num::NonZeroU64;
use std::path::PathBuf;
use std::sync::atomic::AtomicU32;
use std::sync::Arc;

use oxide_batch::item_components::{
    postgres_paging_reader, KeysetColumn, PostgresPagingFormat, PostgresRow,
};
use oxide_batch::{
    BatchStatus, Checkpoint, ChunkCommitReceipt, ChunkComponentRevisions, ChunkCounts,
    ChunkDeliveryMode, ChunkJob, ChunkRestartContract, ChunkSize, ChunkStep, ComponentRevision,
    ComponentStreamIdentity, DefinitionRevision, ExecutionContext, ExecutionCounts,
    FailureCategory, FailureId, InFlightPolicy, JobInstanceKey, JobLauncher, JobName, JobParameter,
    JobParameters, JobRepository, NoopChunkCompletion, ParameterName, ParameterRole,
    ParameterValue, PostgresChunkStateError, PostgresChunkStateProvider,
    PostgresChunkTransactionManager, PostgresConfig, PostgresJobRepository, ReaderError,
    RecoveryRequest, SequentialIdGenerator, StateLimits, StateSchemaId, StateSchemaVersion,
    StepName, StopSource, SystemClock, TlsMode,
};
use sha2::{Digest, Sha256};
use sqlx::Row;

use crate::failpoint::{FailingTransactionManager, FailingWriter, Failpoint, FailureMode};
use crate::model::{hex_digest, SnapshotBundle, CONTRACT_VERSION};
use crate::processor::{ReconciliationProcessor, ReconciliationSourceRow};

const JOB_NAME: &str = "payment-reconciliation-postgres-v1";
const DEFINITION_REVISION: &str = "payment-reconciliation-postgres-paging-v1";
const READER_NAMESPACE: &str =
    "oxide-batch-workloads.payment-reconciliation-postgres.paging-reader";
const CHECKPOINT_SCHEMA: &str = "oxide-batch-workloads.payment-reconciliation-postgres.checkpoint";
const CONTEXT_SCHEMA: &str =
    "oxide-batch-workloads.payment-reconciliation-postgres.execution-context";

pub const DEFAULT_CHUNK_SIZE: u32 = 100;
pub const DEFAULT_PAGE_SIZE: usize = 250;

fn pg_config(database_url: &str) -> anyhow::Result<PostgresConfig> {
    Ok(PostgresConfig::new(database_url)?.with_tls_mode(TlsMode::Plaintext))
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
        ComponentRevision::new("payment-reconciliation.snapshot-reader-v1")?,
        ComponentRevision::new("payment-reconciliation.classifier-v1")?,
        ComponentRevision::new("payment-reconciliation.detail-writer-v1")?,
        ComponentRevision::new("payment-reconciliation.checkpoint-v1")?,
        restart,
    )
    .with_stream_revision(
        namespace,
        ComponentRevision::new("payment-reconciliation.snapshot-reader-stream-v1")?,
    ))
}

fn parameters(bundle: &SnapshotBundle) -> anyhow::Result<JobParameters> {
    let metadata = &bundle.metadata;
    Ok(JobParameters::try_from_iter([
        identifying("snapshot_id", &metadata.snapshot_id)?,
        identifying("contract_version", CONTRACT_VERSION)?,
        identifying("business_date", &metadata.business_date)?,
        identifying("provider_scope", &metadata.provider_scope)?,
        identifying("scenario_case", &metadata.scenario_case)?,
        identifying(
            "internal_snapshot_digest",
            &metadata.internal_snapshot_digest,
        )?,
        identifying(
            "provider_snapshot_digest",
            &metadata.provider_snapshot_digest,
        )?,
        identifying("policy_version", &metadata.policy_version)?,
    ])?)
}

fn identifying(name: &str, value: &str) -> anyhow::Result<(ParameterName, JobParameter)> {
    Ok((
        ParameterName::new(name)?,
        JobParameter::new(ParameterValue::string(value)?, ParameterRole::Identifying),
    ))
}

pub fn run_id(bundle: &SnapshotBundle) -> String {
    let metadata = &bundle.metadata;
    let mut hasher = Sha256::new();
    for value in [
        metadata.snapshot_id.as_str(),
        CONTRACT_VERSION,
        metadata.business_date.as_str(),
        metadata.provider_scope.as_str(),
        metadata.scenario_case.as_str(),
        metadata.internal_snapshot_digest.as_str(),
        metadata.provider_snapshot_digest.as_str(),
        metadata.policy_version.as_str(),
    ] {
        hasher.update(value.as_bytes());
        hasher.update([0]);
    }
    let digest = hasher.finalize();
    hex_digest(digest.as_ref())
}

fn source_query(snapshot_id: &str) -> String {
    format!(
        r#"
        SELECT
            COALESCE(i.provider_payment_id, p.provider_payment_id) AS provider_payment_id,
            COALESCE(i.row_count, 0)::BIGINT AS internal_row_count,
            COALESCE(p.row_count, 0)::BIGINT AS provider_row_count,
            COALESCE(i.currency, '') AS internal_currency,
            COALESCE(p.currency, '') AS provider_currency,
            COALESCE(i.normalized_status, '') AS internal_status,
            COALESCE(p.normalized_status, '') AS provider_status,
            COALESCE(i.authorized_amount_minor, 0)::BIGINT AS internal_authorized_amount_minor,
            COALESCE(p.authorized_amount_minor, 0)::BIGINT AS provider_authorized_amount_minor,
            COALESCE(i.captured_amount_minor, 0)::BIGINT AS internal_captured_amount_minor,
            COALESCE(p.captured_amount_minor, 0)::BIGINT AS provider_captured_amount_minor,
            COALESCE(i.refunded_amount_minor, 0)::BIGINT AS internal_refunded_amount_minor,
            COALESCE(p.refunded_amount_minor, 0)::BIGINT AS provider_refunded_amount_minor
        FROM (
            SELECT
                provider_payment_id,
                COUNT(*)::BIGINT AS row_count,
                MAX(currency) AS currency,
                MAX(normalized_status) AS normalized_status,
                SUM(authorized_amount_minor)::BIGINT AS authorized_amount_minor,
                SUM(captured_amount_minor)::BIGINT AS captured_amount_minor,
                SUM(refunded_amount_minor)::BIGINT AS refunded_amount_minor
            FROM payment_reconciliation.internal_payment_snapshot
            WHERE snapshot_id = '{snapshot_id}'
            GROUP BY provider_payment_id
        ) i
        FULL OUTER JOIN (
            SELECT
                provider_payment_id,
                COUNT(*)::BIGINT AS row_count,
                MAX(currency) AS currency,
                MAX(normalized_status) AS normalized_status,
                SUM(authorized_amount_minor)::BIGINT AS authorized_amount_minor,
                SUM(captured_amount_minor)::BIGINT AS captured_amount_minor,
                SUM(refunded_amount_minor)::BIGINT AS refunded_amount_minor
            FROM payment_reconciliation.provider_payment_snapshot
            WHERE snapshot_id = '{snapshot_id}'
            GROUP BY provider_payment_id
        ) p USING (provider_payment_id)
        "#
    )
}

fn map_source(row: &PostgresRow<'_>) -> Result<ReconciliationSourceRow, ReaderError> {
    Ok(ReconciliationSourceRow {
        provider_payment_id: row.text("provider_payment_id")?,
        internal_row_count: row.i64("internal_row_count")?,
        provider_row_count: row.i64("provider_row_count")?,
        internal_currency: row.text("internal_currency")?,
        provider_currency: row.text("provider_currency")?,
        internal_status: row.text("internal_status")?,
        provider_status: row.text("provider_status")?,
        internal_authorized_amount_minor: row.i64("internal_authorized_amount_minor")?,
        provider_authorized_amount_minor: row.i64("provider_authorized_amount_minor")?,
        internal_captured_amount_minor: row.i64("internal_captured_amount_minor")?,
        provider_captured_amount_minor: row.i64("provider_captured_amount_minor")?,
        internal_refunded_amount_minor: row.i64("internal_refunded_amount_minor")?,
        provider_refunded_amount_minor: row.i64("provider_refunded_amount_minor")?,
    })
}

pub struct RunRequest<'a> {
    pub database_url: &'a str,
    pub snapshot_id: &'a str,
    pub chunk_size: u32,
    pub page_size: usize,
    pub fail_at_chunk: u32,
    pub failure_mode: FailureMode,
    pub pause_for_kill: Option<PathBuf>,
    pub pause_after_processing_commit: Option<PathBuf>,
}

pub async fn run(request: RunRequest<'_>) -> anyhow::Result<String> {
    let RunRequest {
        database_url,
        snapshot_id,
        chunk_size,
        page_size,
        fail_at_chunk,
        failure_mode,
        pause_for_kill,
        pause_after_processing_commit,
    } = request;
    let bundle =
        crate::snapshot::load_integrity_checked_snapshot(database_url, snapshot_id).await?;
    let run_id = run_id(&bundle);
    if !prepare_run(database_url, &run_id, snapshot_id).await? {
        return Ok(run_id);
    }

    let clock = Arc::new(SystemClock);
    let repository = PostgresJobRepository::connect(pg_config(database_url)?, clock).await?;
    let ids = SequentialIdGenerator::new(NonZeroU64::MIN);
    let launcher = JobLauncher::new(&repository, &SystemClock, &ids);

    let namespace = ComponentStreamIdentity::new(READER_NAMESPACE)?;
    let (reader, stream, contract) = postgres_paging_reader(
        pg_config(database_url)?,
        source_query(snapshot_id),
        vec![KeysetColumn::text("provider_payment_id")],
        PostgresPagingFormat::new().with_page_size(page_size),
        map_source,
        namespace.clone(),
    )?;

    let failpoint = match (fail_at_chunk, pause_for_kill) {
        (0, None) => Failpoint::disabled(),
        (0, Some(_)) => anyhow::bail!("--pause-for-kill requires --fail-at-chunk >= 1"),
        (_, None) => anyhow::bail!("--fail-at-chunk requires --pause-for-kill"),
        (target, Some(marker)) => Failpoint::armed(target, failure_mode, marker)?,
    };
    let chunk_ordinal = Arc::new(AtomicU32::new(0));
    let raw_transactions =
        PostgresChunkTransactionManager::new(repository.clone(), state_provider());
    let transactions = FailingTransactionManager::new(
        raw_transactions,
        Arc::clone(&chunk_ordinal),
        failpoint.clone(),
    );
    let writer = FailingWriter::new(
        crate::writer::writer()?,
        Arc::clone(&chunk_ordinal),
        failpoint,
    );
    let step = ChunkStep::new(
        StepName::new("reconcile_frozen_payment_snapshot")?,
        ChunkSize::new(chunk_size)?,
        reader,
        ReconciliationProcessor {
            run_id: run_id.clone(),
        },
        writer,
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
    let job_parameters = parameters(&bundle)?;
    let (_stop_source, stop_token) = StopSource::new();
    let report = launcher
        .launch_chunk(&mut chunk_job, &job_parameters, &stop_token)
        .await?;

    let status = report.launch().job_execution().metadata().status();
    repository.close().await?;
    if status != BatchStatus::Completed {
        anyhow::bail!("reconciliation execution ended with status {status}, not Completed");
    }

    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;
    sqlx::query(
        "UPDATE payment_reconciliation.reconciliation_run
         SET processing_state = 'PROCESSED', updated_at = NOW()
         WHERE run_id = $1 AND business_state IS NULL",
    )
    .bind(&run_id)
    .execute(&pool)
    .await?;
    pool.close().await;

    if let Some(marker) = pause_after_processing_commit {
        crate::failpoint::pause_for_external_kill(
            &marker,
            "after-processing-commit-before-finalization",
        )
        .await;
    }

    Ok(run_id)
}

async fn prepare_run(database_url: &str, run_id: &str, snapshot_id: &str) -> anyhow::Result<bool> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;

    sqlx::query(
        "INSERT INTO payment_reconciliation.reconciliation_run
         (run_id, snapshot_id, processing_state)
         VALUES ($1, $2, 'PENDING')
         ON CONFLICT (run_id) DO NOTHING",
    )
    .bind(run_id)
    .bind(snapshot_id)
    .execute(&pool)
    .await?;

    let row = sqlx::query(
        "SELECT snapshot_id, processing_state, business_state
         FROM payment_reconciliation.reconciliation_run
         WHERE run_id = $1",
    )
    .bind(run_id)
    .fetch_one(&pool)
    .await?;

    let stored_snapshot_id: String = row.try_get("snapshot_id")?;
    let business_state: Option<String> = row.try_get("business_state")?;
    if stored_snapshot_id != snapshot_id {
        anyhow::bail!("run identity collision with a different snapshot");
    }
    if let Some(state) = business_state {
        anyhow::bail!("reconciliation run is already finalized as {state}");
    }

    let processing_state: String = row.try_get("processing_state")?;
    match processing_state.as_str() {
        "PROCESSED" => {
            pool.close().await;
            return Ok(false);
        }
        "PROCESSING" => {
            pool.close().await;
            anyhow::bail!(
                "reconciliation run is already PROCESSING; checkpoint 5 recovery is required"
            );
        }
        "PENDING" | "FAILED" => {}
        other => {
            pool.close().await;
            anyhow::bail!("unsupported reconciliation processing state: {other}");
        }
    }

    sqlx::query(
        "UPDATE payment_reconciliation.reconciliation_run
         SET processing_state = 'PROCESSING', updated_at = NOW()
         WHERE run_id = $1",
    )
    .bind(run_id)
    .execute(&pool)
    .await?;

    pool.close().await;
    Ok(true)
}

pub async fn recover(database_url: &str, snapshot_id: &str) -> anyhow::Result<String> {
    let bundle =
        crate::snapshot::load_integrity_checked_snapshot(database_url, snapshot_id).await?;
    let run_id = run_id(&bundle);

    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;
    let run_state: Option<(String, Option<String>)> = sqlx::query_as(
        "SELECT processing_state, business_state
         FROM payment_reconciliation.reconciliation_run
         WHERE run_id = $1",
    )
    .bind(&run_id)
    .fetch_optional(&pool)
    .await?;
    let Some((processing_state, business_state)) = run_state else {
        pool.close().await;
        anyhow::bail!("reconciliation run does not exist for snapshot {snapshot_id}");
    };
    if business_state.is_some() {
        pool.close().await;
        anyhow::bail!("finalized reconciliation runs are not recovery candidates");
    }
    if processing_state != "PROCESSING" {
        pool.close().await;
        anyhow::bail!("recovery requires workload state PROCESSING, observed {processing_state}");
    }

    let clock = Arc::new(SystemClock);
    let repository = PostgresJobRepository::connect(pg_config(database_url)?, clock).await?;
    let job_parameters = parameters(&bundle)?;
    let key = JobInstanceKey::new(JobName::new(JOB_NAME)?, &job_parameters);

    let mut unit = repository.begin().await?;
    let instance = unit.find_job_instance(&key).await?.ok_or_else(|| {
        anyhow::anyhow!("no OxideBatch JobInstance matches the frozen snapshot identity")
    })?;
    let execution = unit
        .job_executions(instance.id())
        .await?
        .into_iter()
        .last()
        .ok_or_else(|| anyhow::anyhow!("matching OxideBatch JobInstance has no executions"))?;
    unit.rollback().await?;

    if !matches!(
        execution.metadata().status(),
        BatchStatus::Starting | BatchStatus::Started | BatchStatus::Stopping | BatchStatus::Unknown
    ) {
        pool.close().await;
        repository.close().await?;
        anyhow::bail!(
            "latest OxideBatch execution status is {}, not a hard-crash recovery candidate",
            execution.metadata().status()
        );
    }

    let mut hasher = Sha256::new();
    hasher.update(b"payment-reconciliation-hard-crash-recovery-v1\0");
    hasher.update(run_id.as_bytes());
    let evidence_digest: [u8; 32] = hasher.finalize().into();

    let request = RecoveryRequest::mark_failed(
        execution.version(),
        "PAYMENT_RECONCILIATION_HARD_CRASH_RECOVERY",
        "payment-reconciliation-postgres-cli",
        evidence_digest,
        FailureCategory::PermanentInfrastructure,
        FailureId::new(1)?,
    )?;
    let mut unit = repository.begin().await?;
    unit.recover_job_execution(execution.id(), &request).await?;
    unit.commit().await?;
    repository.close().await?;

    let updated = sqlx::query(
        "UPDATE payment_reconciliation.reconciliation_run
         SET processing_state = 'PENDING', updated_at = NOW()
         WHERE run_id = $1
           AND processing_state = 'PROCESSING'
           AND business_state IS NULL",
    )
    .bind(&run_id)
    .execute(&pool)
    .await?;
    pool.close().await;
    if updated.rows_affected() != 1 {
        anyhow::bail!("workload recovery state transition did not update exactly one run");
    }

    Ok(run_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::SnapshotMetadata;

    fn bundle() -> SnapshotBundle {
        SnapshotBundle {
            metadata: SnapshotMetadata {
                snapshot_id: "snapshot-1".into(),
                business_date: "2026-11-14".into(),
                provider_scope: "merchant-1".into(),
                scenario_case: "CLEAR".into(),
                base_snapshot_id: None,
                internal_snapshot_digest: "a".repeat(64),
                provider_snapshot_digest: "b".repeat(64),
                policy_version: "policy-v1".into(),
            },
            internal: vec![],
            provider: vec![],
        }
    }

    #[test]
    fn run_identity_changes_when_snapshot_content_identity_changes() {
        let a = bundle();
        let mut b = bundle();
        b.metadata.provider_snapshot_digest = "c".repeat(64);
        assert_ne!(run_id(&a), run_id(&b));
    }

    #[test]
    fn source_query_keeps_business_verdict_out_of_sql() {
        let sql = source_query("snapshot-1");
        for verdict in [
            "MISSING_PROVIDER",
            "MISSING_INTERNAL",
            "DUPLICATE_INTERNAL",
            "DUPLICATE_PROVIDER",
            "AMOUNT_MISMATCH",
            "ATTRIBUTE_MISMATCH",
            "MATCH",
        ] {
            assert!(!sql.contains(verdict));
        }
        assert!(sql.contains("COUNT(*)"));
        assert!(sql.contains("FULL OUTER JOIN"));
    }
}
