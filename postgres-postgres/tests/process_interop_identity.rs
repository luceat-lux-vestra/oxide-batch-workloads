//! Campaign #98: focused launch/execution identity observations that complement
//! `process_interop.rs` without changing production workload behavior.
#![cfg(target_os = "linux")]

mod support;

use std::fs;
use std::os::unix::process::ExitStatusExt;
use std::process::{Output, Stdio};
use std::time::Duration;

use serde_json::json;
use support::SeedOptions;

const ROWS: u64 = 350;
const CHUNK_SIZE: u32 = 100;
const TARGET_CHUNK: u32 = 2;
const MARKER_TIMEOUT: Duration = Duration::from_secs(30);

async fn latest_execution_identity(
    pool: &sqlx::PgPool,
    job_name: &str,
) -> Option<(String, String, String, String)> {
    sqlx::query_as::<_, (String, String, String, String)>(
        "SELECT i.id::text, e.id::text, e.attempt::text, e.status \
         FROM oxide_batch.ob_job_execution e \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         WHERE i.job_name = $1 \
         ORDER BY e.attempt DESC LIMIT 1",
    )
    .bind(job_name)
    .fetch_optional(pool)
    .await
    .expect("query durable job instance/execution identity")
}

fn spawn_plain_with_pid(import_name: &str) -> (u32, Output) {
    let mut command = support::bin();
    command
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(CHUNK_SIZE.to_string())
        .arg("--reader")
        .arg("cursor")
        .arg("--fetch-size")
        .arg("50")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command
        .spawn()
        .expect("spawn recovery continuation process");
    let pid = child.id();
    let output = child
        .wait_with_output()
        .expect("wait for recovery continuation process");
    (pid, output)
}

#[tokio::test]
async fn launcher_pid_and_durable_execution_identity_survive_recovery_boundary() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: ROWS,
        seed: 9804,
    });

    let launcher_pid = std::process::id();
    let import_name = support::unique_name("interop_identity");
    let marker = support::temp_marker("interop-identity");
    let mut child = support::spawn_run_with_failpoint(
        "cursor",
        &import_name,
        CHUNK_SIZE,
        ("--fetch-size", 50),
        TARGET_CHUNK,
        "during-write",
        &marker,
    );
    let workload_pid = child.id();
    assert_ne!(
        launcher_pid, workload_pid,
        "the launcher/test process and workload must be distinct OS processes"
    );
    assert_eq!(
        support::wait_for_marker(&marker, MARKER_TIMEOUT),
        workload_pid,
        "the semantic failpoint marker must identify the exact workload process"
    );

    let pool = support::pool().await;
    let (instance_id, execution_id, attempt, active_status) =
        latest_execution_identity(&pool, &import_name)
            .await
            .expect("active workload execution identity is durable");
    assert!(
        matches!(active_status.as_str(), "STARTING" | "STARTED"),
        "the paused process must have a durable active execution"
    );

    let killed = support::kill_and_wait(&mut child);
    assert_eq!(
        killed.signal(),
        Some(9),
        "the first execution must end by externally delivered SIGKILL"
    );

    let (post_kill_instance_id, post_kill_execution_id, post_kill_attempt, post_kill_status) =
        latest_execution_identity(&pool, &import_name)
            .await
            .expect("crashed execution identity remains durable");
    assert_eq!(post_kill_instance_id, instance_id);
    assert_eq!(post_kill_execution_id, execution_id);
    assert_eq!(post_kill_attempt, attempt);
    assert!(
        matches!(
            post_kill_status.as_str(),
            "STARTING" | "STARTED" | "UNKNOWN"
        ),
        "hard death must not fabricate a terminal execution state"
    );

    let recovered = support::recover(&import_name, "cursor");
    assert!(
        recovered.status.success(),
        "public recovery transition must succeed: stderr={}",
        String::from_utf8_lossy(&recovered.stderr)
    );
    let execution_count_before_continuation =
        support::job_execution_count(&pool, &import_name).await;

    let (continuation_pid, continuation) = spawn_plain_with_pid(&import_name);
    assert_ne!(
        continuation_pid, workload_pid,
        "recovery continuation must use a genuinely new workload process"
    );
    assert_eq!(
        continuation.status.code(),
        Some(0),
        "successful continuation must expose OS exit code 0: stderr={}",
        String::from_utf8_lossy(&continuation.stderr)
    );
    assert_eq!(
        support::job_execution_count(&pool, &import_name).await,
        execution_count_before_continuation + 1,
        "continuation must append exactly one execution"
    );

    let (completed_instance_id, completed_execution_id, completed_attempt, completed_status) =
        latest_execution_identity(&pool, &import_name)
            .await
            .expect("completed continuation identity is durable");
    assert_eq!(
        completed_instance_id, instance_id,
        "recovery continuation must remain attached to the same logical JobInstance"
    );
    assert_ne!(
        completed_execution_id, execution_id,
        "recovery continuation must have a new durable JobExecution identity"
    );
    assert_ne!(
        completed_attempt, attempt,
        "recovery continuation must advance the durable execution attempt"
    );
    assert_eq!(completed_status, "COMPLETED");
    assert!(support::verify(&import_name).status.success());

    let report = json!({
        "schema": "oxide-batch-workloads.process-interop-identity-observation",
        "schema_version": 1,
        "campaign_issue": 98,
        "producer_checkout": std::env::var("GITHUB_SHA").unwrap_or_else(|_| "local".to_owned()),
        "launcher": {
            "pid": launcher_pid
        },
        "initial_execution": {
            "workload_pid": workload_pid,
            "job_instance_id": instance_id,
            "job_execution_id": execution_id,
            "attempt": attempt,
            "durable_status_before_kill": active_status,
            "termination_signal": 9,
            "durable_status_after_kill": post_kill_status
        },
        "recovery_continuation": {
            "workload_pid": continuation_pid,
            "process_exit_code": continuation.status.code(),
            "job_instance_id": completed_instance_id,
            "job_execution_id": completed_execution_id,
            "attempt": completed_attempt,
            "durable_status": completed_status
        }
    });
    fs::create_dir_all("target").expect("create target directory for identity observation report");
    let report_path = "target/process-interop-identity-campaign-98.json";
    fs::write(
        report_path,
        serde_json::to_vec_pretty(&report).expect("serialize identity observation report"),
    )
    .expect("write identity observation report");
    let emitted = std::process::Command::new("cat")
        .arg(report_path)
        .status()
        .expect("emit identity observation report to CI log");
    assert!(emitted.success(), "emit identity observation report");

    let _ = fs::remove_file(&marker);
}
