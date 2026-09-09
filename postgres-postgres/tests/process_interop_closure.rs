//! Campaign #98 / Track F proof closure for process ownership, SIGTERM
//! observability, durable recovery gap attribution, and exposed output channels.
#![cfg(target_os = "linux")]

mod support;

use std::fs;
use std::os::unix::process::ExitStatusExt;
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

use serde_json::json;
use support::SeedOptions;

const ROWS: u64 = 350;
const CHUNK_SIZE: u32 = 100;
const TARGET_CHUNK: u32 = 2;
const PREVIOUS_COMMITTED_ROWS: i64 = 100;
const MARKER_TIMEOUT: Duration = Duration::from_secs(30);
const ROLLBACK_SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

fn proc_parent_pid(pid: u32) -> u32 {
    let status =
        fs::read_to_string(format!("/proc/{pid}/status")).expect("read child /proc status");
    status
        .lines()
        .find_map(|line| line.strip_prefix("PPid:"))
        .expect("Linux /proc status exposes PPid")
        .trim()
        .parse::<u32>()
        .expect("PPid is numeric")
}

fn proc_children(pid: u32) -> Vec<u32> {
    fs::read_to_string(format!("/proc/{pid}/task/{pid}/children"))
        .expect("read child process descendants")
        .split_whitespace()
        .map(|value| value.parse::<u32>().expect("child PID is numeric"))
        .collect()
}

fn proc_start_time(pid: u32) -> String {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).expect("read child /proc stat");
    let (_, tail) = stat
        .rsplit_once(") ")
        .expect("Linux /proc stat contains a parenthesized comm field");
    tail.split_whitespace()
        .nth(19)
        .expect("Linux /proc stat exposes starttime as field 22")
        .to_owned()
}

fn assert_old_process_dead(pid: u32, old_start_time: &str) {
    let path = format!("/proc/{pid}/stat");
    let Ok(stat) = fs::read_to_string(path) else {
        return;
    };
    let (_, tail) = stat
        .rsplit_once(") ")
        .expect("Linux /proc stat contains a parenthesized comm field");
    let current_start_time = tail
        .split_whitespace()
        .nth(19)
        .expect("Linux /proc stat exposes starttime as field 22");
    assert_ne!(
        current_start_time, old_start_time,
        "the exact pre-termination workload process is still alive"
    );
}

fn assert_output_has_no_database_secret(output: &Output) {
    const SECRET_FRAGMENT: &str = "oxide_batch_workload:oxide_batch_workload";
    assert!(
        !String::from_utf8_lossy(&output.stdout).contains(SECRET_FRAGMENT),
        "database credentials must not leak to stdout"
    );
    assert!(
        !String::from_utf8_lossy(&output.stderr).contains(SECRET_FRAGMENT),
        "database credentials must not leak to stderr"
    );
}

async fn latest_execution_identity(
    pool: &sqlx::PgPool,
    job_name: &str,
) -> (String, String, String) {
    sqlx::query_as::<_, (String, String, String)>(
        "SELECT e.id::text, e.attempt::text, e.status \
         FROM oxide_batch.ob_job_execution e \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         WHERE i.job_name = $1 \
         ORDER BY e.attempt DESC LIMIT 1",
    )
    .bind(job_name)
    .fetch_one(pool)
    .await
    .expect("query latest durable execution identity")
}

async fn execution_status(pool: &sqlx::PgPool, execution_id: &str) -> String {
    sqlx::query_scalar("SELECT status FROM oxide_batch.ob_job_execution WHERE id::text = $1")
        .bind(execution_id)
        .fetch_one(pool)
        .await
        .expect("query durable execution status by id")
}

async fn active_execution_count(pool: &sqlx::PgPool, job_name: &str) -> i64 {
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM oxide_batch.ob_job_execution e \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         WHERE i.job_name = $1 \
           AND e.status IN ('STARTING', 'STARTED', 'STOPPING', 'UNKNOWN')",
    )
    .bind(job_name)
    .fetch_one(pool)
    .await
    .expect("count active or unresolved job executions")
}

