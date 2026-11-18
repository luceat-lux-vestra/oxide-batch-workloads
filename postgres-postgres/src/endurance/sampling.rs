use std::collections::VecDeque;
#[cfg(any(target_os = "linux", target_os = "android"))]
use std::fs;
use std::path::{Path, PathBuf};
#[cfg(any(target_os = "linux", target_os = "android"))]
use std::process::Command as StdCommand;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use serde_json::Value;
use sqlx::postgres::PgPoolOptions;
use sqlx::PgPool;
use tokio::task::JoinHandle;
use tokio::time::{sleep, Instant};

use super::evidence::{
    append_jsonl, execute_recorded_iteration_in_store, finalize_failure, finalize_success,
    now_unix_nanos, EvidenceStore, RecordedIterationOutput,
};
use super::{plan_iteration, EnduranceFailure, IterationObserver};

const SAMPLE_SCHEMA: &str = "oxide-batch-workloads.endurance-sample";
const SAMPLE_SCHEMA_VERSION: u32 = 1;
const MIN_SAMPLE_INTERVAL_MILLIS: u64 = 10;
const MAX_SAMPLE_INTERVAL_MILLIS: u64 = 60_000;
const SAMPLER_POLL_MILLIS: u64 = 5;

#[derive(Debug, Serialize)]
pub struct SampledRecordedIterationOutput {
    pub recorded: RecordedIterationOutput,
    pub samples_path: String,
    pub sample_interval_millis: u64,
    pub samples_written: u64,
}

#[derive(Clone, Debug, Serialize)]
pub struct ProcessObservation {
    pub pid: u32,
    pub role: String,
    pub rss_bytes: Option<u64>,
    pub pss_bytes: Option<u64>,
    pub smaps_rollup_rss_bytes: Option<u64>,
    pub private_clean_bytes: Option<u64>,
    pub private_dirty_bytes: Option<u64>,
    pub shared_clean_bytes: Option<u64>,
    pub shared_dirty_bytes: Option<u64>,
    pub swap_bytes: Option<u64>,
    pub minor_faults: Option<u64>,
    pub major_faults: Option<u64>,
    pub cpu_time_ticks: Option<u64>,
    pub voluntary_context_switches: Option<u64>,
    pub nonvoluntary_context_switches: Option<u64>,
    pub thread_count: Option<u64>,
    pub fd_count: Option<u64>,
    pub unavailable_reason: Option<String>,
    pub smaps_rollup_unavailable_reason: Option<String>,
    pub fd_count_unavailable_reason: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct FilesystemObservation {
    pub total_bytes: u64,
    pub used_bytes: u64,
    pub available_bytes: u64,
}

pub trait ResourceProbe: Send + Sync {
    fn process(&self, pid: u32, role: &str) -> Result<ProcessObservation>;
    fn filesystem(&self, path: &Path) -> Result<FilesystemObservation>;
}

#[derive(Debug, Default)]
pub struct LinuxResourceProbe;

impl ResourceProbe for LinuxResourceProbe {
    fn process(&self, pid: u32, role: &str) -> Result<ProcessObservation> {
        #[cfg(any(target_os = "linux", target_os = "android"))]
        {
            process_observation_linux(pid, role)
        }

        #[cfg(not(any(target_os = "linux", target_os = "android")))]
        {
            let _ = (pid, role);
            bail!("endurance resource process sampling requires Linux/Android procfs");
        }
    }

