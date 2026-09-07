use std::error::Error;
use std::fmt;
use std::fmt::Write as _;
use std::num::NonZeroU64;
use std::str::FromStr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{bail, Result};
use clap::{Parser, Subcommand};
use futures_util::TryStreamExt;
use oxide_batch::{
    BatchStatus, BoxFuture, ComponentRevision, DefinitionRevision, ExecutionContext,
    FlowExecutionOutcome, FlowGraph, FlowJob, FlowLauncher, FlowNode, FlowRuntimeError, FlowTarget,
    JobInstanceKey, JobName, JobParameters, JobRepository, NodeId, PartitionBudget, PartitionCount,
    PartitionFactoryError, PartitionKey, PartitionPlanEntry, PartitionPlanFactory,
    PartitionTaskletFactory, PartitionedStepNode, PostgresConfig, PostgresJobRepository,
    PostgresMigrator, SequentialIdGenerator, StateLimits, StepComponents, StepName, StepNode,
    StopSource, SystemClock, Tasklet, TaskletContext, TaskletError, TaskletOutcome, TaskletStep,
    TerminalKind, TlsMode,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions, PgSslMode};
use sqlx::{PgPool, Row};

const JOB_PREFIX: &str = "postgres-local-partition";
const APP_APPLICATION_NAME: &str = "oxide-batch-workload-local-partition";
const BUSINESS_POOL_CONNECTIONS: u32 = 64;
const ADMISSION_TIMEOUT: Duration = Duration::from_secs(30);
const DENSE_WORKER_POINTS: [u8; 7] = [1, 2, 4, 8, 16, 32, 64];

#[derive(Parser)]
#[command(name = "postgres-local-partition")]
#[command(about = "External OxideBatch 0.6.0 local-partition validation workload")]
struct Cli {
    #[arg(long, env = "DATABASE_URL")]
    database_url: String,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Migrate,
    Seed {
        #[arg(long)]
        rows: u64,
        #[arg(long)]
        seed: i64,
    },
    Run {
        #[arg(long)]
        run_name: String,
        #[arg(long)]
        rows: u64,
        #[arg(long)]
        partitions: u16,
        #[arg(long)]
        workers: u8,
    },
    Verify {
        #[arg(long)]
        run_name: String,
        #[arg(long)]
        rows: u64,
        #[arg(long)]
        partitions: u16,
    },
    Qualify {
        #[arg(long)]
        prefix: String,
        #[arg(long)]
        rows: u64,
        #[arg(long)]
        partitions: u16,
    },
    Corrupt {
        #[arg(long)]
        run_name: String,
    },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
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

#[derive(Clone, Debug, Eq, PartialEq)]
struct Assignment {
    index: u16,
    key: String,
    start: u64,
    end: u64,
}

#[derive(Default)]
struct Occupancy {
    active: AtomicUsize,
    peak: AtomicUsize,
    durations: Mutex<Vec<Duration>>,
}

impl Occupancy {
    fn enter(&self) {
        let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(active, Ordering::SeqCst);
    }

    fn leave(&self, elapsed: Duration) {
        self.active.fetch_sub(1, Ordering::SeqCst);
        if let Ok(mut durations) = self.durations.lock() {
            durations.push(elapsed);
        }
    }

    fn active(&self) -> usize {
        self.active.load(Ordering::SeqCst)
    }

    fn peak(&self) -> usize {
        self.peak.load(Ordering::SeqCst)
    }
}

struct AdmissionGate {
    target: usize,
    next_slot: AtomicUsize,
    arrived: AtomicUsize,
    barrier: tokio::sync::Barrier,
}

impl AdmissionGate {
    fn new(target: usize) -> Self {
        Self {
            target,
            next_slot: AtomicUsize::new(0),
            arrived: AtomicUsize::new(0),
            barrier: tokio::sync::Barrier::new(target.max(1)),
        }
    }

    async fn admit(&self) -> std::result::Result<(), AdmissionTimeout> {
        let slot = self.next_slot.fetch_add(1, Ordering::SeqCst);
        if slot >= self.target {
            return Ok(());
        }
        self.arrived.fetch_add(1, Ordering::SeqCst);
        tokio::time::timeout(ADMISSION_TIMEOUT, self.barrier.wait())
            .await
            .map(|_| ())
            .map_err(|_| AdmissionTimeout {
                target: self.target,
                arrived: self.arrived.load(Ordering::SeqCst),
            })
    }
}

#[derive(Debug)]
struct AdmissionTimeout {
    target: usize,
    arrived: usize,
}

impl fmt::Display for AdmissionTimeout {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "admission gate timed out waiting for {} workers; observed {} arrivals",
            self.target, self.arrived
        )
    }
}

