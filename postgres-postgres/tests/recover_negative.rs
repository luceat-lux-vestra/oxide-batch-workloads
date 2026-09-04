//! Campaign #63 PR 3: `recover` must fail closed (nonzero exit) on every
//! ambiguous or invalid selection, never silently pick *some* execution for
//! an import name. Exercised entirely through the public CLI against real
//! database state -- no `oxide_batch` metadata is mutated by this test.

mod support;

use std::time::Duration;

use support::SeedOptions;

#[tokio::test]
async fn recover_fails_closed_when_no_job_instance_exists() {
    support::migrate();
    let import_name = support::unique_name("recover_nonexistent");

    let output = support::recover(&import_name, "cursor");
    assert!(
        !output.status.success(),
        "recover must reject an import_name/reader that was never run"
    );
}

#[tokio::test]
async fn recover_fails_closed_against_an_already_completed_execution() {
    support::migrate();
    support::seed(SeedOptions { rows: 50, seed: 46 });
    let import_name = support::unique_name("recover_completed");

    let run_output = support::run_cursor_with_fetch_size(&import_name, 25, 10);
    assert!(run_output.status.success());

    let output = support::recover(&import_name, "cursor");
    assert!(
        !output.status.success(),
        "recover must reject an execution that already reached a terminal COMPLETED status, \
         not apply recovery speculatively: stdout={}\nstderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}

#[tokio::test]
async fn recover_fails_closed_against_the_wrong_reader_mode() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 550,
        seed: 47,
    });
    let import_name = support::unique_name("recover_wrong_mode");
    let marker = support::temp_marker("recover-wrong-mode");

    // Crash a cursor-mode run mid-flight, leaving a non-terminal execution
    // keyed under reader_mode = "cursor".
    let mut child = support::spawn_run_with_failpoint(
        "cursor",
        &import_name,
        100,
        ("--fetch-size", 50),
        3,
        "during-write",
        &marker,
    );
    let spawned_pid = child.id();
    let paused_pid = support::wait_for_marker(&marker, Duration::from_secs(30));
    assert_eq!(paused_pid, spawned_pid);
    let exit_status = support::kill_and_wait(&mut child);
    assert!(!exit_status.success());

    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest");
    support::wait_for_row_count(
        &pool,
        &import_name,
        &source_digest,
        200,
        Duration::from_secs(5),
    )
    .await;

    // reader_mode is its own identifying parameter (see src/job.rs::parameters):
    // recovering under "paging" must not resolve to the "cursor" instance.
    let output = support::recover(&import_name, "paging");
    assert!(
        !output.status.success(),
        "recover must not resolve a crashed cursor-mode instance when a different reader mode \
         is explicitly selected: stdout={}\nstderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );

    // The correct mode still recovers cleanly (sanity check that the
    // negative result above is about mode selection, not a broken fixture).
    let correct_recover = support::recover(&import_name, "cursor");
    assert!(correct_recover.status.success());
}