    fn filesystem(&self, path: &Path) -> Result<FilesystemObservation> {
        #[cfg(any(target_os = "linux", target_os = "android"))]
        {
            filesystem_observation_df(path)
        }

        #[cfg(not(any(target_os = "linux", target_os = "android")))]
        {
            let _ = path;
            bail!("endurance resource filesystem sampling requires Linux/Android");
        }
    }
}

#[derive(Debug, Serialize)]
struct PostgresObservation {
    total_connections: i64,
    active_connections: i64,
    database_size_bytes: i64,
}

#[derive(Debug, Serialize)]
struct MetadataObservation {
    job_instances: i64,
    job_executions: i64,
    step_executions: i64,
    job_instance_table_bytes: i64,
    job_execution_table_bytes: i64,
    step_execution_table_bytes: i64,
}

#[derive(Debug, Serialize)]
struct ResourceSampleRecord {
    schema: &'static str,
    schema_version: u32,
    timestamp_unix_nanos_utc: u64,
    campaign_id: String,
    sequence: u64,
    sample_kind: &'static str,
    sample_interval_millis: u64,
    process: Option<ProcessObservation>,
    postgres: PostgresObservation,
    oxide_batch: MetadataObservation,
    filesystem: FilesystemObservation,
}

#[derive(Clone, Debug)]
struct ProcessTarget {
    pid: u32,
    role: String,
}

#[derive(Debug)]
struct BoundaryRequest {
    kind: &'static str,
    process: Option<ProcessObservation>,
}

#[derive(Debug, Default)]
struct SamplerState {
    active: Option<ProcessTarget>,
    boundaries: VecDeque<BoundaryRequest>,
    failure: Option<String>,
    started: bool,
    stop: bool,
}

struct SamplingObserver {
    state: Arc<Mutex<SamplerState>>,
    probe: Arc<dyn ResourceProbe>,
    workload_role: &'static str,
}

impl SamplingObserver {
    fn new(
        state: Arc<Mutex<SamplerState>>,
        probe: Arc<dyn ResourceProbe>,
        failure: EnduranceFailure,
    ) -> Self {
        let workload_role = if failure == EnduranceFailure::Clean {
            "clean-workload"
        } else {
            "crash-target"
        };
        Self {
            state,
            probe,
            workload_role,
        }
    }

    fn enqueue_terminal(&self, success: bool) -> Result<()> {
        let mut state = lock_state(&self.state)?;
        fail_if_sampler_failed(&state)?;
        state.boundaries.push_back(BoundaryRequest {
            kind: if success {
                "terminal-success"
            } else {
                "terminal-failure"
            },
            process: None,
        });
        Ok(())
    }

    fn stop(&self) -> Result<()> {
        lock_state(&self.state)?.stop = true;
        Ok(())
    }

    fn set_process(&self, role: &'static str, detail: &Value) -> Result<()> {
        let pid_u64 = detail
            .get("pid")
            .and_then(Value::as_u64)
            .ok_or_else(|| anyhow!("resource sampler child-start event is missing numeric pid"))?;
        let pid = u32::try_from(pid_u64).context("resource sampler child pid does not fit u32")?;
        let process = self.probe.process(pid, role)?;

        let mut state = lock_state(&self.state)?;
        fail_if_sampler_failed(&state)?;
        state.active = Some(ProcessTarget {
            pid,
            role: role.to_owned(),
        });
        state.boundaries.push_back(BoundaryRequest {
            kind: "process-role-start",
            process: Some(process),
        });
        Ok(())
    }

    fn clear_process(&self) -> Result<()> {
        let mut state = lock_state(&self.state)?;
        fail_if_sampler_failed(&state)?;
        state.active = None;
        Ok(())
    }

    fn enqueue_current_boundary(&self, kind: &'static str) -> Result<()> {
        let target = {
            let state = lock_state(&self.state)?;
            fail_if_sampler_failed(&state)?;
            state.active.clone()
        };
        let process = target
            .as_ref()
            .map(|target| self.probe.process(target.pid, &target.role))
            .transpose()?;
        let mut state = lock_state(&self.state)?;
        fail_if_sampler_failed(&state)?;
        state
            .boundaries
            .push_back(BoundaryRequest { kind, process });
        Ok(())
    }

