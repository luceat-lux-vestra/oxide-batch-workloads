//! T4, T5, T6, T8: the crash-window matrix (spec ss19-20) and clean-vs-
//! recovered equivalence (ss27). Every "crash" here is the compiled
//! `csv-postgres` binary run as a real child process; a `--hard-crash`
//! failpoint calls `std::process::abort()` inside that child, a genuine
//! process death (SIGABRT), never a same-process object recreated in the
//! test itself (ss21). Restart always launches a brand-new child process.

mod support;

use support::{
    business_row_count_in_range, content_digest_in_range, latest_execution_status, GenerateOptions,
};

const CHUNK_SIZE: &str = "100"; // 500 rows / 100 => 5 chunks

fn run_with_failpoint(
    input: &std::path::Path,
    import_name: &str,
    fail_at: &str,
    failure_mode: &str,
    hard_crash: bool,
) -> std::process::ExitStatus {
    let mut cmd = support::bin();
    cmd.arg("run")
        .arg("--input")
        .arg(input)
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(CHUNK_SIZE)
        .arg("--fail-at")
        .arg(fail_at)
        .arg("--failure-mode")
        .arg(failure_mode);
    if hard_crash {
        cmd.arg("--hard-crash");
    }
    cmd.status().expect("spawn csv-postgres")
}

fn restart(input: &std::path::Path, import_name: &str) {
    support::run_ok(
        support::bin()
            .arg("run")
            .arg("--input")
            .arg(input)
            .arg("--import-name")
            .arg(import_name)
            .arg("--chunk-size")
            .arg(CHUNK_SIZE),
    );
}

/// T4: a graceful (non-crash) failure before a chunk's business commit.
/// The launcher's own future completes normally and persists a terminal
/// FAILED status, so -- unlike a hard crash -- a plain restart works
/// without an explicit `recover` step first (documented here as observed
/// behavior, not assumed).
#[tokio::test]
async fn graceful_failure_before_commit_reaches_a_terminal_status_and_restarts_without_recover() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 500,
        label: "graceful-before-write",
        ..Default::default()
    });
    let import_name = support::unique_name("graceful_before_write");

    let status = run_with_failpoint(
        &dataset.path,
        &import_name,
        "chunk:3",
        "before-write",
        false,
    );
    assert!(
        !status.success(),
        "a failed job's process should not exit 0"
    );

    let pool = support::pool().await;
    let (status_after_failure, _) = latest_execution_status(&pool, &import_name)
        .await
        .expect("job execution recorded");
    assert_eq!(
        status_after_failure, "FAILED",
        "graceful component failure reaches a terminal status"
    );

    let committed_before_restart =
        business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        committed_before_restart, 200,
        "chunks 1-2 committed (100 rows each), chunk 3 never did"
    );

    // No `recover` call here: a graceful failure must not require one.
    restart(&dataset.path, &import_name);

    let (final_status, _) = latest_execution_status(&pool, &import_name)
        .await
        .expect("execution recorded");
    assert_eq!(final_status, "COMPLETED");
    let final_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        final_rows, dataset.rows as i64,
        "restart resumed and completed the remainder exactly once"
    );
}

/// T5/F3: a hard crash after the writer's real INSERT executes but before
/// the chunk transaction commits. PostgreSQL rolls back the abandoned
/// transaction on connection loss, so this chunk's business rows must not
/// be durable at all.
#[tokio::test]
async fn hard_crash_during_write_before_commit_leaves_zero_partial_rows_and_restarts_cleanly() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 500,
        label: "hardcrash-during-write",
        ..Default::default()
    });
    let import_name = support::unique_name("hardcrash_during_write");

    let status = run_with_failpoint(&dataset.path, &import_name, "chunk:3", "during-write", true);
    assert!(
        !status.success(),
        "an aborted child process must not exit successfully"
    );
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        assert_eq!(
            status.signal(),
            Some(6),
            "SIGABRT (6) from std::process::abort()"
        );
    }

    let pool = support::pool().await;
    let committed = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        committed, 200,
        "chunk 3's uncommitted INSERT must not be durable after the crash"
    );

    let (status_after_crash, exit_code) = latest_execution_status(&pool, &import_name)
        .await
        .expect("execution row exists even though the process never reported a terminal status");
    assert!(
        matches!(status_after_crash.as_str(), "STARTED" | "STARTING" | "UNKNOWN"),
        "a process that died mid-flight cannot itself have persisted a terminal status, got {status_after_crash}/{exit_code}"
    );

    // The framework itself refuses a plain relaunch against an execution
    // still recorded as in-progress.
    let plain_restart = support::bin()
        .arg("run")
        .arg("--input")
        .arg(&dataset.path)
        .arg("--import-name")
        .arg(&import_name)
        .arg("--chunk-size")
        .arg(CHUNK_SIZE)
        .output()
        .expect("spawn csv-postgres");
    assert!(
        !plain_restart.status.success(),
        "restart without recover must be rejected"
    );

    support::recover(&import_name, &dataset.path);
    restart(&dataset.path, &import_name);

    let final_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(final_rows, dataset.rows as i64);
}