impl Error for AdmissionTimeout {}

#[derive(Debug)]
struct WorkerFailure(String);

impl fmt::Display for WorkerFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for WorkerFailure {}

struct PartitionWorker {
    occupancy: Arc<Occupancy>,
    gate: Arc<AdmissionGate>,
    business: PgPool,
    run_name: String,
    key: String,
    context_json: Option<Vec<u8>>,
    expected_source_digest: String,
    rows_per_partition: u64,
}

impl Tasklet for PartitionWorker {
    fn execute<'a>(
        &'a self,
        _context: TaskletContext<'a>,
    ) -> BoxFuture<'a, std::result::Result<TaskletOutcome, TaskletError>> {
        Box::pin(async move {
            let started = Instant::now();
            self.occupancy.enter();
            let result = async {
                self.gate.admit().await.map_err(TaskletError::from_error)?;

                let context_json = self.context_json.as_deref().ok_or_else(|| {
                    TaskletError::from_error(WorkerFailure(
                        "partition context could not be serialized".to_owned(),
                    ))
                })?;
                let envelope: ContextEnvelope =
                    serde_json::from_slice(context_json).map_err(|error| {
                        TaskletError::from_error(WorkerFailure(format!(
                            "partition context could not be decoded: {error}"
                        )))
                    })?;
                let payload = envelope.payload;
                let expected_key = partition_key(payload.partition_index);
                let expected_start = u64::from(payload.partition_index)
                    .checked_mul(self.rows_per_partition)
                    .and_then(|value| value.checked_add(1))
                    .ok_or_else(|| {
                        TaskletError::from_error(WorkerFailure(
                            "partition range start overflowed".to_owned(),
                        ))
                    })?;
                let expected_end = expected_start
                    .checked_add(self.rows_per_partition - 1)
                    .ok_or_else(|| {
                        TaskletError::from_error(WorkerFailure(
                            "partition range end overflowed".to_owned(),
                        ))
                    })?;

                if self.key != expected_key
                    || payload.range_start != expected_start
                    || payload.range_end != expected_end
                    || payload.source_digest != self.expected_source_digest
                {
                    return Err(TaskletError::from_error(WorkerFailure(
                        "durable partition assignment did not match the workload contract"
                            .to_owned(),
                    )));
                }

                let start = i64::try_from(payload.range_start).map_err(|_| {
                    TaskletError::from_error(WorkerFailure(
                        "partition range start exceeded PostgreSQL bigint".to_owned(),
                    ))
                })?;
                let end = i64::try_from(payload.range_end).map_err(|_| {
                    TaskletError::from_error(WorkerFailure(
                        "partition range end exceeded PostgreSQL bigint".to_owned(),
                    ))
                })?;

                let written = sqlx::query(
                    "INSERT INTO app_business.local_partition_projection \
                     (run_name, source_id, projected_value, partition_key) \
                     SELECT $1, source_id, payload_value * 3 + 7, $4 \
                     FROM app_source.local_partition_source \
                     WHERE source_id BETWEEN $2 AND $3 \
                     ORDER BY source_id \
                     ON CONFLICT (run_name, source_id) DO UPDATE SET \
                     projected_value = EXCLUDED.projected_value, \
                     partition_key = EXCLUDED.partition_key",
                )
                .bind(&self.run_name)
                .bind(start)
                .bind(end)
                .bind(&self.key)
                .execute(&self.business)
                .await
                .map_err(TaskletError::from_error)?;

                let expected_rows = payload.range_end - payload.range_start + 1;
                if written.rows_affected() != expected_rows {
                    return Err(TaskletError::from_error(WorkerFailure(format!(
                        "partition {} affected {} rows, expected {expected_rows}",
                        self.key,
                        written.rows_affected()
                    ))));
                }
                Ok(TaskletOutcome::Completed)
            }
            .await;
            self.occupancy.leave(started.elapsed());
            result
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct NormalizedDurableState {
    job_status: String,
    parent_status: String,
    partitions: Vec<(String, String)>,
}

#[derive(Debug, Serialize)]
struct FrameworkVerification {
    terminal_state_consistent: bool,
    durable_partition_plan_matches_expected: bool,
    durable_partition_contexts_match_expected: bool,
    normalized: NormalizedDurableState,
}

impl FrameworkVerification {
    fn passed(&self) -> bool {
        self.terminal_state_consistent
            && self.durable_partition_plan_matches_expected
            && self.durable_partition_contexts_match_expected
    }
}

#[derive(Debug, Serialize)]
struct BusinessVerification {
    source_rows: u64,
    destination_rows: u64,
    distinct_destination_ids: u64,
    missing_source_ids: u64,
    extra_destination_ids: u64,
    source_digest: String,
    expected_destination_digest: String,
    actual_destination_digest: String,
    range_ownership_complete: bool,
}

impl BusinessVerification {
    fn passed(&self, expected_rows: u64) -> bool {
        self.source_rows == expected_rows
            && self.destination_rows == expected_rows
            && self.distinct_destination_ids == expected_rows
            && self.missing_source_ids == 0
            && self.extra_destination_ids == 0
            && self.expected_destination_digest == self.actual_destination_digest
            && self.range_ownership_complete
    }
}

#[derive(Debug, Serialize)]
struct RunVerification {
    workers: u8,
    peak_active_workers: usize,
    active_workers_after_join: usize,
    launch_completed: bool,
    framework: FrameworkVerification,
    business: BusinessVerification,
}

impl RunVerification {
    fn passed(&self, expected_rows: u64) -> bool {
        self.launch_completed
            && self.peak_active_workers == usize::from(self.workers)
            && self.active_workers_after_join == 0
            && self.framework.passed()
            && self.business.passed(expected_rows)
    }
}

#[derive(Debug, Serialize)]
struct PoolCeilingProof {
    rejected_with_insufficient_pool_capacity: bool,
    observed_peak_workers: usize,
    job_instance_created: bool,
}

impl PoolCeilingProof {
    fn passed(&self) -> bool {
        self.rejected_with_insufficient_pool_capacity
            && self.observed_peak_workers == 0
            && !self.job_instance_created
    }
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Migrate => migrate(&cli.database_url).await?,
        Command::Seed {
            rows,
            seed: seed_value,
        } => seed(&cli.database_url, rows, seed_value).await?,
        Command::Run {
            run_name,
            rows,
            partitions,
            workers,
        } => {
            let verification =
                run_once(&cli.database_url, &run_name, rows, partitions, workers).await?;
            println!("{}", serde_json::to_string_pretty(&verification)?);
            if !verification.passed(rows) {
                bail!("run verification failed");
            }
        }
        Command::Verify {
            run_name,
            rows,
            partitions,
        } => {
            let verification =
                verify_existing(&cli.database_url, &run_name, rows, partitions).await?;
            println!("{}", serde_json::to_string_pretty(&verification)?);
            if !verification.framework.passed() || !verification.business.passed(rows) {
                bail!("verification failed");
            }
        }
        Command::Qualify {
            prefix,
            rows,
            partitions,
        } => qualify(&cli.database_url, &prefix, rows, partitions).await?,
        Command::Corrupt { run_name } => corrupt(&cli.database_url, &run_name).await?,
    }
    Ok(())
}

async fn migrate(url: &str) -> Result<()> {
    PostgresMigrator::migrate(&framework_config(url, 1)?).await?;
    let pool = app_pool(url, 1).await?;
    for statement in [
        "CREATE SCHEMA IF NOT EXISTS app_source",
        "CREATE SCHEMA IF NOT EXISTS app_business",
        "CREATE TABLE IF NOT EXISTS app_source.local_partition_source (\
         source_id bigint PRIMARY KEY, payload_value bigint NOT NULL)",
        "CREATE TABLE IF NOT EXISTS app_business.local_partition_projection (\
         run_name text NOT NULL, source_id bigint NOT NULL, projected_value bigint NOT NULL, \
         partition_key text NOT NULL, PRIMARY KEY (run_name, source_id))",
        "CREATE INDEX IF NOT EXISTS local_partition_projection_run_partition_idx \
         ON app_business.local_partition_projection (run_name, partition_key, source_id)",
    ] {
        sqlx::query(statement).execute(&pool).await?;
    }
    pool.close().await;
    Ok(())
}

async fn seed(url: &str, rows: u64, seed: i64) -> Result<()> {
    if rows == 0 {
        bail!("rows must be nonzero");
    }
    let rows_i64 = i64::try_from(rows)?;
    let pool = app_pool(url, 1).await?;
    sqlx::query(
        "TRUNCATE app_business.local_partition_projection, app_source.local_partition_source",
    )
    .execute(&pool)
    .await?;
    sqlx::query(
        "INSERT INTO app_source.local_partition_source (source_id, payload_value) \
         SELECT source_id, ((source_id * 48271 + $2) % 2147483647) \
         FROM generate_series(1, $1::bigint) AS generated(source_id) \
         ORDER BY source_id",
    )
    .bind(rows_i64)
    .bind(seed)
    .execute(&pool)
    .await?;
    let identity = source_identity(&pool).await?;
    pool.close().await;
    if identity.0 != rows {
        bail!("seeded {} rows, expected {rows}", identity.0);
    }
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "rows": identity.0,
            "seed": seed,
            "source_digest_sha256": identity.1,
        }))?
    );
    Ok(())
}

