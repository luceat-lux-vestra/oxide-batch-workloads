//! Campaign #63 PR 3: a typed failure after a target chunk's business
//! writes execute, but before that chunk's transaction commits, rolls the
//! *whole* chunk back -- not a partial write -- for both reader modes.
//! `tests/verifier_negative_control.rs` proves the verifier catches
//! corruption after the fact; this proves the production path itself never
//! durably writes a rolled-back chunk in the first place, by reading real
//! `app_business.customer_projection`/`oxide_batch` state after the failure,
//! never by trusting log output.

mod support;

use support::SeedOptions;

const CHUNK_SIZE: u32 = 100;
const ROWS: u64 = 550; // not evenly divisible by CHUNK_SIZE: 5 full chunks + one 50-row partial
const TARGET_CHUNK: u32 = 3; // covers rows (200, 300]
const PREVIOUS_COMMITTED_ROWS: i64 = 200; // chunks 1-2

async fn typed_rollback_case(reader_mode: &str, size_flag: (&str, usize)) {
    support::migrate();
    // Exact whole-table row/chunk-boundary math below requires exclusive
    // ownership of app_source/app_business for this test (see
    // tests/support/mod.rs::reset's doc comment).
    support::reset();
    support::seed(SeedOptions {
        rows: ROWS,
        seed: 42,
    });
    let import_name = support::unique_name(&format!("rollback_{reader_mode}"));

    let output = support::run_with_typed_failpoint(
        reader_mode,
        &import_name,
        CHUNK_SIZE,
        size_flag,
        TARGET_CHUNK,
        "during-write",
    );
    assert!(
        !output.status.success(),
        "a chunk that failed pre-commit must not exit 0: stdout={}\nstderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );

    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest");

    let committed = support::destination_row_count(&pool, &import_name, &source_digest).await;
    assert_eq!(
        committed, PREVIOUS_COMMITTED_ROWS,
        "only the two previously committed chunks are durable; the target chunk's writes are \
         fully rolled back, not partially present"
    );

    let (status, commit_count, position) = support::latest_checkpoint(&pool, &import_name)
        .await
        .expect("execution recorded even though it failed");
    assert_eq!(
        status, "FAILED",
        "a typed pre-commit failure reaches a terminal FAILED status"
    );
    assert_eq!(commit_count, 2, "only 2 chunk commits are durable");
    assert_eq!(
        position, PREVIOUS_COMMITTED_ROWS,
        "the durable checkpoint position matches the durable business row count exactly -- no \
         split state between the two"
    );

    // A graceful (non-crash) failure needs no explicit `recover` step: the
    // launcher's own future completed normally and persisted a terminal
    // status, so a plain restart resumes it.
    let restart = match reader_mode {
        "cursor" => support::run_cursor_with_fetch_size(&import_name, CHUNK_SIZE, size_flag.1),
        "paging" => support::run_paging_with_page_size(&import_name, CHUNK_SIZE, size_flag.1),
        other => panic!("unknown reader mode {other}"),
    };
    assert!(
        restart.status.success(),
        "restart after a graceful pre-commit failure must succeed"
    );

    let final_rows = support::destination_row_count(&pool, &import_name, &source_digest).await;
    assert_eq!(
        final_rows, ROWS as i64,
        "the restarted run completes the exact remainder, with no gap and no duplicate"
    );
    let verify = support::verify(&import_name);
    assert!(
        verify.status.success(),
        "recovered final state must verify cleanly: stdout={}\nstderr={}",
        String::from_utf8_lossy(&verify.stdout),
        String::from_utf8_lossy(&verify.stderr),
    );
}

#[tokio::test]
async fn cursor_typed_failure_rolls_back_target_chunk_only() {
    typed_rollback_case("cursor", ("--fetch-size", 50)).await;
}

#[tokio::test]
async fn paging_typed_failure_rolls_back_target_chunk_only() {
    typed_rollback_case("paging", ("--page-size", 60)).await;
}