async fn active_step_execution_count(pool: &sqlx::PgPool, job_name: &str) -> i64 {
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM oxide_batch.ob_step_execution s \
         JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         WHERE i.job_name = $1 \
           AND s.status IN ('STARTING', 'STARTED', 'STOPPING', 'UNKNOWN')",
    )
    .bind(job_name)
    .fetch_one(pool)
    .await
    .expect("count active or unresolved step executions")
}

async fn execution_history(
    pool: &sqlx::PgPool,
    job_name: &str,
) -> Vec<(String, String, String, String)> {
    sqlx::query_as::<_, (String, String, String, String)>(
        "SELECT e.id::text, e.attempt::text, e.status, e.exit_code \
         FROM oxide_batch.ob_job_execution e \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         WHERE i.job_name = $1 \
         ORDER BY e.attempt",
    )
    .bind(job_name)
    .fetch_all(pool)
    .await
    .expect("query durable job execution history")
}

async fn step_history(pool: &sqlx::PgPool, job_name: &str) -> Vec<(String, String, i64, i64)> {
    sqlx::query_as::<_, (String, String, i64, i64)>(
        "SELECT s.job_execution_id::text, s.status, s.commit_count, \
                COALESCE((s.checkpoint_payload->>'position')::bigint, 0) \
         FROM oxide_batch.ob_step_execution s \
         JOIN oxide_batch.ob_job_execution e ON e.id = s.job_execution_id \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         WHERE i.job_name = $1 \
         ORDER BY e.attempt",
    )
    .bind(job_name)
    .fetch_all(pool)
    .await
    .expect("query durable step execution history")
}

async fn assert_no_idle_transaction(pool: &sqlx::PgPool) {
    let count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM pg_stat_activity \
         WHERE pid <> pg_backend_pid() AND state = 'idle in transaction'",
    )
    .fetch_one(pool)
    .await
    .expect("query PostgreSQL session state");
    assert_eq!(
        count, 0,
        "terminal processes must not leak an idle transaction"
    );
}

fn spawn_paused_sigterm_target(import_name: &str, marker: &std::path::Path) -> std::process::Child {
    support::bin()
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(CHUNK_SIZE.to_string())
        .arg("--reader")
        .arg("paging")
        .arg("--page-size")
        .arg("60")
        .arg("--fail-at-chunk")
        .arg(TARGET_CHUNK.to_string())
        .arg("--failure-mode")
        .arg("during-write")
        .arg("--pause-for-kill")
        .arg(marker)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn external SIGTERM target")
}

fn spawn_continuation(import_name: &str) -> (u32, Output) {
    let mut command = support::bin();
    command
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(CHUNK_SIZE.to_string())
        .arg("--reader")
        .arg("paging")
        .arg("--page-size")
        .arg("60")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command.spawn().expect("spawn recovery continuation");
    let pid = child.id();
    let output = child
        .wait_with_output()
        .expect("wait for recovery continuation");
    (pid, output)
}