async fn qualify(url: &str, prefix: &str, rows: u64, partitions: u16) -> Result<()> {
    validate_shape(rows, partitions, 64)?;
    let identity_pool = app_pool(url, 1).await?;
    let (baseline_source_rows, baseline_source_digest) = source_identity(&identity_pool).await?;
    identity_pool.close().await;
    if baseline_source_rows != rows {
        bail!("source contains {baseline_source_rows} rows, expected {rows}");
    }

    let mut baseline: Option<NormalizedDurableState> = None;
    let mut points = Vec::new();

    for workers in DENSE_WORKER_POINTS {
        let run_name = format!("{prefix}-w{workers}");
        let verification = run_once(url, &run_name, rows, partitions, workers).await?;
        if !verification.passed(rows) {
            println!("{}", serde_json::to_string_pretty(&verification)?);
            bail!("dense qualification failed at {workers} workers");
        }
        if verification.business.source_digest != baseline_source_digest {
            bail!("source identity changed before or during the {workers}-worker point");
        }
        if let Some(expected) = &baseline {
            if &verification.framework.normalized != expected {
                bail!("normalized durable state diverged at {workers} workers");
            }
        } else {
            baseline = Some(verification.framework.normalized.clone());
        }
        points.push(json!({
            "workers": workers,
            "peak_active_workers": verification.peak_active_workers,
            "destination_rows": verification.business.destination_rows,
            "destination_digest_sha256": verification.business.actual_destination_digest,
            "durable_state_equivalent_to_one_worker": true,
        }));
    }

    let ceiling = prove_pool_ceiling(url, prefix, rows).await?;
    if !ceiling.passed() {
        println!("{}", serde_json::to_string_pretty(&ceiling)?);
        bail!("derived repository pool ceiling did not fail closed");
    }

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "campaign": 93,
            "validation_subject": "oxide-batch =0.6.0",
            "classification": "correctness qualification only; no performance claim",
            "rows": rows,
            "partitions": partitions,
            "worker_points": DENSE_WORKER_POINTS,
            "points": points,
            "pool_ceiling_proof": ceiling,
        }))?
    );
    Ok(())
}