/// T5/F4/F6: the highest-stakes crash window -- the process is aborted
/// immediately after the chunk transaction's commit call returns success.
/// No assumption is made in advance about whether the committed chunk gets
/// reprocessed on restart (spec ss20/F4); this test observes and records
/// the real outcome via `committed_read`/`committed_written` on the restart
/// attempt.
#[tokio::test]
async fn hard_crash_immediately_after_business_commit_then_restart() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 500,
        label: "hardcrash-after-commit",
        ..Default::default()
    });
    let import_name = support::unique_name("hardcrash_after_commit");

    let status = run_with_failpoint(
        &dataset.path,
        &import_name,
        "chunk:3",
        "after-business-commit",
        true,
    );
    assert!(!status.success());

    let pool = support::pool().await;
    let committed_before_restart =
        business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        committed_before_restart, 300,
        "chunks 1-3 (including the one the crash fired right after) are durably committed"
    );

    support::recover(&import_name, &dataset.path);
    let restart_output = support::run_ok(
        support::bin()
            .arg("run")
            .arg("--input")
            .arg(&dataset.path)
            .arg("--import-name")
            .arg(&import_name)
            .arg("--chunk-size")
            .arg(CHUNK_SIZE),
    );
    let restart_stderr = String::from_utf8_lossy(&restart_output.stderr);
    eprintln!("restart evidence: {restart_stderr}");

    let final_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(
        final_rows, dataset.rows as i64,
        "final business state has no duplicates regardless of whether chunk 3 was reprocessed (Claim B)"
    );
}

/// T6: a real OS-level process crash (`std::process::abort()`, SIGABRT) and
/// restart in a brand-new process, run end to end through the CLI a second
/// time (this is the same mechanism as the two tests above; kept as its own
/// scenario per spec ss52/T6 for direct traceability in the report).
#[tokio::test]
async fn real_process_crash_and_restart_produce_a_completed_job() {
    support::migrate();
    let dataset = support::generate(GenerateOptions {
        rows: 300,
        label: "process-crash",
        ..Default::default()
    });
    let import_name = support::unique_name("process_crash");

    let status = run_with_failpoint(
        &dataset.path,
        &import_name,
        "chunk:2",
        "after-business-commit",
        true,
    );
    assert!(!status.success());

    support::recover(&import_name, &dataset.path);
    restart(&dataset.path, &import_name);

    let pool = support::pool().await;
    let (final_status, _) = latest_execution_status(&pool, &import_name).await.unwrap();
    assert_eq!(final_status, "COMPLETED");
    let final_rows = business_row_count_in_range(&pool, dataset.id_offset, dataset.rows).await;
    assert_eq!(final_rows, dataset.rows as i64);
}

/// T8: a clean run and a crash+recover+restart run of the *same* generated
/// content (same seed, independent id_offsets since both share one
/// business table) converge to an identical content digest -- not just an
/// identical row count.
#[tokio::test]
async fn clean_run_and_recovered_run_converge_to_the_same_content() {
    support::migrate();

    let clean = support::generate(GenerateOptions {
        rows: 400,
        seed: 777,
        label: "equivalence-clean",
        ..Default::default()
    });
    let clean_import_name = support::unique_name("equivalence_clean");
    restart(&clean.path, &clean_import_name); // plain clean run, no failpoint

    let recovered = support::generate(GenerateOptions {
        rows: 400,
        seed: 777, // same content pattern as `clean`, different id_offset
        label: "equivalence-recovered",
        ..Default::default()
    });
    let recovered_import_name = support::unique_name("equivalence_recovered");
    let status = run_with_failpoint(
        &recovered.path,
        &recovered_import_name,
        "chunk:3",
        "after-business-commit",
        true,
    );
    assert!(!status.success());
    support::recover(&recovered_import_name, &recovered.path);
    restart(&recovered.path, &recovered_import_name);

    let pool = support::pool().await;
    let clean_digest = content_digest_in_range(&pool, clean.id_offset, clean.rows).await;
    let recovered_digest =
        content_digest_in_range(&pool, recovered.id_offset, recovered.rows).await;
    assert_eq!(
        clean_digest, recovered_digest,
        "identical source content must converge to identical final business content, crash or not"
    );
}