    fn enqueue_boundary(&self, kind: &'static str) -> Result<()> {
        let mut state = lock_state(&self.state)?;
        fail_if_sampler_failed(&state)?;
        if kind == "sampler-start" {
            state.started = true;
        }
        state.boundaries.push_back(BoundaryRequest {
            kind,
            process: None,
        });
        Ok(())
    }
}

impl IterationObserver for SamplingObserver {
    fn record(&mut self, phase: &'static str, detail: Value) -> Result<()> {
        {
            let state = lock_state(&self.state)?;
            fail_if_sampler_failed(&state)?;
        }

        match phase {
            "iteration-started" => self.enqueue_boundary("sampler-start"),
            "workload-child-started" => self.set_process(self.workload_role, &detail),
            "plain-relaunch-child-started" => self.set_process("plain-relaunch", &detail),
            "recovery-child-started" => self.set_process("recovery", &detail),
            "continuation-child-started" => self.set_process("continuation", &detail),
            "verification-child-started" => self.set_process("verifier", &detail),
            "crash-marker-observed" => self.enqueue_current_boundary("crash-marker-observed"),
            "post-crash-state-observed" => self.enqueue_boundary("post-crash-state"),
            "workload-child-completed"
            | "crash-child-reaped"
            | "plain-relaunch-child-completed"
            | "recovery-child-completed"
            | "continuation-child-completed"
            | "verification-child-completed" => self.clear_process(),
            "cleanup-completed" => self.enqueue_boundary("post-cleanup"),
            _ => Ok(()),
        }
    }
}

struct SamplerContext {
    state: Arc<Mutex<SamplerState>>,
    probe: Arc<dyn ResourceProbe>,
    samples_path: PathBuf,
    evidence_path: PathBuf,
    campaign_id: String,
    sequence: u64,
    interval: Duration,
    interval_millis: u64,
}

struct SamplerTask {
    handle: Option<JoinHandle<Result<u64>>>,
}

impl SamplerTask {
    fn spawn(context: SamplerContext, pool: PgPool) -> Self {
        Self {
            handle: Some(tokio::spawn(run_sampler(context, pool))),
        }
    }

    async fn join(&mut self) -> Result<u64> {
        let result = match self.handle.as_mut() {
            Some(handle) => match handle.await {
                Ok(result) => result,
                Err(error) => Err(anyhow!("resource sampler task join failed: {error}")),
            },
            None => Err(anyhow!("resource sampler task was already joined")),
        };
        self.handle = None;
        result
    }
}

impl Drop for SamplerTask {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            handle.abort();
        }
    }
}

pub async fn execute_sampled_recorded_iteration(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    sequence: u64,
    sample_interval: Duration,
) -> Result<SampledRecordedIterationOutput> {
    execute_sampled_recorded_iteration_with_probe(
        workload_binary,
        database_url,
        evidence_root,
        campaign_id,
        sequence,
        sample_interval,
        Arc::new(LinuxResourceProbe),
    )
    .await
}