async fn run_once(
    url: &str,
    run_name: &str,
    rows: u64,
    partitions: u16,
    workers: u8,
) -> Result<RunVerification> {
    let rows_per_partition = validate_shape(rows, partitions, workers)?;
    let identity_pool = app_pool(url, 1).await?;
    let (source_rows, source_digest) = source_identity(&identity_pool).await?;
    identity_pool.close().await;
    if source_rows != rows {
        bail!("source contains {source_rows} rows, expected {rows}");
    }

    let repository = PostgresJobRepository::connect(
        framework_config(url, pool_budget(workers))?,
        Arc::new(SystemClock),
    )
    .await?;
    let business = app_pool(url, BUSINESS_POOL_CONNECTIONS).await?;
    let occupancy = Arc::new(Occupancy::default());
    let gate = Arc::new(AdmissionGate::new(usize::from(workers)));
    let job_name = job_name(run_name)?;
    let job = build_job(
        job_name.clone(),
        run_name,
        rows,
        partitions,
        workers,
        &source_digest,
        business.clone(),
        Arc::clone(&occupancy),
        Arc::clone(&gate),
    )?;
    let ids = SequentialIdGenerator::new(NonZeroU64::MIN);
    let (_source, stop) = StopSource::new();
    let report = FlowLauncher::new(&repository, &SystemClock, &ids)
        .launch(&job, &JobParameters::new(), &stop)
        .await?;
    let launch_completed = report.outcome() == &FlowExecutionOutcome::Completed;
    business.close().await;
    repository.close().await?;

    let framework = verify_framework(url, &job_name, rows, partitions, &source_digest).await?;
    let business = verify_business(url, run_name, rows, partitions, rows_per_partition).await?;
    Ok(RunVerification {
        workers,
        peak_active_workers: occupancy.peak(),
        active_workers_after_join: occupancy.active(),
        launch_completed,
        framework,
        business,
    })
}

