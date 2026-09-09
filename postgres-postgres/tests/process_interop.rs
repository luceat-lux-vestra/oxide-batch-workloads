//! Campaign #98 / Track F: scheduler-neutral process-boundary qualification.
//!
//! This test deliberately drives the already-shipped `postgres-postgres`
//! binary as an external OS process. It does not add a scheduler, a hosted
//! control plane, framework code, or production workload behavior. The
//! assertions distinguish OS-process observations from durable OxideBatch
//! metadata and from workload-owned business state.
#![cfg(target_os = "linux")]

mod support;

use std::collections::BTreeSet;
use std::fs;
use std::os::unix::process::ExitStatusExt;
use std::process::{Command, Output, Stdio};
use std::time::Duration;

use serde_json::{json, Value};
use support::SeedOptions;

const CHUNK_SIZE: u32 = 100;
const ROWS: u64 = 550;
const TARGET_CHUNK: u32 = 3;
const PREVIOUS_COMMITTED_ROWS: i64 = 200;
const MARKER_TIMEOUT: Duration = Duration::from_secs(30);
const ROLLBACK_SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

fn proc_args(pid: u32) -> Vec<String> {
    fs::read(format!("/proc/{pid}/cmdline"))
        .expect("read child /proc cmdline")
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8(part.to_vec()).expect("child argv is UTF-8"))
        .collect()
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
        "the exact process observed before termination is still alive"
    );
}

fn spawn_plain(reader_mode: &str, import_name: &str, size_flag: (&str, usize)) -> (u32, Output) {
    let mut command = support::bin();
    command
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(CHUNK_SIZE.to_string())
        .arg("--reader")
        .arg(reader_mode)
        .arg(size_flag.0)
        .arg(size_flag.1.to_string())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command.spawn().expect("spawn external workload process");
    let pid = child.id();
    let output = child
        .wait_with_output()
        .expect("wait for external workload process");
    (pid, output)
}

fn assert_identifying_parameters(
    identifying: &[Value],
    import_name: &str,
    source_digest: &str,
    reader_mode: &str,
) {
    assert_eq!(identifying.len(), 1, "one logical identity is expected");
    let object = identifying[0]
        .as_object()
        .expect("identifying_parameters is a JSON object");
    let actual_keys: BTreeSet<String> = object.keys().cloned().collect();
    let expected_keys = BTreeSet::from([
        "import_name".to_owned(),
        "reader_mode".to_owned(),
        "source_digest".to_owned(),
    ]);
    assert_eq!(
        actual_keys, expected_keys,
        "this workload binds exactly its three documented identifying parameters; no scheduler correlation parameter is fabricated"
    );

    for key in ["import_name", "reader_mode", "source_digest"] {
        assert_eq!(
            identifying[0][key]["identifying"].as_bool(),
            Some(true),
            "{key} must be durably marked identifying"
        );
    }
    assert_eq!(
        identifying[0]["import_name"]["value"].as_str(),
        Some(import_name)
    );
    assert_eq!(
        identifying[0]["reader_mode"]["value"].as_str(),
        Some(reader_mode)
    );
    assert_eq!(
        identifying[0]["source_digest"]["value"].as_str(),
        Some(source_digest)
    );
}

async fn assert_no_idle_transaction(pool: &sqlx::PgPool) {
    let idle_in_transaction: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM pg_stat_activity \
         WHERE pid <> pg_backend_pid() AND state = 'idle in transaction'",
    )
    .fetch_one(pool)
    .await
    .expect("query PostgreSQL session state");
    assert_eq!(
        idle_in_transaction, 0,
        "terminal workload processes must not leave an idle transaction behind"
    );
}