pub async fn execute_sampled_recorded_iteration_with_probe(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    sequence: u64,
    sample_interval: Duration,
    probe: Arc<dyn ResourceProbe>,
) -> Result<SampledRecordedIterationOutput> {
    let interval_millis =
        u64::try_from(sample_interval.as_millis()).context("sample interval does not fit u64")?;
    if !(MIN_SAMPLE_INTERVAL_MILLIS..=MAX_SAMPLE_INTERVAL_MILLIS).contains(&interval_millis) {
        bail!(
            "sample interval must be between {MIN_SAMPLE_INTERVAL_MILLIS} and {MAX_SAMPLE_INTERVAL_MILLIS} milliseconds"
        );
    }

    let plan = plan_iteration(campaign_id, sequence)?;
    let store = EvidenceStore::open(evidence_root, campaign_id, workload_binary)?;
    let pool = match PgPoolOptions::new()
        .max_connections(1)
        .connect(database_url)
        .await
    {
        Ok(pool) => pool,
        Err(error) => {
            let error = anyhow!("resource sampler PostgreSQL connection failed: {error}");
            finalize_failure(&store, database_url, sequence, &error)?;
            return Err(error);
        }
    };

    let samples_path = store.samples_path();
    let state = Arc::new(Mutex::new(SamplerState::default()));
    let mut observer = SamplingObserver::new(state.clone(), probe.clone(), plan.failure);
    let context = SamplerContext {
        state: state.clone(),
        probe,
        samples_path: samples_path.clone(),
        evidence_path: store.campaign_dir().to_path_buf(),
        campaign_id: campaign_id.to_owned(),
        sequence,
        interval: sample_interval,
        interval_millis,
    };
    let mut sampler = SamplerTask::spawn(context, pool);

    let execution = execute_recorded_iteration_in_store(
        &store,
        workload_binary,
        database_url,
        campaign_id,
        sequence,
        Some(&mut observer),
    )
    .await;

    let terminal_result = observer.enqueue_terminal(execution.is_ok());
    let stop_result = observer.stop();
    let (samples_written, sampler_error) = match sampler.join().await {
        Ok(samples_written) => (Some(samples_written), None),
        Err(error) => (None, Some(error)),
    };

    if let Some(sampling_error) = terminal_result
        .err()
        .or(stop_result.err())
        .or(sampler_error)
    {
        let error = match execution {
            Ok(_) => sampling_error,
            Err(execution_error) => anyhow!(
                "resource sampler failed: {sampling_error:#}; bounded iteration also failed: {execution_error:#}"
            ),
        };
        finalize_failure(&store, database_url, sequence, &error)?;
        return Err(error);
    }

    let Some(samples_written) = samples_written else {
        let error = anyhow!("resource sampler completed without a sample-count result");
        finalize_failure(&store, database_url, sequence, &error)?;
        return Err(error);
    };

    match execution {
        Ok(execution) => {
            let recorded = finalize_success(&store, campaign_id, sequence, execution)?;
            Ok(SampledRecordedIterationOutput {
                recorded,
                samples_path: samples_path.display().to_string(),
                sample_interval_millis: interval_millis,
                samples_written,
            })
        }
        Err(error) => {
            finalize_failure(&store, database_url, sequence, &error)?;
            Err(error)
        }
    }
}

async fn run_sampler(context: SamplerContext, pool: PgPool) -> Result<u64> {
    let result = run_sampler_inner(&context, &pool).await;
    if let Err(error) = &result {
        if let Ok(mut state) = context.state.lock() {
            state.failure = Some(format!("{error:#}"));
            state.stop = true;
        }
    }
    pool.close().await;
    result
}

async fn run_sampler_inner(context: &SamplerContext, pool: &PgPool) -> Result<u64> {
    let mut samples_written = 0_u64;
    let mut next_periodic = Instant::now() + context.interval;

    loop {
        let (boundaries, active, started, stop) = {
            let mut state = lock_state(&context.state)?;
            let boundaries = state.boundaries.drain(..).collect::<Vec<_>>();
            (boundaries, state.active.clone(), state.started, state.stop)
        };

        for boundary in boundaries {
            collect_sample(context, pool, boundary.kind, boundary.process).await?;
            samples_written += 1;
        }

        let now = Instant::now();
        if started && !stop && now >= next_periodic {
            let process = active
                .as_ref()
                .map(|target| context.probe.process(target.pid, &target.role))
                .transpose()?;
            collect_sample(context, pool, "periodic", process).await?;
            samples_written += 1;
            next_periodic = Instant::now() + context.interval;
        }

        let should_stop = {
            let state = lock_state(&context.state)?;
            state.stop && state.boundaries.is_empty()
        };
        if should_stop {
            return Ok(samples_written);
        }

        let until_periodic = next_periodic.saturating_duration_since(Instant::now());
        let delay = if until_periodic.is_zero() {
            Duration::from_millis(1)
        } else {
            until_periodic.min(Duration::from_millis(SAMPLER_POLL_MILLIS))
        };
        sleep(delay).await;
    }
}

async fn collect_sample(
    context: &SamplerContext,
    pool: &PgPool,
    kind: &'static str,
    process: Option<ProcessObservation>,
) -> Result<()> {
    let postgres = observe_postgres(pool).await?;
    let oxide_batch = observe_metadata(pool).await?;
    let filesystem = context.probe.filesystem(&context.evidence_path)?;

    let record = ResourceSampleRecord {
        schema: SAMPLE_SCHEMA,
        schema_version: SAMPLE_SCHEMA_VERSION,
        timestamp_unix_nanos_utc: now_unix_nanos()?,
        campaign_id: context.campaign_id.clone(),
        sequence: context.sequence,
        sample_kind: kind,
        sample_interval_millis: context.interval_millis,
        process,
        postgres,
        oxide_batch,
        filesystem,
    };
    append_jsonl(&context.samples_path, &record)
        .with_context(|| format!("append {}", context.samples_path.display()))
}