async fn verify_existing(
    url: &str,
    run_name: &str,
    rows: u64,
    partitions: u16,
) -> Result<RunVerification> {
    let rows_per_partition = validate_shape(rows, partitions, 1)?;
    let pool = app_pool(url, 1).await?;
    let (source_rows, source_digest) = source_identity(&pool).await?;
    pool.close().await;
    if source_rows != rows {
        bail!("source contains {source_rows} rows, expected {rows}");
    }
    let job_name = job_name(run_name)?;
    let framework = verify_framework(url, &job_name, rows, partitions, &source_digest).await?;
    let business = verify_business(url, run_name, rows, partitions, rows_per_partition).await?;
    Ok(RunVerification {
        workers: 0,
        peak_active_workers: 0,
        active_workers_after_join: 0,
        launch_completed: framework.terminal_state_consistent,
        framework,
        business,
    })
}

async fn verify_framework(
    url: &str,
    job_name: &JobName,
    rows: u64,
    partitions: u16,
    source_digest: &str,
) -> Result<FrameworkVerification> {
    let repository =
        PostgresJobRepository::connect(framework_config(url, 2)?, Arc::new(SystemClock)).await?;
    let parameters = JobParameters::new();
    let key = JobInstanceKey::new(job_name.clone(), &parameters);
    let mut unit = repository.begin().await?;
    let instance = unit
        .find_job_instance(&key)
        .await?
        .ok_or_else(|| WorkerFailure("durable job instance is missing".to_owned()))?;
    let execution = unit
        .job_executions(instance.id())
        .await?
        .into_iter()
        .last()
        .ok_or_else(|| WorkerFailure("durable job execution is missing".to_owned()))?;
    let steps = unit.step_executions(execution.id()).await?;
    let parent = steps
        .iter()
        .find(|step| step.step_name().as_str() == "partitioned")
        .ok_or_else(|| WorkerFailure("partition manager step is missing".to_owned()))?;
    let mut durable = unit.step_partition_plan(parent.id()).await?;
    unit.rollback().await?;
    repository.close().await?;

    durable.sort_by(|left, right| left.key().as_str().cmp(right.key().as_str()));
    let expected = assignments(rows, partitions)?;
    let mut plan_matches = durable.len() == expected.len();
    let mut contexts_match = durable.len() == expected.len();
    let mut normalized_partitions = Vec::with_capacity(durable.len());

    for (position, partition) in durable.iter().enumerate() {
        normalized_partitions.push((
            partition.key().as_str().to_owned(),
            partition.status().to_string(),
        ));
        let Some(expected_assignment) = expected.get(position) else {
            plan_matches = false;
            contexts_match = false;
            continue;
        };
        plan_matches &= partition.key().as_str() == expected_assignment.key
            && partition.ordinal() == u32::from(expected_assignment.index) + 1;
        let bytes = partition.context().to_json()?;
        let envelope: ContextEnvelope = serde_json::from_slice(&bytes)?;
        contexts_match &= envelope.payload.partition_index == expected_assignment.index
            && envelope.payload.range_start == expected_assignment.start
            && envelope.payload.range_end == expected_assignment.end
            && envelope.payload.source_digest == source_digest;
    }

    let all_partitions_completed = durable
        .iter()
        .all(|partition| partition.status() == BatchStatus::Completed);
    let terminal_state_consistent = execution.metadata().status() == BatchStatus::Completed
        && parent.metadata().status() == BatchStatus::Completed
        && all_partitions_completed;
    let normalized = NormalizedDurableState {
        job_status: execution.metadata().status().to_string(),
        parent_status: parent.metadata().status().to_string(),
        partitions: normalized_partitions,
    };
    Ok(FrameworkVerification {
        terminal_state_consistent,
        durable_partition_plan_matches_expected: plan_matches,
        durable_partition_contexts_match_expected: contexts_match,
        normalized,
    })
}