#[tokio::test]
async fn sigterm_recovery_qualifies_process_ownership_and_exposes_step_lifecycle_gap() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: ROWS,
        seed: 9805,
    });

    let launcher_pid = std::process::id();
    let import_name = support::unique_name("interop_closure");
    let marker = support::temp_marker("interop-closure");
    let child = spawn_paused_sigterm_target(&import_name, &marker);
    let workload_pid = child.id();
    let workload_start_time = proc_start_time(workload_pid);

    assert_eq!(
        support::wait_for_marker(&marker, MARKER_TIMEOUT),
        workload_pid,
        "failpoint marker must identify the exact workload process"
    );
    assert_eq!(
        proc_parent_pid(workload_pid),
        launcher_pid,
        "the external workload must be directly owned by the launcher process"
    );
    assert!(
        proc_children(workload_pid).is_empty(),
        "the paused workload must not own child OS processes"
    );

    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest");
    assert_eq!(
        support::wait_for_row_count(
            &pool,
            &import_name,
            &source_digest,
            PREVIOUS_COMMITTED_ROWS,
            ROLLBACK_SETTLE_TIMEOUT,
        )
        .await,
        PREVIOUS_COMMITTED_ROWS
    );
    let (crashed_execution_id, crashed_attempt, pre_signal_status) =
        latest_execution_identity(&pool, &import_name).await;
    assert_eq!(crashed_attempt, "1");
    assert!(
        matches!(pre_signal_status.as_str(), "STARTING" | "STARTED"),
        "paused workload must have a durable active JobExecution"
    );

    let signal_started = Instant::now();
    let delivered = Command::new("kill")
        .arg("-TERM")
        .arg(workload_pid.to_string())
        .status()
        .expect("deliver external SIGTERM");
    assert!(delivered.success(), "SIGTERM delivery command failed");
    let term_output = child.wait_with_output().expect("reap SIGTERM workload");
    let sigterm_shutdown_latency = signal_started.elapsed();

    assert_eq!(
        term_output.status.signal(),
        Some(15),
        "workload must terminate from the externally delivered SIGTERM"
    );
    assert_eq!(
        term_output.status.code(),
        None,
        "Rust ExitStatus exposes no numeric exit code when terminated by SIGTERM"
    );
    assert!(
        term_output.stdout.is_empty(),
        "run exposes no stdout launch-result envelope"
    );
    assert!(
        !term_output.stderr.is_empty(),
        "run diagnostics are actually exposed on stderr"
    );
    assert_output_has_no_database_secret(&term_output);
    assert_old_process_dead(workload_pid, &workload_start_time);
    assert_eq!(
        support::wait_for_row_count(
            &pool,
            &import_name,
            &source_digest,
            PREVIOUS_COMMITTED_ROWS,
            ROLLBACK_SETTLE_TIMEOUT,
        )
        .await,
        PREVIOUS_COMMITTED_ROWS,
        "SIGTERM target chunk must roll back to the committed prefix"
    );

    let (status_after_signal, commits_after_signal, position_after_signal) =
        support::latest_checkpoint(&pool, &import_name)
            .await
            .expect("SIGTERM execution remains durably observable");
    assert!(
        matches!(
            status_after_signal.as_str(),
            "STARTING" | "STARTED" | "UNKNOWN"
        ),
        "raw SIGTERM must not fabricate a graceful terminal status"
    );
    assert_eq!(commits_after_signal, 1);
    assert_eq!(position_after_signal, PREVIOUS_COMMITTED_ROWS);

    let recovery = support::recover(&import_name, "paging");
    assert_eq!(recovery.status.code(), Some(0));
    assert!(
        recovery.stdout.is_empty(),
        "recover exposes no stdout machine-result envelope"
    );
    assert!(
        !recovery.stderr.is_empty(),
        "recover diagnostics are actually exposed on stderr"
    );
    assert_output_has_no_database_secret(&recovery);
    assert_eq!(
        execution_status(&pool, &crashed_execution_id).await,
        "FAILED",
        "public recovery must resolve the crashed JobExecution to FAILED"
    );
    assert_eq!(
        active_execution_count(&pool, &import_name).await,
        0,
        "public recovery must leave no active or unresolved JobExecution"
    );
    assert_eq!(
        active_step_execution_count(&pool, &import_name).await,
        1,
        "exact published 0.6.0 leaves the crashed attempt's StepExecution nonterminal; \
         this observed framework gap is tracked by oxide-batch#269"
    );

    let (continuation_pid, continuation) = spawn_continuation(&import_name);
    assert_ne!(
        continuation_pid, workload_pid,
        "recovery continuation must execute in a genuinely new OS process"
    );
    assert_eq!(continuation.status.code(), Some(0));
    assert!(
        continuation.stdout.is_empty(),
        "successful run exposes no stdout launch-result envelope"
    );
    assert!(
        !continuation.stderr.is_empty(),
        "successful run diagnostics are exposed on stderr"
    );
    assert_output_has_no_database_secret(&continuation);
    assert!(support::verify(&import_name).status.success());
    assert_eq!(
        support::destination_row_count(&pool, &import_name, &source_digest).await,
        ROWS as i64
    );
    assert_eq!(active_execution_count(&pool, &import_name).await, 0);
    assert_eq!(
        active_step_execution_count(&pool, &import_name).await,
        1,
        "the recovered attempt's stale StepExecution remains observable after a successful \
         continuation; do not misreport this as lifecycle closure"
    );
    assert_no_idle_transaction(&pool).await;

    let executions = execution_history(&pool, &import_name).await;
    assert_eq!(
        executions.len(),
        2,
        "exactly two execution attempts are expected"
    );
    assert_eq!(executions[0].0, crashed_execution_id);
    assert_eq!(executions[0].1, "1");
    assert_eq!(executions[0].2, "FAILED");
    assert_eq!(executions[1].1, "2");
    assert_eq!(executions[1].2, "COMPLETED");
    assert_eq!(executions[1].3, "COMPLETED");
    assert_ne!(executions[0].0, executions[1].0);

    let steps = step_history(&pool, &import_name).await;
    assert_eq!(
        steps.len(),
        2,
        "each durable JobExecution attempt must own one observable StepExecution"
    );
    assert_eq!(steps[0].0, executions[0].0);
    assert_eq!(
        steps[0].1, "STARTED",
        "0.6.0 recovery leaves the crashed attempt's child step nonterminal"
    );
    assert_eq!(steps[0].2, 1);
    assert_eq!(steps[0].3, PREVIOUS_COMMITTED_ROWS);
    assert_eq!(steps[1].0, executions[1].0);
    assert_eq!(steps[1].1, "COMPLETED");
    assert_eq!(steps[1].3, ROWS as i64);

    let report = json!({
        "schema": "oxide-batch-workloads.process-interop-closure-observation",
        "schema_version": 1,
        "campaign_issue": 98,
        "producer_checkout": std::env::var("GITHUB_SHA").unwrap_or_else(|_| "local".to_owned()),
        "launcher_pid": launcher_pid,
        "sigterm": {
            "workload_pid": workload_pid,
            "launcher_is_direct_parent": true,
            "signal": 15,
            "numeric_exit_code": term_output.status.code(),
            "shutdown_latency_micros": sigterm_shutdown_latency.as_micros(),
            "stdout_bytes": term_output.stdout.len(),
            "stderr_bytes": term_output.stderr.len(),
            "durable_status_before_recovery": status_after_signal,
            "committed_chunks_before_recovery": commits_after_signal,
            "checkpoint_position_before_recovery": position_after_signal
        },
        "public_recovery": {
            "process_exit_code": recovery.status.code(),
            "stdout_bytes": recovery.stdout.len(),
            "stderr_bytes": recovery.stderr.len(),
            "recovered_job_execution_id": crashed_execution_id,
            "recovered_status": executions[0].2,
            "active_job_executions_after_recovery": 0,
            "active_step_executions_after_recovery": 1,
            "step_lifecycle_closure": "UNSUPPORTED/GAP",
            "upstream_issue": "luceat-lux-vestra/oxide-batch#269"
        },
        "continuation": {
            "workload_pid": continuation_pid,
            "process_exit_code": continuation.status.code(),
            "stdout_bytes": continuation.stdout.len(),
            "stderr_bytes": continuation.stderr.len(),
            "job_execution_id": executions[1].0,
            "attempt": executions[1].1,
            "status": executions[1].2,
            "active_job_executions_final": 0,
            "active_step_executions_final": 1
        },
        "capability_gaps": {
            "recovery_step_lifecycle_closure": "UNSUPPORTED/GAP: exact published 0.6.0 recovers the JobExecution but leaves the crashed child StepExecution STARTED; tracked by oxide-batch#269"
        },
        "step_execution_history": steps
    });
    fs::create_dir_all("target").expect("create target directory for closure observation report");
    let report_path = "target/process-interop-closure-campaign-98.json";
    fs::write(
        report_path,
        serde_json::to_vec_pretty(&report).expect("serialize closure observation report"),
    )
    .expect("write closure observation report");
    let emitted = Command::new("cat")
        .arg(report_path)
        .status()
        .expect("emit closure observation report to CI log");
    assert!(emitted.success(), "emit closure observation report");

    let _ = fs::remove_file(&marker);
}