async fn observe_postgres(pool: &PgPool) -> Result<PostgresObservation> {
    let (total_connections, active_connections, database_size_bytes) =
        sqlx::query_as::<_, (i64, i64, i64)>(
            "SELECT \
             (SELECT COUNT(*)::bigint FROM pg_stat_activity WHERE datname = current_database()), \
             (SELECT COUNT(*)::bigint FROM pg_stat_activity \
              WHERE datname = current_database() AND state = 'active'), \
             pg_database_size(current_database())::bigint",
        )
        .fetch_one(pool)
        .await
        .context("observe PostgreSQL connection/database size")?;

    Ok(PostgresObservation {
        total_connections,
        active_connections,
        database_size_bytes,
    })
}

async fn observe_metadata(pool: &PgPool) -> Result<MetadataObservation> {
    let (
        job_instances,
        job_executions,
        step_executions,
        job_instance_table_bytes,
        job_execution_table_bytes,
        step_execution_table_bytes,
    ) = sqlx::query_as::<_, (i64, i64, i64, i64, i64, i64)>(
        "SELECT \
         (SELECT COUNT(*)::bigint FROM oxide_batch.ob_job_instance), \
         (SELECT COUNT(*)::bigint FROM oxide_batch.ob_job_execution), \
         (SELECT COUNT(*)::bigint FROM oxide_batch.ob_step_execution), \
         pg_total_relation_size('oxide_batch.ob_job_instance'::regclass)::bigint, \
         pg_total_relation_size('oxide_batch.ob_job_execution'::regclass)::bigint, \
         pg_total_relation_size('oxide_batch.ob_step_execution'::regclass)::bigint",
    )
    .fetch_one(pool)
    .await
    .context("observe OxideBatch metadata growth")?;

    Ok(MetadataObservation {
        job_instances,
        job_executions,
        step_executions,
        job_instance_table_bytes,
        job_execution_table_bytes,
        step_execution_table_bytes,
    })
}

fn lock_state(state: &Arc<Mutex<SamplerState>>) -> Result<MutexGuard<'_, SamplerState>> {
    state
        .lock()
        .map_err(|_| anyhow!("resource sampler state mutex poisoned"))
}

fn fail_if_sampler_failed(state: &SamplerState) -> Result<()> {
    if let Some(failure) = &state.failure {
        bail!("resource sampler failed: {failure}");
    }
    Ok(())
}

#[cfg(any(target_os = "linux", target_os = "android"))]
#[derive(Debug, Default)]
struct StatusMetrics {
    rss_bytes: Option<u64>,
    voluntary_context_switches: Option<u64>,
    nonvoluntary_context_switches: Option<u64>,
    thread_count: Option<u64>,
}

#[cfg(any(target_os = "linux", target_os = "android"))]
#[derive(Debug)]
struct StatMetrics {
    minor_faults: u64,
    major_faults: u64,
    cpu_time_ticks: u64,
}