#[tokio::test]
async fn external_process_launch_lifecycle_duplicate_and_recovery_are_qualified() {
    support::migrate();

    // ------------------------------------------------------------------
    // Validation rejection: caller error is process-visible and never
    // becomes a durable framework execution.
    // ------------------------------------------------------------------
    support::reset();
    support::seed(SeedOptions { rows: 20, seed: 98 });
    let invalid_name = support::unique_name("interop_missing_reader");
    let invalid = support::bin()
        .arg("run")
        .arg("--import-name")
        .arg(&invalid_name)
        .output()
        .expect("spawn invalid external invocation");
    assert!(
        !invalid.status.success(),
        "missing --reader must fail closed"
    );
    let pool = support::pool().await;
    assert!(
        support::latest_execution_status(&pool, &invalid_name)
            .await
            .is_none(),
        "CLI validation rejection must not create a JobExecution"
    );

    // ------------------------------------------------------------------
    // Infrastructure failure before repository access: a scheduler sees a
    // nonzero process result, while the real repository has no execution to
    // query for this logical request.
    // ------------------------------------------------------------------
    let infrastructure_name = support::unique_name("interop_infrastructure");
    let infrastructure = support::bin()
        .arg("run")
        .arg("--database-url")
        .arg("postgresql://unused:unused@127.0.0.1:1/unreachable")
        .arg("--import-name")
        .arg(&infrastructure_name)
        .arg("--chunk-size")
        .arg("10")
        .arg("--reader")
        .arg("cursor")
        .output()
        .expect("spawn infrastructure-failure invocation");
    assert!(
        !infrastructure.status.success(),
        "unreachable PostgreSQL must be process-visible as failure"
    );
    assert!(
        support::latest_execution_status(&pool, &infrastructure_name)
            .await
            .is_none(),
        "a failure before connecting to the repository cannot have durable execution state"
    );

    // ------------------------------------------------------------------
    // Typed component failure: terminal FAILED is durable, process exit is
    // nonzero, and a plain new process may resume without an operator
    // recovery transition.
    // ------------------------------------------------------------------
    support::reset();
    support::seed(SeedOptions {
        rows: ROWS,
        seed: 9801,
    });
    let typed_name = support::unique_name("interop_typed_failure");
    let typed = support::run_with_typed_failpoint(
        "cursor",
        &typed_name,
        CHUNK_SIZE,
        ("--fetch-size", 50),
        TARGET_CHUNK,
        "during-write",
    );
    assert!(
        !typed.status.success(),
        "typed component failure must produce a nonzero process result"
    );
    let typed_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute typed-failure source digest");
    assert_eq!(
        support::destination_row_count(&pool, &typed_name, &typed_digest).await,
        PREVIOUS_COMMITTED_ROWS
    );
    let (typed_status, typed_commits, typed_position) =
        support::latest_checkpoint(&pool, &typed_name)
            .await
            .expect("typed failure has durable execution metadata");
    assert_eq!(typed_status, "FAILED");
    assert_eq!(typed_commits, 2);
    assert_eq!(typed_position, PREVIOUS_COMMITTED_ROWS);
    let typed_restart = support::run_cursor_with_fetch_size(&typed_name, CHUNK_SIZE, 50);
    assert!(typed_restart.status.success());
    assert!(support::verify(&typed_name).status.success());

    // ------------------------------------------------------------------
    // Active duplicate + lost-response retry. The first real process is
    // paused at a semantic boundary so the second delivery is guaranteed to
    // overlap an execution still recorded as active.
    // ------------------------------------------------------------------
    support::reset();
    support::seed(SeedOptions {
        rows: ROWS,
        seed: 9802,
    });
    let primary_name = support::unique_name("interop_active_primary");
    let marker = support::temp_marker("interop-active-primary");
    let mut primary = support::spawn_run_with_failpoint(
        "cursor",
        &primary_name,
        CHUNK_SIZE,
        ("--fetch-size", 50),
        TARGET_CHUNK,
        "during-write",
        &marker,
    );
    let primary_pid = primary.id();
    let primary_start_time = proc_start_time(primary_pid);
    let marker_pid = support::wait_for_marker(&marker, MARKER_TIMEOUT);
    assert_eq!(
        marker_pid, primary_pid,
        "marker must identify the exact child"
    );

    let argv = proc_args(primary_pid);
    let expected_argv = vec![
        "run".to_owned(),
        "--import-name".to_owned(),
        primary_name.clone(),
        "--chunk-size".to_owned(),
        CHUNK_SIZE.to_string(),
        "--reader".to_owned(),
        "cursor".to_owned(),
        "--fetch-size".to_owned(),
        "50".to_owned(),
        "--fail-at-chunk".to_owned(),
        TARGET_CHUNK.to_string(),
        "--failure-mode".to_owned(),
        "during-write".to_owned(),
        "--pause-for-kill".to_owned(),
        marker.to_string_lossy().into_owned(),
    ];
    assert_eq!(
        argv.get(1..),
        Some(expected_argv.as_slice()),
        "the separate workload process must receive the deterministic launch arguments unchanged"
    );
    assert!(
        !argv
            .join(" ")
            .contains("oxide_batch_workload:oxide_batch_workload"),
        "database credentials must not be present in the process command line"
    );
    assert!(
        proc_children(primary_pid).is_empty(),
        "the paused workload owns no child OS process that could be orphaned"
    );

    let primary_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute active-run source digest");
    assert_eq!(
        support::wait_for_row_count(
            &pool,
            &primary_name,
            &primary_digest,
            PREVIOUS_COMMITTED_ROWS,
            ROLLBACK_SETTLE_TIMEOUT,
        )
        .await,
        PREVIOUS_COMMITTED_ROWS
    );
    assert_eq!(support::job_instance_count(&pool, &primary_name).await, 1);
    assert_eq!(support::job_execution_count(&pool, &primary_name).await, 1);
    let identifying = support::job_instance_identifying_parameters(&pool, &primary_name).await;
    assert_identifying_parameters(&identifying, &primary_name, &primary_digest, "cursor");

    // Model a lost launch response by redelivering the exact same logical
    // request while the original process is still active. This must not
    // create a second execution or any additional durable business prefix.
    let duplicate = support::run_plain("cursor", &primary_name, CHUNK_SIZE, ("--fetch-size", 50));
    assert!(
        !duplicate.status.success(),
        "same-identity overlapping delivery must fail closed"
    );
    assert_eq!(support::job_instance_count(&pool, &primary_name).await, 1);
    assert_eq!(support::job_execution_count(&pool, &primary_name).await, 1);
    assert_eq!(
        support::destination_row_count(&pool, &primary_name, &primary_digest).await,
        PREVIOUS_COMMITTED_ROWS,
        "duplicate/retry rejection must not create additional business effects"
    );

    // A different identifying parameter (`import_name`) is a distinct
    // logical job instance and may execute while the first instance is
    // paused. This is not framework launch-idempotency; it is identity
    // isolation.
    let independent_name = support::unique_name("interop_independent_identity");
    let (independent_pid, independent) =
        spawn_plain("cursor", &independent_name, ("--fetch-size", 50));
    assert_ne!(independent_pid, primary_pid);
    assert!(
        independent.status.success(),
        "a different logical identity should complete independently: stderr={}",
        String::from_utf8_lossy(&independent.stderr)
    );
    assert!(support::verify(&independent_name).status.success());
    assert_eq!(
        support::job_instance_count(&pool, &independent_name).await,
        1
    );
    assert_eq!(
        support::job_execution_count(&pool, &independent_name).await,
        1
    );
    let clean_digest =
        support::destination_content_digest(&pool, &independent_name, &primary_digest).await;

    // Real externally delivered SIGKILL. The old PID is reaped; durable
    // state remains nonterminal and requires explicit public recovery.
    let killed = support::kill_and_wait(&mut primary);
    assert_eq!(killed.signal(), Some(9), "termination must be real SIGKILL");
    assert_old_process_dead(primary_pid, &primary_start_time);
    let (killed_status, killed_commits, killed_position) =
        support::latest_checkpoint(&pool, &primary_name)
            .await
            .expect("killed execution remains durably observable");
    assert!(
        matches!(killed_status.as_str(), "STARTED" | "STARTING" | "UNKNOWN"),
        "SIGKILL cannot have persisted a terminal lifecycle state"
    );
    assert_eq!(killed_commits, 2);
    assert_eq!(killed_position, PREVIOUS_COMMITTED_ROWS);

    let unrecovered = support::run_plain("cursor", &primary_name, CHUNK_SIZE, ("--fetch-size", 50));
    assert!(
        !unrecovered.status.success(),
        "plain launch against a crashed active execution must be rejected"
    );
    assert_eq!(support::job_execution_count(&pool, &primary_name).await, 1);

    let recovered = support::recover(&primary_name, "cursor");
    assert!(
        recovered.status.success(),
        "public recovery transition must succeed: stderr={}",
        String::from_utf8_lossy(&recovered.stderr)
    );
    let executions_before_restart = support::job_execution_count(&pool, &primary_name).await;
    let (restart_pid, restart) = spawn_plain("cursor", &primary_name, ("--fetch-size", 50));
    assert_ne!(
        restart_pid, primary_pid,
        "continuation must execute in a genuinely new OS process"
    );
    assert!(
        restart.status.success(),
        "recovered continuation must complete: stderr={}",
        String::from_utf8_lossy(&restart.stderr)
    );
    let executions_after_restart = support::job_execution_count(&pool, &primary_name).await;
    assert_eq!(
        executions_after_restart,
        executions_before_restart + 1,
        "recovery continuation must append exactly one new execution lifecycle"
    );
    assert_eq!(
        support::destination_row_count(&pool, &primary_name, &primary_digest).await,
        ROWS as i64
    );
    assert!(support::verify(&primary_name).status.success());
    let recovered_digest =
        support::destination_content_digest(&pool, &primary_name, &primary_digest).await;
    assert_eq!(
        recovered_digest, clean_digest,
        "clean and SIGKILL+recovered executions of identical source content must converge exactly"
    );
    let (final_status, final_exit_code) = support::latest_execution_status(&pool, &primary_name)
        .await
        .expect("completed recovery execution is durable");
    assert_eq!(final_status, "COMPLETED");
    assert_eq!(final_exit_code, "COMPLETED");

    // A completed instance is not a fresh logical occurrence. Redelivery of
    // the exact same identifying parameters is rejected rather than turned
    // into an uncorrelated new instance/execution.
    let execution_count_before_completed_redelivery =
        support::job_execution_count(&pool, &primary_name).await;
    let completed_redelivery =
        support::run_plain("cursor", &primary_name, CHUNK_SIZE, ("--fetch-size", 50));
    assert!(
        !completed_redelivery.status.success(),
        "same-identity relaunch after COMPLETED must not silently become a new occurrence"
    );
    assert_eq!(
        support::job_execution_count(&pool, &primary_name).await,
        execution_count_before_completed_redelivery,
        "completed redelivery must not append a new execution"
    );

    // Independent verifier negative control: corrupt workload-owned
    // business state directly, require detection, then restore it so the
    // process-lifecycle test leaves a clean terminal business state.
    let corrupted = sqlx::query(
        "UPDATE app_business.customer_projection \
         SET loyalty_score = loyalty_score + 1 \
         WHERE import_name = $1 AND source_digest = $2 \
           AND customer_id = (SELECT min(customer_id) FROM app_business.customer_projection \
                              WHERE import_name = $1 AND source_digest = $2)",
    )
    .bind(&primary_name)
    .bind(&primary_digest)
    .execute(&pool)
    .await
    .expect("deliberately corrupt one business row");
    assert_eq!(corrupted.rows_affected(), 1);
    assert!(
        !support::verify(&primary_name).status.success(),
        "independent verifier must reject deliberate business corruption"
    );
    let restored = sqlx::query(
        "UPDATE app_business.customer_projection \
         SET loyalty_score = loyalty_score - 1 \
         WHERE import_name = $1 AND source_digest = $2 \
           AND customer_id = (SELECT min(customer_id) FROM app_business.customer_projection \
                              WHERE import_name = $1 AND source_digest = $2)",
    )
    .bind(&primary_name)
    .bind(&primary_digest)
    .execute(&pool)
    .await
    .expect("restore deliberately corrupted business row");
    assert_eq!(restored.rows_affected(), 1);
    assert!(support::verify(&primary_name).status.success());
    assert_no_idle_transaction(&pool).await;
    let _ = fs::remove_file(&marker);

    // ------------------------------------------------------------------
    // SIGTERM qualification. OxideBatch 0.6.0 is not given a workload-side
    // signal bridge here: actual POSIX SIGTERM kills the process abruptly,
    // leaves the durable execution nonterminal, and therefore requires the
    // same explicit recovery transition as hard death. This is evidence of
    // an unsupported graceful signal contract, not a simulated stop.
    // ------------------------------------------------------------------
    support::reset();
    support::seed(SeedOptions {
        rows: ROWS,
        seed: 9803,
    });
    let term_name = support::unique_name("interop_sigterm");
    let term_marker = support::temp_marker("interop-sigterm");
    let mut term_child = support::spawn_run_with_failpoint(
        "paging",
        &term_name,
        CHUNK_SIZE,
        ("--page-size", 60),
        TARGET_CHUNK,
        "during-write",
        &term_marker,
    );
    let term_pid = term_child.id();
    let term_start_time = proc_start_time(term_pid);
    assert_eq!(
        support::wait_for_marker(&term_marker, MARKER_TIMEOUT),
        term_pid
    );
    assert!(proc_children(term_pid).is_empty());
    let term_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute SIGTERM source digest");
    assert_eq!(
        support::wait_for_row_count(
            &pool,
            &term_name,
            &term_digest,
            PREVIOUS_COMMITTED_ROWS,
            ROLLBACK_SETTLE_TIMEOUT,
        )
        .await,
        PREVIOUS_COMMITTED_ROWS
    );

    let delivered_term = Command::new("kill")
        .arg("-TERM")
        .arg(term_pid.to_string())
        .status()
        .expect("deliver external SIGTERM");
    assert!(delivered_term.success(), "SIGTERM delivery command failed");
    let term_exit = term_child.wait().expect("reap SIGTERM child");
    assert_eq!(term_exit.signal(), Some(15), "must observe real SIGTERM");
    assert_old_process_dead(term_pid, &term_start_time);
    let (term_status, term_commits, term_position) = support::latest_checkpoint(&pool, &term_name)
        .await
        .expect("SIGTERM execution remains durably observable");
    assert!(
        matches!(term_status.as_str(), "STARTED" | "STARTING" | "UNKNOWN"),
        "without a signal bridge SIGTERM must not be misreported as graceful terminal stop"
    );
    assert_eq!(term_commits, 2);
    assert_eq!(term_position, PREVIOUS_COMMITTED_ROWS);
    assert!(
        !support::run_plain("paging", &term_name, CHUNK_SIZE, ("--page-size", 60))
            .status
            .success(),
        "SIGTERM-abandoned execution requires explicit recovery"
    );
    assert!(support::recover(&term_name, "paging").status.success());
    let (term_restart_pid, term_restart) = spawn_plain("paging", &term_name, ("--page-size", 60));
    assert_ne!(term_restart_pid, term_pid);
    assert!(
        term_restart.status.success(),
        "SIGTERM recovery continuation must complete: stderr={}",
        String::from_utf8_lossy(&term_restart.stderr)
    );
    assert_eq!(
        support::destination_row_count(&pool, &term_name, &term_digest).await,
        ROWS as i64
    );
    assert!(support::verify(&term_name).status.success());
    let (term_final_status, term_final_exit_code) =
        support::latest_execution_status(&pool, &term_name)
            .await
            .expect("SIGTERM recovery completion is durable");
    assert_eq!(term_final_status, "COMPLETED");
    assert_eq!(term_final_exit_code, "COMPLETED");
    assert_no_idle_transaction(&pool).await;
    let _ = fs::remove_file(&term_marker);

    // Primitive observations are emitted for the GitHub Actions log. The
    // assertions above, not this producer-authored summary, are normative.
    let report = json!({
        "schema": "oxide-batch-workloads.process-interop-observation",
        "schema_version": 1,
        "campaign_issue": 98,
        "producer_checkout": std::env::var("GITHUB_SHA").unwrap_or_else(|_| "local".to_owned()),
        "validation_subject": {
            "crate": "oxide-batch",
            "version": "0.6.0",
            "source": "crates.io"
        },
        "validation_rejection": {
            "process_exit_code": invalid.status.code(),
            "durable_execution_created": false
        },
        "infrastructure_failure": {
            "process_exit_code": infrastructure.status.code(),
            "durable_execution_observable_in_target_repository": false
        },
        "typed_failure": {
            "process_exit_code": typed.status.code(),
            "durable_status": typed_status,
            "durable_commits": typed_commits,
            "durable_position": typed_position,
            "plain_restart_supported": true
        },
        "active_duplicate_lost_response_retry": {
            "primary_pid": primary_pid,
            "duplicate_process_exit_code": duplicate.status.code(),
            "job_instances_after_retry": 1,
            "job_executions_after_retry": 1,
            "durable_business_rows_after_retry": PREVIOUS_COMMITTED_ROWS,
            "different_identity_pid": independent_pid,
            "different_identity_completed": true
        },
        "sigkill": {
            "old_pid": primary_pid,
            "signal": 9,
            "durable_status_before_recovery": killed_status,
            "durable_commits_before_recovery": killed_commits,
            "durable_position_before_recovery": killed_position,
            "new_process_pid": restart_pid,
            "final_status": final_status,
            "final_exit_code": final_exit_code,
            "clean_recovered_content_digest_equal": true
        },
        "completed_redelivery": {
            "process_exit_code": completed_redelivery.status.code(),
            "new_execution_created": false
        },
        "sigterm": {
            "old_pid": term_pid,
            "signal": 15,
            "durable_status_before_recovery": term_status,
            "durable_commits_before_recovery": term_commits,
            "durable_position_before_recovery": term_position,
            "new_process_pid": term_restart_pid,
            "final_status": term_final_status,
            "final_exit_code": term_final_exit_code,
            "graceful_signal_contract_observed": false
        },
        "verifier_negative_control": {
            "deliberate_corruption_detected": true,
            "restored_state_verified": true
        },
        "capability_gaps": {
            "dedicated_external_correlation_identity": "NOT_PROVEN: this workload binds only import_name/source_digest/reader_mode as identifying parameters",
            "machine_readable_launch_result": "NOT_PROVEN: run emits no machine-readable result envelope; durable repository state remains authoritative",
            "graceful_sigterm": "UNSUPPORTED/GAP: raw SIGTERM is abrupt without a consumer signal bridge",
            "stop_cancel_process_contract": "UNSUPPORTED/GAP: no Track-F process stop/cancel command is exposed by this workload"
        }
    });
    fs::create_dir_all("target").expect("create target directory for observation report");
    let report_path = "target/process-interop-campaign-98.json";
    fs::write(
        report_path,
        serde_json::to_vec_pretty(&report).expect("serialize observation report"),
    )
    .expect("write observation report");
    let cat_status = Command::new("cat")
        .arg(report_path)
        .status()
        .expect("emit observation report to CI log");
    assert!(cat_status.success(), "emit observation report");
}
