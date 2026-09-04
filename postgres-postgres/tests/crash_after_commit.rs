//! Campaign #63 PR 3: the highest-stakes crash window -- a real `SIGKILL`
//! delivered immediately after a target chunk's atomic transaction commit
//! call returns success, before the next chunk produces any business
//! effect. Proves the committed chunk's business rows and checkpoint are
//! both durable together (never a split state), that recovery does not
//! redeliver (and thus duplicate) that already-committed chunk, and that
//! the final recovered business state is representation-identical to a
//! clean run over the identical source content. For both reader modes.

mod support;

use std::time::Duration;

use support::SeedOptions;

const CHUNK_SIZE: u32 = 100;
const ROWS: u64 = 550; // 5 full chunks + one 50-row partial
const TARGET_CHUNK: u32 = 3;
const ROWS_AFTER_TARGET_COMMIT: i64 = 300; // chunks 1-3, including the targeted one
const MARKER_TIMEOUT: Duration = Duration::from_secs(30);
const SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

async fn hard_crash_after_commit_case(reader_mode: &str, size_flag: (&str, usize)) {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: ROWS,
        seed: 44,
    });
    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest");

    // Clean baseline over the identical source content (same digest, no
    // failpoint), for the clean-vs-recovered equivalence check below.
    let clean_import_name = support::unique_name(&format!("clean_baseline_{reader_mode}"));
    let clean_run = match reader_mode {
        "cursor" => {
            support::run_cursor_with_fetch_size(&clean_import_name, CHUNK_SIZE, size_flag.1)
        }
        "paging" => support::run_paging_with_page_size(&clean_import_name, CHUNK_SIZE, size_flag.1),
        other => panic!("unknown reader mode {other}"),
    };
    assert!(clean_run.status.success());
    let clean_digest =
        support::destination_content_digest(&pool, &clean_import_name, &source_digest).await;

    let import_name = support::unique_name(&format!("crash_after_{reader_mode}"));
    let marker = support::temp_marker(&format!("after-{reader_mode}"));

    let mut child = support::spawn_run_with_failpoint(
        reader_mode,
        &import_name,
        CHUNK_SIZE,
        size_flag,
        TARGET_CHUNK,
        "after-business-commit",
        &marker,
    );
    let spawned_pid = child.id();
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
        assert_eq!(exit_status.signal(), Some(9), "must be a real SIGKILL");
    }

    let committed = support::wait_for_row_count(
        &pool,
        &import_name,
        &source_digest,
        ROWS_AFTER_TARGET_COMMIT,
        SETTLE_TIMEOUT,
    )
    .await;
    assert_eq!(
        committed, ROWS_AFTER_TARGET_COMMIT,
        "the targeted chunk's already-committed business rows (and the chunks before it) must \
         be durable"
    );

    let (status, commit_count, position) = support::latest_checkpoint(&pool, &import_name)
        .await
        .expect("an execution row exists even though the process never reported a terminal status");
    assert!(
        matches!(status.as_str(), "STARTED" | "STARTING" | "UNKNOWN"),
        "a process that died mid-flight cannot itself have persisted a terminal status, got {status}"
    );
    assert_eq!(
        commit_count, 3,
        "the targeted chunk's own commit is durable"
    );
    assert_eq!(
        position, ROWS_AFTER_TARGET_COMMIT,
        "checkpoint and business rows are both durable together, in the same transaction -- no \
         split state where one advanced without the other"
    );

    let recover_output = support::recover(&import_name, reader_mode);
    assert!(
        recover_output.status.success(),
        "recover must succeed: stdout={}\nstderr={}",
        String::from_utf8_lossy(&recover_output.stdout),
        String::from_utf8_lossy(&recover_output.stderr),
    );

    let restart = match reader_mode {
        "cursor" => support::run_cursor_with_fetch_size(&import_name, CHUNK_SIZE, size_flag.1),
        "paging" => support::run_paging_with_page_size(&import_name, CHUNK_SIZE, size_flag.1),
        other => panic!("unknown reader mode {other}"),
    };
    assert!(
        restart.status.success(),
        "restart after recovery must complete the job"
    );

    let final_rows = support::destination_row_count(&pool, &import_name, &source_digest).await;
    assert_eq!(
        final_rows, ROWS as i64,
        "final row count has no duplicate: the already-committed target chunk was not \
         redelivered on top of itself (a real redelivery would either duplicate this count or \
         hit the destination primary key and fail the restart outright)"
    );
    let verify = support::verify(&import_name);
    assert!(
        verify.status.success(),
        "independent verifier must pass against the recovered final state: stdout={}\nstderr={}",
        String::from_utf8_lossy(&verify.stdout),
        String::from_utf8_lossy(&verify.stderr),
    );

    let recovered_digest =
        support::destination_content_digest(&pool, &import_name, &source_digest).await;
    assert_eq!(
        clean_digest, recovered_digest,
        "identical source content, crash-and-recovered or not: the full recovered business \
         projection (customer_id, display_name, loyalty_score, is_premium, row_fingerprint) must \
         be representation-identical to the clean run's, not merely the same row count"
    );
}

#[tokio::test]
async fn cursor_hard_crash_after_commit_recovers_without_duplication() {
    hard_crash_after_commit_case("cursor", ("--fetch-size", 50)).await;
}

#[tokio::test]
async fn paging_hard_crash_after_commit_recovers_without_duplication() {
    hard_crash_after_commit_case("paging", ("--page-size", 60)).await;
}