#[cfg(any(target_os = "linux", target_os = "android"))]
#[derive(Debug, Default)]
struct SmapsRollupMetrics {
    rss_bytes: Option<u64>,
    pss_bytes: Option<u64>,
    private_clean_bytes: Option<u64>,
    private_dirty_bytes: Option<u64>,
    shared_clean_bytes: Option<u64>,
    shared_dirty_bytes: Option<u64>,
    swap_bytes: Option<u64>,
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn process_observation_linux(pid: u32, role: &str) -> Result<ProcessObservation> {
    let proc_root = PathBuf::from(format!("/proc/{pid}"));
    let status_path = proc_root.join("status");
    let stat_path = proc_root.join("stat");
    let smaps_rollup_path = proc_root.join("smaps_rollup");
    let fd_path = proc_root.join("fd");

    let status = match fs::read_to_string(&status_path) {
        Ok(status) => status,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(ProcessObservation {
                pid,
                role: role.to_owned(),
                rss_bytes: None,
                pss_bytes: None,
                smaps_rollup_rss_bytes: None,
                private_clean_bytes: None,
                private_dirty_bytes: None,
                shared_clean_bytes: None,
                shared_dirty_bytes: None,
                swap_bytes: None,
                minor_faults: None,
                major_faults: None,
                cpu_time_ticks: None,
                voluntary_context_switches: None,
                nonvoluntary_context_switches: None,
                thread_count: None,
                fd_count: None,
                unavailable_reason: Some("process-exited-before-sample".to_owned()),
                smaps_rollup_unavailable_reason: None,
                fd_count_unavailable_reason: None,
            });
        }
        Err(error) => {
            return Err(error)
                .with_context(|| format!("read process status {}", status_path.display()));
        }
    };

