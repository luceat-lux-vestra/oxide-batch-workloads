use std::path::{Path, PathBuf};
use std::process::{ExitStatus, Output, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use serde::Serialize;
use sqlx::postgres::PgPoolOptions;
use tokio::process::{Child, Command};
use tokio::time::{sleep, Instant};

use super::{
    cleanup_business, plan_iteration, ChildProcessResult, EnduranceFailure, EnduranceReader,
    IterationPlan, CANONICAL_CHUNK_SIZE, CANONICAL_CURSOR_FETCH_SIZE, CANONICAL_PAGING_PAGE_SIZE,
};
use crate::source_digest;

pub const CANONICAL_CRASH_CHUNK: u32 = 3;

const MARKER_TIMEOUT: Duration = Duration::from_secs(30);
const CRASH_STATE_SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct CrashProcessResult {
    pub pid: u32,
    pub marker_pid: u32,
    pub marker_boundary: String,
    pub success: bool,
    pub code: Option<i32>,
    pub signal: Option<i32>,
    pub status: String,
    pub reaped: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct CrashStateSnapshot {
    pub execution_status: String,
    pub execution_exit_code: String,
    pub commit_count: i64,
    pub checkpoint_position: i64,
    pub business_rows: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct CrashIterationResult {
    pub campaign_id: String,
    pub sequence: u64,
    pub reader: EnduranceReader,
    pub failure: EnduranceFailure,
    pub import_name: String,
    pub chunk_size: u32,
    pub fetch_size: Option<usize>,
    pub page_size: Option<usize>,
    pub fail_at_chunk: u32,
    pub crash: CrashProcessResult,
    pub post_crash: CrashStateSnapshot,
    pub plain_relaunch: ChildProcessResult,
    pub recovery: ChildProcessResult,
    pub continuation: ChildProcessResult,
    pub verification: ChildProcessResult,
    pub cleanup_deleted_rows: u64,
}

#[derive(Debug)]
struct PauseMarker {
    pid: u32,
    boundary: String,
}

pub async fn execute_crash_iteration(
    workload_binary: &Path,
    database_url: &str,
    campaign_id: &str,
    sequence: u64,
) -> Result<CrashIterationResult> {
    #[cfg(not(unix))]
    {
        let _ = (workload_binary, database_url, campaign_id, sequence);
        bail!("endurance crash orchestration requires Unix SIGKILL semantics");
    }

    #[cfg(unix)]
    {
        execute_crash_iteration_unix(workload_binary, database_url, campaign_id, sequence).await
    }
}

#[cfg(unix)]
async fn execute_crash_iteration_unix(
    workload_binary: &Path,
    database_url: &str,
    campaign_id: &str,
    sequence: u64,
) -> Result<CrashIterationResult> {
    let plan = plan_iteration(campaign_id, sequence)?;
    let (failure_mode, expected_marker_boundary, expected_commits) =
        crash_contract(plan.failure, sequence)?;
    let (fetch_size, page_size) = reader_sizes(plan.reader);

    let marker_path = marker_path(campaign_id, sequence)?;
    let _ = std::fs::remove_file(&marker_path);

    let mut crash_command = build_run_command(workload_binary, database_url, &plan);
    crash_command
        .arg("--fail-at-chunk")
        .arg(CANONICAL_CRASH_CHUNK.to_string())
        .arg("--failure-mode")
        .arg(failure_mode)
        .arg("--pause-for-kill")
        .arg(&marker_path)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    let mut crash_child = crash_command
        .spawn()
        .context("spawn postgres-postgres crash child")?;
    let crash_pid = crash_child
        .id()
        .ok_or_else(|| anyhow::anyhow!("postgres-postgres crash child has no process id"))?;

    let marker = match wait_for_pause_marker(
        &mut crash_child,
        &marker_path,
        expected_marker_boundary,
        MARKER_TIMEOUT,
    )
    .await
    {
        Ok(marker) => marker,
        Err(error) => {
            terminate_for_error(&mut crash_child).await;
            let _ = std::fs::remove_file(&marker_path);
            return Err(error);
        }
    };

    if marker.pid != crash_pid {
        terminate_for_error(&mut crash_child).await;
        let _ = std::fs::remove_file(&marker_path);
        bail!(
            "pause marker pid {} does not match spawned crash child pid {}",
            marker.pid,
            crash_pid
        );
    }

    crash_child
        .start_kill()
        .context("deliver external SIGKILL to paused workload child")?;
    let crash_output = crash_child
        .wait_with_output()
        .await
        .context("reap externally killed workload child")?;
    let _ = std::fs::remove_file(&marker_path);

    let crash = CrashProcessResult {
        pid: crash_pid,
        marker_pid: marker.pid,
        marker_boundary: marker.boundary,
        success: crash_output.status.success(),
        code: crash_output.status.code(),
        signal: exit_signal(&crash_output.status),
        status: crash_output.status.to_string(),
        reaped: true,
    };
    require_sigkill(&crash, &crash_output)?;

    let expected_rows = i64::from(expected_commits) * i64::from(CANONICAL_CHUNK_SIZE);
    let post_crash = observe_crash_state(database_url, &plan.import_name, expected_rows).await?;
    require_crash_state(&post_crash, expected_commits, expected_rows)?;

    let (plain_relaunch, plain_output) = run_child(
        build_run_command(workload_binary, database_url, &plan),
        "plain relaunch",
    )
    .await?;
    require_failure("plain relaunch", &plain_relaunch, &plain_output)?;

    let (recovery, recovery_output) = run_child(
        build_recover_command(workload_binary, database_url, &plan),
        "recover",
    )
    .await?;
    require_success("recover", &recovery, &recovery_output)?;

    let (continuation, continuation_output) = run_child(
        build_run_command(workload_binary, database_url, &plan),
        "continuation",
    )
    .await?;
    require_success("continuation", &continuation, &continuation_output)?;

    let (verification, verify_output) = run_child(
        build_verify_command(workload_binary, database_url, &plan),
        "verify",
    )
    .await?;
    require_success("verify", &verification, &verify_output)?;

    let cleanup_deleted_rows = cleanup_business(database_url, &plan.import_name).await?;

    Ok(CrashIterationResult {
        campaign_id: plan.campaign_id,
        sequence: plan.sequence,
        reader: plan.reader,
        failure: plan.failure,
        import_name: plan.import_name,
        chunk_size: CANONICAL_CHUNK_SIZE,
        fetch_size,
        page_size,
        fail_at_chunk: CANONICAL_CRASH_CHUNK,
        crash,
        post_crash,
        plain_relaunch,
        recovery,
        continuation,
        verification,
        cleanup_deleted_rows,
    })
}

fn crash_contract(
    failure: EnduranceFailure,
    sequence: u64,
) -> Result<(&'static str, &'static str, u32)> {
    match failure {
        EnduranceFailure::BeforeCommit => Ok((
            "during-write",
            "writer during-write",
            CANONICAL_CRASH_CHUNK - 1,
        )),
        EnduranceFailure::AfterCommit => Ok((
            "after-business-commit",
            "after-business-commit",
            CANONICAL_CRASH_CHUNK,
        )),
        EnduranceFailure::Clean => bail!(
            "sequence {sequence} is clean and cannot run through the crash/recovery Slice C path"
        ),
    }
}

fn reader_sizes(reader: EnduranceReader) -> (Option<usize>, Option<usize>) {
    match reader {
        EnduranceReader::Cursor => (Some(CANONICAL_CURSOR_FETCH_SIZE), None),
        EnduranceReader::Paging => (None, Some(CANONICAL_PAGING_PAGE_SIZE)),
    }
}

fn build_run_command(workload_binary: &Path, database_url: &str, plan: &IterationPlan) -> Command {
    let mut command = Command::new(workload_binary);
    command
        .arg("run")
        .arg("--database-url")
        .arg(database_url)
        .arg("--import-name")
        .arg(&plan.import_name)
        .arg("--chunk-size")
        .arg(CANONICAL_CHUNK_SIZE.to_string())
        .arg("--reader")
        .arg(plan.reader.as_str());

    match plan.reader {
        EnduranceReader::Cursor => {
            command
                .arg("--fetch-size")
                .arg(CANONICAL_CURSOR_FETCH_SIZE.to_string());
        }
        EnduranceReader::Paging => {
            command
                .arg("--page-size")
                .arg(CANONICAL_PAGING_PAGE_SIZE.to_string());
        }
    }

    command
}

fn build_recover_command(
    workload_binary: &Path,
    database_url: &str,
    plan: &IterationPlan,
) -> Command {
    let mut command = Command::new(workload_binary);
    command
        .arg("recover")
        .arg("--database-url")
        .arg(database_url)
        .arg("--import-name")
        .arg(&plan.import_name)
        .arg("--reader")
        .arg(plan.reader.as_str());
    command
}

fn build_verify_command(
    workload_binary: &Path,
    database_url: &str,
    plan: &IterationPlan,
) -> Command {
    let mut command = Command::new(workload_binary);
    command
        .arg("verify")
        .arg("--database-url")
        .arg(database_url)
        .arg("--import-name")
        .arg(&plan.import_name);
    command
}

async fn run_child(mut command: Command, phase: &str) -> Result<(ChildProcessResult, Output)> {
    command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    let child = command
        .spawn()
        .with_context(|| format!("spawn postgres-postgres {phase} child"))?;
    let pid = child
        .id()
        .ok_or_else(|| anyhow::anyhow!("postgres-postgres {phase} child has no process id"))?;
    let output = child
        .wait_with_output()
        .await
        .with_context(|| format!("wait for postgres-postgres {phase} child"))?;
    let process = ChildProcessResult {
        pid,
        success: output.status.success(),
        code: output.status.code(),
        status: output.status.to_string(),
    };
    Ok((process, output))
}

fn require_success(phase: &str, process: &ChildProcessResult, output: &Output) -> Result<()> {
    if process.success {
        return Ok(());
    }

    bail!(
        "{phase} child failed: pid={} status={} code={:?}\nstdout={}\nstderr={}",
        process.pid,
        process.status,
        process.code,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}

fn require_failure(phase: &str, process: &ChildProcessResult, output: &Output) -> Result<()> {
    if !process.success {
        return Ok(());
    }

    bail!(
        "{phase} unexpectedly succeeded: pid={} status={} code={:?}\nstdout={}\nstderr={}",
        process.pid,
        process.status,
        process.code,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}

#[cfg(unix)]
fn require_sigkill(process: &CrashProcessResult, output: &Output) -> Result<()> {
    if !process.success && process.signal == Some(9) {
        return Ok(());
    }

    bail!(
        "crash child was not terminated by external SIGKILL: pid={} status={} code={:?} signal={:?}\nstdout={}\nstderr={}",
        process.pid,
        process.status,
        process.code,
        process.signal,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}

#[cfg(unix)]
fn exit_signal(status: &ExitStatus) -> Option<i32> {
    use std::os::unix::process::ExitStatusExt;
    status.signal()
}

async fn wait_for_pause_marker(
    child: &mut Child,
    marker_path: &Path,
    expected_boundary: &str,
    timeout: Duration,
) -> Result<PauseMarker> {
    let deadline = Instant::now() + timeout;

    loop {
        match tokio::fs::read_to_string(marker_path).await {
            Ok(contents) => {
                let mut lines = contents.lines();
                if let (Some(pid_text), Some(boundary)) = (lines.next(), lines.next()) {
                    if let Ok(pid) = pid_text.trim().parse::<u32>() {
                        if boundary != expected_boundary {
                            bail!(
                                "pause marker boundary mismatch: expected {expected_boundary}, got {boundary}"
                            );
                        }
                        return Ok(PauseMarker {
                            pid,
                            boundary: boundary.to_owned(),
                        });
                    }
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("read pause marker {}", marker_path.display()));
            }
        }

        if let Some(status) = child
            .try_wait()
            .context("poll crash child while waiting for pause marker")?
        {
            bail!("crash child exited before semantic pause marker appeared: status={status}");
        }
        if Instant::now() >= deadline {
            bail!(
                "pause marker {} did not appear within {timeout:?}",
                marker_path.display()
            );
        }

        sleep(Duration::from_millis(20)).await;
    }
}

async fn terminate_for_error(child: &mut Child) {
    let _ = child.start_kill();
    let _ = child.wait().await;
}

fn marker_path(campaign_id: &str, sequence: u64) -> Result<PathBuf> {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before UNIX epoch")?
        .as_nanos();
    Ok(std::env::temp_dir().join(format!(
        "postgres-postgres-endurance-{campaign_id}-{sequence}-{}-{nonce}.marker",
        std::process::id()
    )))
}

async fn observe_crash_state(
    database_url: &str,
    import_name: &str,
    expected_business_rows: i64,
) -> Result<CrashStateSnapshot> {
    let pool = PgPoolOptions::new().connect(database_url).await?;
    let source_digest = source_digest::compute(&pool).await?;
    let business_rows = wait_for_business_rows(
        &pool,
        import_name,
        &source_digest,
        expected_business_rows,
        CRASH_STATE_SETTLE_TIMEOUT,
    )
    .await?;

    let row = sqlx::query_as::<_, (String, String, i64, i64)>(
        "SELECT e.status, e.exit_code, s.commit_count, \
         (s.checkpoint_payload->>'position')::bigint \
         FROM oxide_batch.ob_job_execution e \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         JOIN oxide_batch.ob_step_execution s ON s.job_execution_id = e.id \
         WHERE i.job_name = $1 \
         ORDER BY e.attempt DESC, s.id DESC LIMIT 1",
    )
    .bind(import_name)
    .fetch_optional(&pool)
    .await?;
    pool.close().await;

    let (execution_status, execution_exit_code, commit_count, checkpoint_position) =
        row.ok_or_else(|| anyhow::anyhow!("no durable execution/checkpoint exists after crash"))?;

    Ok(CrashStateSnapshot {
        execution_status,
        execution_exit_code,
        commit_count,
        checkpoint_position,
        business_rows,
    })
}

async fn wait_for_business_rows(
    pool: &sqlx::PgPool,
    import_name: &str,
    source_digest: &str,
    expected: i64,
    timeout: Duration,
) -> Result<i64> {
    let deadline = Instant::now() + timeout;

    loop {
        let count: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM app_business.customer_projection \
             WHERE import_name = $1 AND source_digest = $2",
        )
        .bind(import_name)
        .bind(source_digest)
        .fetch_one(pool)
        .await?;

        if count == expected || Instant::now() >= deadline {
            return Ok(count);
        }

        sleep(Duration::from_millis(20)).await;
    }
}

fn require_crash_state(
    state: &CrashStateSnapshot,
    expected_commits: u32,
    expected_position: i64,
) -> Result<()> {
    if !matches!(
        state.execution_status.as_str(),
        "STARTED" | "STARTING" | "UNKNOWN"
    ) {
        bail!(
            "crashed execution unexpectedly reached terminal status {}",
            state.execution_status
        );
    }
    if state.commit_count != i64::from(expected_commits) {
        bail!(
            "crash checkpoint commit count mismatch: expected {}, got {}",
            expected_commits,
            state.commit_count
        );
    }
    if state.checkpoint_position != expected_position {
        bail!(
            "crash checkpoint position mismatch: expected {}, got {}",
            expected_position,
            state.checkpoint_position
        );
    }
    if state.business_rows != expected_position {
        bail!(
            "crash business-row count mismatch: expected {}, got {}",
            expected_position,
            state.business_rows
        );
    }
    Ok(())
}