async fn verify_business(
    url: &str,
    run_name: &str,
    rows: u64,
    partitions: u16,
    rows_per_partition: u64,
) -> Result<BusinessVerification> {
    let pool = app_pool(url, 2).await?;
    let (source_rows, source_digest) = source_identity(&pool).await?;
    let (expected_rows, expected_digest) =
        expected_destination_identity(&pool, rows_per_partition).await?;
    let (destination_rows, actual_digest) = destination_identity(&pool, run_name).await?;
    let distinct_destination_ids: i64 = sqlx::query_scalar(
        "SELECT count(DISTINCT source_id) FROM app_business.local_partition_projection \
         WHERE run_name = $1",
    )
    .bind(run_name)
    .fetch_one(&pool)
    .await?;
    let missing_source_ids: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM app_source.local_partition_source source \
         WHERE NOT EXISTS (SELECT 1 FROM app_business.local_partition_projection destination \
         WHERE destination.run_name = $1 AND destination.source_id = source.source_id)",
    )
    .bind(run_name)
    .fetch_one(&pool)
    .await?;
    let extra_destination_ids: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM app_business.local_partition_projection destination \
         WHERE destination.run_name = $1 AND NOT EXISTS \
         (SELECT 1 FROM app_source.local_partition_source source \
         WHERE source.source_id = destination.source_id)",
    )
    .bind(run_name)
    .fetch_one(&pool)
    .await?;
    pool.close().await;

    let range_ownership_complete = expected_rows == rows
        && assignments(rows, partitions)?
            .iter()
            .enumerate()
            .all(|(index, assignment)| {
                assignment.index == u16::try_from(index).unwrap_or(u16::MAX)
                    && assignment.start
                        == u64::try_from(index)
                            .ok()
                            .and_then(|value| value.checked_mul(rows_per_partition))
                            .and_then(|value| value.checked_add(1))
                            .unwrap_or(0)
                    && assignment.end == assignment.start + rows_per_partition - 1
            });

    Ok(BusinessVerification {
        source_rows,
        destination_rows,
        distinct_destination_ids: u64::try_from(distinct_destination_ids)?,
        missing_source_ids: u64::try_from(missing_source_ids)?,
        extra_destination_ids: u64::try_from(extra_destination_ids)?,
        source_digest,
        expected_destination_digest: expected_digest,
        actual_destination_digest: actual_digest,
        range_ownership_complete,
    })
}

async fn prove_pool_ceiling(url: &str, prefix: &str, rows: u64) -> Result<PoolCeilingProof> {
    const WORKERS: u8 = 4;
    const PARTITIONS: u16 = 4;
    let rows_per_partition = validate_shape(rows, PARTITIONS, WORKERS)?;
    if rows_per_partition == 0 {
        bail!("pool ceiling proof requires nonempty partitions");
    }
    let identity_pool = app_pool(url, 1).await?;
    let (_, source_digest) = source_identity(&identity_pool).await?;
    identity_pool.close().await;

    let repository = PostgresJobRepository::connect(
        framework_config(url, pool_budget(WORKERS) - 1)?,
        Arc::new(SystemClock),
    )
    .await?;
    let business = app_pool(url, 1).await?;
    let occupancy = Arc::new(Occupancy::default());
    let gate = Arc::new(AdmissionGate::new(usize::from(WORKERS)));
    let run_name = format!("{prefix}-pool-ceiling");
    let name = job_name(&run_name)?;
    let job = build_job(
        name.clone(),
        &run_name,
        rows,
        PARTITIONS,
        WORKERS,
        &source_digest,
        business.clone(),
        Arc::clone(&occupancy),
        gate,
    )?;
    let ids = SequentialIdGenerator::new(NonZeroU64::MIN);
    let (_source, stop) = StopSource::new();
    let launched = FlowLauncher::new(&repository, &SystemClock, &ids)
        .launch(&job, &JobParameters::new(), &stop)
        .await;
    let rejected = matches!(
        launched,
        Err(FlowRuntimeError::InsufficientPoolCapacity { .. })
    );

    let parameters = JobParameters::new();
    let key = JobInstanceKey::new(name, &parameters);
    let mut unit = repository.begin().await?;
    let job_instance_created = unit.find_job_instance(&key).await?.is_some();
    unit.rollback().await?;
    business.close().await;
    repository.close().await?;
    Ok(PoolCeilingProof {
        rejected_with_insufficient_pool_capacity: rejected,
        observed_peak_workers: occupancy.peak(),
        job_instance_created,
    })
}