    let status_metrics = parse_status_metrics(&status)?;
    let (stat_metrics, stat_missing) = match fs::read_to_string(&stat_path) {
        Ok(stat) => (Some(parse_stat_metrics(&stat)?), false),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (None, true),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("read process stat {}", stat_path.display()));
        }
    };

    let (smaps, smaps_rollup_unavailable_reason) = match fs::read_to_string(&smaps_rollup_path) {
        Ok(smaps) => (parse_smaps_rollup(&smaps)?, None),
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::PermissionDenied
            ) =>
        {
            (
                SmapsRollupMetrics::default(),
                Some(match error.kind() {
                    std::io::ErrorKind::PermissionDenied => {
                        "smaps-rollup-permission-denied".to_owned()
                    }
                    _ => "smaps-rollup-unavailable".to_owned(),
                }),
            )
        }
        Err(error) => {
            return Err(error).with_context(|| {
                format!("read process smaps_rollup {}", smaps_rollup_path.display())
            });
        }
    };

    let (fd_count, fd_count_unavailable_reason) = match fs::read_dir(&fd_path) {
        Ok(entries) => {
            let mut count = 0_u64;
            for entry in entries {
                entry.with_context(|| format!("read process fd entry {}", fd_path.display()))?;
                count = count
                    .checked_add(1)
                    .ok_or_else(|| anyhow!("process fd count overflow"))?;
            }
            (Some(count), None)
        }
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::PermissionDenied
            ) =>
        {
            (
                None,
                Some(match error.kind() {
                    std::io::ErrorKind::PermissionDenied => {
                        "fd-directory-permission-denied".to_owned()
                    }
                    _ => "process-exited-before-fd-sample".to_owned(),
                }),
            )
        }
        Err(error) => {
            return Err(error)
                .with_context(|| format!("read process fd directory {}", fd_path.display()));
        }
    };

    let unavailable_reason = match (status_metrics.rss_bytes.is_none(), stat_missing) {
        (false, false) => None,
        (true, false) => Some("rss-unavailable".to_owned()),
        (false, true) => Some("process-exited-before-stat-sample".to_owned()),
        (true, true) => Some("rss-and-stat-unavailable".to_owned()),
    };

    Ok(ProcessObservation {
        pid,
        role: role.to_owned(),
        rss_bytes: status_metrics.rss_bytes,
        pss_bytes: smaps.pss_bytes,
        smaps_rollup_rss_bytes: smaps.rss_bytes,
        private_clean_bytes: smaps.private_clean_bytes,
        private_dirty_bytes: smaps.private_dirty_bytes,
        shared_clean_bytes: smaps.shared_clean_bytes,
        shared_dirty_bytes: smaps.shared_dirty_bytes,
        swap_bytes: smaps.swap_bytes,
        minor_faults: stat_metrics.as_ref().map(|metrics| metrics.minor_faults),
        major_faults: stat_metrics.as_ref().map(|metrics| metrics.major_faults),
        cpu_time_ticks: stat_metrics.as_ref().map(|metrics| metrics.cpu_time_ticks),
        voluntary_context_switches: status_metrics.voluntary_context_switches,
        nonvoluntary_context_switches: status_metrics.nonvoluntary_context_switches,
        thread_count: status_metrics.thread_count,
        fd_count,
        unavailable_reason,
        smaps_rollup_unavailable_reason,
        fd_count_unavailable_reason,
    })
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn parse_status_metrics(status: &str) -> Result<StatusMetrics> {
    Ok(StatusMetrics {
        rss_bytes: parse_proc_kib_field(status, "VmRSS:")?,
        voluntary_context_switches: parse_proc_u64_field(status, "voluntary_ctxt_switches:")?,
        nonvoluntary_context_switches: parse_proc_u64_field(status, "nonvoluntary_ctxt_switches:")?,
        thread_count: parse_proc_u64_field(status, "Threads:")?,
    })
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn parse_stat_metrics(stat: &str) -> Result<StatMetrics> {
    let close = stat
        .rfind(')')
        .ok_or_else(|| anyhow!("proc stat is missing command closing parenthesis"))?;
    let fields = stat
        .get(close + 2..)
        .ok_or_else(|| anyhow!("proc stat is truncated after command"))?
        .split_whitespace()
        .collect::<Vec<_>>();
    if fields.len() <= 12 {
        bail!("proc stat does not contain fault/utime/stime fields");
    }

    let minor_faults = fields[7].parse::<u64>().context("parse proc minflt")?;
    let major_faults = fields[9].parse::<u64>().context("parse proc majflt")?;
    let user_ticks = fields[11]
        .parse::<u64>()
        .context("parse proc utime ticks")?;
    let system_ticks = fields[12]
        .parse::<u64>()
        .context("parse proc stime ticks")?;
    let cpu_time_ticks = user_ticks
        .checked_add(system_ticks)
        .ok_or_else(|| anyhow!("process CPU tick sum overflow"))?;

    Ok(StatMetrics {
        minor_faults,
        major_faults,
        cpu_time_ticks,
    })
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn parse_smaps_rollup(smaps: &str) -> Result<SmapsRollupMetrics> {
    Ok(SmapsRollupMetrics {
        rss_bytes: parse_proc_kib_field(smaps, "Rss:")?,
        pss_bytes: parse_proc_kib_field(smaps, "Pss:")?,
        private_clean_bytes: parse_proc_kib_field(smaps, "Private_Clean:")?,
        private_dirty_bytes: parse_proc_kib_field(smaps, "Private_Dirty:")?,
        shared_clean_bytes: parse_proc_kib_field(smaps, "Shared_Clean:")?,
        shared_dirty_bytes: parse_proc_kib_field(smaps, "Shared_Dirty:")?,
        swap_bytes: parse_proc_kib_field(smaps, "Swap:")?,
    })
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn parse_proc_kib_field(text: &str, field: &str) -> Result<Option<u64>> {
    let Some(value) = proc_field_value(text, field) else {
        return Ok(None);
    };
    let mut fields = value.split_whitespace();
    let kib = fields
        .next()
        .ok_or_else(|| anyhow!("{field} line is missing numeric value"))?
        .parse::<u64>()
        .with_context(|| format!("parse {field} KiB"))?;
    let unit = fields.next();
    if unit.is_some_and(|unit| unit != "kB") {
        bail!("{field} line has unexpected unit {unit:?}");
    }
    kib.checked_mul(1024)
        .map(Some)
        .ok_or_else(|| anyhow!("{field} byte conversion overflow"))
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn parse_proc_u64_field(text: &str, field: &str) -> Result<Option<u64>> {
    let Some(value) = proc_field_value(text, field) else {
        return Ok(None);
    };
    let raw = value
        .split_whitespace()
        .next()
        .ok_or_else(|| anyhow!("{field} line is missing numeric value"))?;
    raw.parse::<u64>()
        .map(Some)
        .with_context(|| format!("parse {field} value"))
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn proc_field_value<'a>(text: &'a str, field: &str) -> Option<&'a str> {
    text.lines()
        .find_map(|line| line.strip_prefix(field).map(str::trim))
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn filesystem_observation_df(path: &Path) -> Result<FilesystemObservation> {
    let output = StdCommand::new("df")
        .arg("-Pk")
        .arg(path)
        .output()
        .with_context(|| format!("run df for evidence path {}", path.display()))?;
    if !output.status.success() {
        bail!(
            "df failed for evidence path {}: status={} stderr={}",
            path.display(),
            output.status,
            String::from_utf8_lossy(&output.stderr)
        );
    }

    let stdout = String::from_utf8(output.stdout).context("df output is not UTF-8")?;
    let line = stdout
        .lines()
        .rfind(|line| !line.trim().is_empty())
        .ok_or_else(|| anyhow!("df returned no data row"))?;
    let columns = line.split_whitespace().collect::<Vec<_>>();
    if columns.len() < 6 {
        bail!("df data row has fewer than six columns: {line}");
    }

    let total_kib = columns[1].parse::<u64>().context("parse df total KiB")?;
    let used_kib = columns[2].parse::<u64>().context("parse df used KiB")?;
    let available_kib = columns[3]
        .parse::<u64>()
        .context("parse df available KiB")?;

    Ok(FilesystemObservation {
        total_bytes: kib_to_bytes(total_kib)?,
        used_bytes: kib_to_bytes(used_kib)?,
        available_bytes: kib_to_bytes(available_kib)?,
    })
}

#[cfg(any(target_os = "linux", target_os = "android"))]
fn kib_to_bytes(value: u64) -> Result<u64> {
    value
        .checked_mul(1024)
        .ok_or_else(|| anyhow!("filesystem KiB-to-byte conversion overflow"))
}

#[cfg(all(test, any(target_os = "linux", target_os = "android")))]
mod procfs_parser_tests {
    use super::*;

    #[test]
    fn parses_status_resource_fields() {
        let status = "\
VmRSS:\t123 kB
Threads:\t5
voluntary_ctxt_switches:\t7
nonvoluntary_ctxt_switches:\t11
";
        let metrics = parse_status_metrics(status).expect("parse status metrics");
        assert_eq!(metrics.rss_bytes, Some(123 * 1024));
        assert_eq!(metrics.thread_count, Some(5));
        assert_eq!(metrics.voluntary_context_switches, Some(7));
        assert_eq!(metrics.nonvoluntary_context_switches, Some(11));
    }

    #[test]
    fn parses_stat_faults_and_cpu_ticks_with_spaces_in_comm() {
        let stat = "123 (worker process) S 1 2 3 4 5 6 7 8 9 10 11 12 13";
        let metrics = parse_stat_metrics(stat).expect("parse stat metrics");
        assert_eq!(metrics.minor_faults, 7);
        assert_eq!(metrics.major_faults, 9);
        assert_eq!(metrics.cpu_time_ticks, 23);
    }

    #[test]
    fn parses_smaps_rollup_byte_fields() {
        let smaps = "\
Rss:                 100 kB
Pss:                  80 kB
Shared_Clean:         10 kB
Shared_Dirty:          2 kB
Private_Clean:         3 kB
Private_Dirty:        65 kB
Swap:                  4 kB
";
        let metrics = parse_smaps_rollup(smaps).expect("parse smaps_rollup metrics");
        assert_eq!(metrics.rss_bytes, Some(100 * 1024));
        assert_eq!(metrics.pss_bytes, Some(80 * 1024));
        assert_eq!(metrics.shared_clean_bytes, Some(10 * 1024));
        assert_eq!(metrics.shared_dirty_bytes, Some(2 * 1024));
        assert_eq!(metrics.private_clean_bytes, Some(3 * 1024));
        assert_eq!(metrics.private_dirty_bytes, Some(65 * 1024));
        assert_eq!(metrics.swap_bytes, Some(4 * 1024));
    }

    #[test]
    fn missing_optional_procfs_field_stays_none() {
        let metrics = parse_status_metrics("Threads:\t2\n").expect("parse sparse status");
        assert_eq!(metrics.rss_bytes, None);
        assert_eq!(metrics.thread_count, Some(2));
        assert_eq!(metrics.voluntary_context_switches, None);
        assert_eq!(metrics.nonvoluntary_context_switches, None);
    }
}
