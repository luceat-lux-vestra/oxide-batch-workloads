//! Campaign #63 PR 3: a real OS-level hard kill (`SIGKILL`, via
//! `std::process::Child::kill`, never `Result::Err`, a graceful shutdown, or
//! a self-inflicted `abort()`) delivered to the real compiled workload
//! binary while a target chunk's business writes have executed but its
//! transaction has not yet committed -- and recovery/continuation performed
//! by a genuinely new process afterward. For both reader modes.
//!
//! Synchronization between this test (the parent) and the child under test
//! is a marker file the child's own failpoint writes (with its own PID)
//! immediately before pausing forever (see `src/failpoint.rs`), which this
//! test polls for; the kill is delivered only once that marker is observed,
//! never after a fixed sleep guess.

mod support;

use std::time::Duration;

use support::SeedOptions;

const CHUNK_SIZE: u32 = 100;
const ROWS: u64 = 550; // 5 full chunks + one 50-row partial
const TARGET_CHUNK: u32 = 3; // covers rows (200, 300]
const PREVIOUS_COMMITTED_ROWS: i64 = 200; // chunks 1-2
const MARKER_TIMEOUT: Duration = Duration::from_secs(30);
const ROLLBACK_SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

async fn hard_crash_before_commit_case(reader_mode: &str, size_flag: (&str, usize)) {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: ROWS,
        seed: 43,
    });
    let import_name = support::unique_name(&format!("crash_before_{reader_mode}"));
    let marker = support::temp_marker(&format!("before-{reader_mode}"));

    let mut child = support::spawn_run_with_failpoint(
        reader_mode,
        &import_name,
        CHUNK_SIZE,
        size_flag,
        TARGET_CHUNK,
        "during-write",
        &marker,
    );
    let spawned_pid = child.id();

    // Proof the target chunk actually reached its business-write phase
    // before anything is killed: the failpoint only writes the marker file
    // (with its own PID) after the real INSERT has executed inside the
    // still-open enlisted transaction.
    let paused_pid = support::wait_for_marker(&marker, MARKER_TIMEOUT);
    assert_eq!(
        paused_pid, spawned_pid,
        "the process observed pausing via the marker file must be this exact spawned child"
    );

    let exit_status = support::kill_and_wait(&mut child);
    assert!(
        !exit_status.success(),
        "a killed child must not exit successfully"
    );
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        assert_eq!(
            exit_status.signal(),
            Some(9),
            "must be a real SIGKILL, not a graceful exit"
        );
    }

    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest");

    let committed = support::wait_for_row_count(
        &pool,
        &import_name,
        &source_digest,
        PREVIOUS_COMMITTED_ROWS,
        ROLLBACK_SETTLE_TIMEOUT,
    )
    .await;
    assert_eq!(
        committed, PREVIOUS_COMMITTED_ROWS,
        "the killed chunk's in-flight, uncommitted business writes must not be durable"
    );

    let (status, commit_count, position) = support::latest_checkpoint(&pool, &import_name)
        .await
        .expect("an execution row exists even though the process never reported a terminal status");
    assert!(
        matches!(status.as_str(), "STARTED" | "STARTING" | "UNKNOWN"),
        "a process that died mid-flight cannot itself have persisted a terminal status, got {status}"
    );
    assert_eq!(
        commit_count, 2,
        "only the two previously committed chunks are durable"
    );
    assert_eq!(
        position, PREVIOUS_COMMITTED_ROWS,
        "the durable checkpoint must not have advanced into the killed chunk"
    );

    // The framework itself refuses a plain relaunch against an execution
    // still recorded as in-progress -- proven, not assumed.
    let plain_restart = support::run_plain(reader_mode, &import_name, CHUNK_SIZE, size_flag);
    assert!(
        !plain_restart.status.success(),
        "restart without an explicit recover must be rejected"
    );

    let recover_output = support::recover(&import_name, reader_mode);
    assert!(
        recover_output.status.success(),
        "recover must succeed against a genuinely crashed, still-in-progress execution: \
         stdout={}\nstderr={}",
        String::from_utf8_lossy(&recover_output.stdout),
        String::from_utf8_lossy(&recover_output.stderr),
    );

    let executions_before_restart = support::job_execution_count(&pool, &import_name).await;

    // Continuation runs in a genuinely new child process -- a fresh `spawn`
    // through `bin()`, never any state from the killed process above (which
    // no longer exists).
    let restart = match reader_mode {
        "cursor" => support::run_cursor_with_fetch_size(&import_name, CHUNK_SIZE, size_flag.1),
        "paging" => support::run_paging_with_page_size(&import_name, CHUNK_SIZE, size_flag.1),
        other => panic!("unknown reader mode {other}"),
    };
    assert!(
        restart.status.success(),
        "restart after recovery must complete the job"
    );

    let executions_after_restart = support::job_execution_count(&pool, &import_name).await;
    assert!(
        executions_after_restart > executions_before_restart,
        "recovery continuation is recorded as a new execution lifecycle, not an overwrite of \
         the crashed attempt's own history"
    );

    let final_rows = support::destination_row_count(&pool, &import_name, &source_digest).await;
    assert_eq!(
        final_rows, ROWS as i64,
        "final business state has exactly the expected row count: no missing, no extra, no \
         duplicate"
    );
    let verify = support::verify(&import_name);
    assert!(
        verify.status.success(),
        "independent verifier must pass against the recovered final state: stdout={}\nstderr={}",
        String::from_utf8_lossy(&verify.stdout),
        String::from_utf8_lossy(&verify.stderr),
    );
}

#[tokio::test]
async fn cursor_hard_crash_before_commit_recovers_in_a_new_process() {
    hard_crash_before_commit_case("cursor", ("--fetch-size", 50)).await;
}

#[tokio::test]
async fn paging_hard_crash_before_commit_recovers_in_a_new_process() {
    hard_crash_before_commit_case("paging", ("--page-size", 60)).await;
}