// This construction boundary intentionally keeps every restart-relevant and
// resource-relevant input explicit; hiding them in an unvalidated options bag
// would make the workload contract harder to audit.
#[allow(clippy::too_many_arguments)]
fn build_job(
    name: JobName,
    run_name: &str,
    rows: u64,
    partitions: u16,
    workers: u8,
    source_digest: &str,
    business: PgPool,
    occupancy: Arc<Occupancy>,
    gate: Arc<AdmissionGate>,
) -> Result<FlowJob> {
    let rows_per_partition = validate_shape(rows, partitions, workers)?;
    let manager = NodeId::new("partitioned")?;
    let worker_name = StepName::new("worker")?;
    let worker = StepNode::new(
        NodeId::new("worker")?,
        worker_name.clone(),
        StepComponents::Tasklet(ComponentRevision::new("campaign93-worker-v1")?),
    );
    let plan = FlowGraph::new(manager.clone())
        .with_node(FlowNode::partitioned_step(PartitionedStepNode::new(
            manager.clone(),
            StepName::new("partitioned")?,
            worker,
            ComponentRevision::new("campaign93-partitioner-v1")?,
            ComponentRevision::new("campaign93-aggregate-v1")?,
            PartitionCount::new(partitions)?,
            PartitionBudget::new(workers, pool_budget(workers))?,
        )))
        .with_sequence(
            manager.clone(),
            FlowTarget::Terminal(TerminalKind::Complete),
        )?
        .compile(&name, DefinitionRevision::new("campaign93-v1")?)?;

    let entries = assignments(rows, partitions)?
        .into_iter()
        .map(|assignment| partition_entry(&assignment, source_digest))
        .collect::<Result<Vec<_>>>()?;
    let partitioner = PartitionPlanFactory::new(move |request| {
        if request.partition_count().get() != partitions {
            return Err(PartitionFactoryError::Rejected);
        }
        Ok(entries.clone())
    });

    let factory_name = worker_name.clone();
    let run_name = run_name.to_owned();
    let source_digest = source_digest.to_owned();
    let factory = PartitionTaskletFactory::new(worker_name, move |input| {
        TaskletStep::new(
            factory_name.clone(),
            Arc::new(PartitionWorker {
                occupancy: Arc::clone(&occupancy),
                gate: Arc::clone(&gate),
                business: business.clone(),
                run_name: run_name.clone(),
                key: input.key().as_str().to_owned(),
                context_json: input.context().to_json().ok(),
                expected_source_digest: source_digest.clone(),
                rows_per_partition,
            }),
        )
    });
    Ok(FlowJob::new(name, plan)?.with_partitioned_tasklet(manager, partitioner, factory)?)
}

fn partition_entry(assignment: &Assignment, source_digest: &str) -> Result<PartitionPlanEntry> {
    let bytes = serde_json::to_vec(&json!({
        "format": "oxide-batch.execution-context",
        "format_version": 1,
        "schema": "workload.postgres-local-partition.assignment",
        "schema_version": 1,
        "payload": PartitionPayload {
            partition_index: assignment.index,
            range_start: assignment.start,
            range_end: assignment.end,
            source_digest: source_digest.to_owned(),
        },
    }))?;
    let context = ExecutionContext::from_json(&bytes, StateLimits::new(4 * 1024, 16)?)?;
    Ok(PartitionPlanEntry::new(
        PartitionKey::new(assignment.key.clone())?,
        context,
    )?)
}

fn assignments(rows: u64, partitions: u16) -> Result<Vec<Assignment>> {
    let rows_per_partition = validate_shape(rows, partitions, 1)?;
    (0..partitions)
        .map(|index| {
            let start = u64::from(index)
                .checked_mul(rows_per_partition)
                .and_then(|value| value.checked_add(1))
                .ok_or_else(|| WorkerFailure("partition range start overflowed".to_owned()))?;
            let end = start
                .checked_add(rows_per_partition - 1)
                .ok_or_else(|| WorkerFailure("partition range end overflowed".to_owned()))?;
            Ok(Assignment {
                index,
                key: partition_key(index),
                start,
                end,
            })
        })
        .collect::<std::result::Result<Vec<_>, WorkerFailure>>()
        .map_err(Into::into)
}

