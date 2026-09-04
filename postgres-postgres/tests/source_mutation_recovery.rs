//! Campaign #63 PR 3: source-mutation / stale-checkpoint isolation across a
//! real process crash. A crashed, non-terminal execution is keyed under
//! source content A's digest; once the source is mutated to content B
//! (after the run/lock window has already closed -- see
//! `src/source_digest.rs`), B's digest must differ from A's, `recover`
//! against the same `(import_name, reader_mode)` must fail to find A's
//! instance under B's *live* digest rather than silently resuming it, and a
//! fresh `run` against the mutated source must launch as a genuinely new,
//! independent `JobInstance` -- never a silent reinterpretation of A's
//! stale checkpoint against B's different content.
//!
//! This test never deletes or edits the crashed instance's own
//! `oxide_batch` metadata rows to engineer its result: the isolation comes
//! entirely from the framework's own identity semantics, observed through
//! public commands and read-only SQL.

mod support;

use std::time::Duration;

use support::SeedOptions;

const CHUNK_SIZE: u32 = 100;
const ROWS: u64 = 550;
const TARGET_CHUNK: u32 = 3;
const PREVIOUS_COMMITTED_ROWS: i64 = 200;
const MARKER_TIMEOUT: Duration = Duration::from_secs(30);
const SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

#[tokio::test]
async fn mutated_source_after_a_crash_is_not_silently_resumed_by_recover_or_restart() {
    support::migrate();
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: ROWS,
        seed: 45,
    });
    let pool = support::pool().await;

    let digest_a = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest A");

    let import_name = support::unique_name("source_mutation");
    let marker = support::temp_marker("source-mutation");

    // Crash mid-run, leaving a non-terminal execution keyed under digest A.
    let mut child = support::spawn_run_with_failpoint(
        "cursor",
        &import_name,
        CHUNK_SIZE,
        ("--fetch-size", 50),
        TARGET_CHUNK,
        "during-write",
        &marker,
    );
    let spawned_pid = child.id();
    let paused_pid = support::wait_for_marker(&marker, MARKER_TIMEOUT);
    assert_eq!(paused_pid, spawned_pid);
    let exit_status = support::kill_and_wait(&mut child);
    assert!(!exit_status.success());

    let committed_a = support::wait_for_row_count(
        &pool,
        &import_name,
        &digest_a,
        PREVIOUS_COMMITTED_ROWS,
        SETTLE_TIMEOUT,
    )
    .await;
    assert_eq!(committed_a, PREVIOUS_COMMITTED_ROWS);

    let (status_a, _, _) = support::latest_checkpoint(&pool, &import_name)
        .await
        .expect("crashed execution recorded");
    assert!(matches!(
        status_a.as_str(),
        "STARTED" | "STARTING" | "UNKNOWN"
    ));

    let instances_before_mutation = support::job_instance_count(&pool, &import_name).await;
    assert_eq!(instances_before_mutation, 1);

    // The run/lock window closed when the crashed process died (the guard
    // transaction's connection dropped, releasing `LOCK TABLE ... IN SHARE
    // MODE` immediately -- see src/source_digest.rs). Mutate one row's
    // business content directly (never row count) so a fresh digest
    // computation genuinely observes different content, not just a
    // different row count.
    let mutated_customer_id = dataset.id_offset as i64 + 1;
    sqlx::query(
        "UPDATE app_source.source_customer SET balance_cents = balance_cents + 1 \
         WHERE customer_id = $1",
    )
    .bind(mutated_customer_id)
    .execute(&pool)
    .await
    .expect("mutate one source row");

    let digest_b = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest B");
    assert_ne!(
        digest_a, digest_b,
        "a mutated business field must change the source content identity"
    );

    // `recover` recomputes the digest live -- it must fail to find any
    // instance under the *current* (mutated) identity; it must never fall
    // back to "the only crashed instance for this import_name" by ignoring
    // source content.
    let recover_output = support::recover(&import_name, "cursor");
    assert!(
        !recover_output.status.success(),
        "recover must refuse to resolve an instance once the source has changed underneath it: \
         stdout={}\nstderr={}",
        String::from_utf8_lossy(&recover_output.stdout),
        String::from_utf8_lossy(&recover_output.stderr),
    );

    // A plain restart is likewise refused: the mutated content resolves to
    // a *different* JobInstanceKey than the crashed (still in-progress)
    // instance, so the framework's own duplicate-in-progress-execution
    // guard for THAT key does not apply -- but the crashed instance A is
    // still unresolved, so the run above launches as instance B rather than
    // resuming A. Confirmed below via job_instance_count and via A's own
    // destination scope being left completely untouched.
    let fresh_run = support::run_cursor_with_fetch_size(&import_name, CHUNK_SIZE, 50);
    assert!(
        fresh_run.status.success(),
        "a run against the same import_name but genuinely different source content must launch \
         as a new instance, not error out: stdout={}\nstderr={}",
        String::from_utf8_lossy(&fresh_run.stdout),
        String::from_utf8_lossy(&fresh_run.stderr),
    );

    let instances_after_mutation = support::job_instance_count(&pool, &import_name).await;
    assert_eq!(
        instances_after_mutation, 2,
        "the mutated-content run resolved to a second, distinct JobInstance -- never a resume of \
         instance A's stale checkpoint"
    );

    let rows_under_b = support::destination_row_count(&pool, &import_name, &digest_b).await;
    assert_eq!(
        rows_under_b, ROWS as i64,
        "instance B processed its own full current source content from scratch, not merely the \
         remainder A's stale checkpoint would imply"
    );
    let verify_b = support::verify(&import_name);
    assert!(
        verify_b.status.success(),
        "instance B's business state must verify cleanly"
    );

    // Instance A's own (pre-crash) destination scope is completely
    // untouched by any of the above: still exactly its 2 committed chunks,
    // never merged with, overwritten by, or silently reinterpreted as B's
    // content.
    let rows_under_a_after = support::destination_row_count(&pool, &import_name, &digest_a).await;
    assert_eq!(
        rows_under_a_after, PREVIOUS_COMMITTED_ROWS,
        "instance A's own destination scope must remain exactly what was durably committed \
         before the crash -- untouched by the unrelated instance B"
    );
}