fn validate_shape(rows: u64, partitions: u16, workers: u8) -> Result<u64> {
    if rows == 0 || partitions == 0 || workers == 0 {
        bail!("rows, partitions, and workers must all be nonzero");
    }
    if u16::from(workers) > partitions {
        bail!("workers cannot exceed partitions");
    }
    if !rows.is_multiple_of(u64::from(partitions)) {
        bail!("rows must be exactly divisible by partitions");
    }
    let rows_per_partition = rows / u64::from(partitions);
    if rows_per_partition == 0 {
        bail!("every partition must own at least one row");
    }
    let _ = i64::try_from(rows)?;
    Ok(rows_per_partition)
}

fn partition_key(index: u16) -> String {
    format!("partition-{index:04}")
}

fn job_name(run_name: &str) -> Result<JobName> {
    Ok(JobName::new(format!("{JOB_PREFIX}-{run_name}"))?)
}

const fn pool_budget(workers: u8) -> u32 {
    workers as u32 + 1
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

async fn expected_destination_identity(
    pool: &PgPool,
    rows_per_partition: u64,
) -> Result<(u64, String)> {
    let mut stream = sqlx::query(
        "SELECT source_id, payload_value FROM app_source.local_partition_source ORDER BY source_id",
    )
    .fetch(pool);
    let mut count = 0_u64;
    let mut hasher = Sha256::new();
    while let Some(row) = stream.try_next().await? {
        let source_id: i64 = row.try_get("source_id")?;
        let payload_value: i64 = row.try_get("payload_value")?;
        let source_id_u64 = u64::try_from(source_id)?;
        let partition_index = u16::try_from((source_id_u64 - 1) / rows_per_partition)?;
        let key = partition_key(partition_index);
        let projected_value = payload_value
            .checked_mul(3)
            .and_then(|value| value.checked_add(7))
            .ok_or_else(|| WorkerFailure("projected value overflowed".to_owned()))?;
        hasher.update(format!("{source_id}|{projected_value}|{key}\n").as_bytes());
        count += 1;
    }
    Ok((count, finish_digest(hasher)))
}

async fn destination_identity(pool: &PgPool, run_name: &str) -> Result<(u64, String)> {
    let mut stream = sqlx::query(
        "SELECT source_id, projected_value, partition_key \
         FROM app_business.local_partition_projection \
         WHERE run_name = $1 ORDER BY source_id",
    )
    .bind(run_name)
    .fetch(pool);
    let mut count = 0_u64;
    let mut hasher = Sha256::new();
    while let Some(row) = stream.try_next().await? {
        let source_id: i64 = row.try_get("source_id")?;
        let projected_value: i64 = row.try_get("projected_value")?;
        let key: String = row.try_get("partition_key")?;
        hasher.update(format!("{source_id}|{projected_value}|{key}\n").as_bytes());
        count += 1;
    }
    Ok((count, finish_digest(hasher)))
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

async fn corrupt(url: &str, run_name: &str) -> Result<()> {
    let pool = app_pool(url, 1).await?;
    let changed = sqlx::query(
        "UPDATE app_business.local_partition_projection \
         SET projected_value = projected_value + 1 \
         WHERE run_name = $1 AND source_id = (\
         SELECT min(source_id) FROM app_business.local_partition_projection WHERE run_name = $1)",
    )
    .bind(run_name)
    .execute(&pool)
    .await?;
    pool.close().await;
    if changed.rows_affected() != 1 {
        bail!("negative control expected to corrupt exactly one row");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_ranges_cover_input_exactly() -> Result<()> {
        let ranges = assignments(512, 64)?;
        if ranges.len() != 64 {
            bail!("unexpected range count");
        }
        let mut next = 1_u64;
        for range in ranges {
            if range.start != next || range.end < range.start {
                bail!("range gap or overlap detected");
            }
            next = range.end + 1;
        }
        if next != 513 {
            bail!("range plan did not cover the whole input");
        }
        Ok(())
    }

    #[test]
    fn partition_keys_are_stable_and_sorted() -> Result<()> {
        let ranges = assignments(512, 64)?;
        let mut keys = ranges
            .iter()
            .map(|range| range.key.clone())
            .collect::<Vec<_>>();
        let original = keys.clone();
        keys.sort();
        if keys != original
            || keys.first().map(String::as_str) != Some("partition-0000")
            || keys.last().map(String::as_str) != Some("partition-0063")
        {
            bail!("partition keys are not canonical");
        }
        Ok(())
    }
}
