//! Configuration negative cases (campaign #63, PR 2): a mode-incompatible
//! option or an invalid mode-specific size must fail the run outright --
//! never be silently ignored, and never be reported as a completed
//! workload run. Every case here asserts both a nonzero process exit *and*
//! that no `oxide_batch` job execution was ever recorded for the attempt
//! (see `src/job.rs::run`'s doc comment: the mode-incompatible check runs
//! before any database connection is opened at all, and the released
//! paging reader's own zero-page-size validation runs before
//! `JobLauncher::launch_chunk` is ever called -- so a rejected
//! configuration never reaches, let alone completes, the production launch
//! path).

mod support;

use std::process::Output;

use support::SeedOptions;

fn run_raw(args: &[&str]) -> Output {
    support::bin()
        .arg("run")
        .args(args)
        .output()
        .expect("spawn postgres-postgres run")
}

#[tokio::test]
async fn page_size_is_rejected_under_cursor_mode() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions { rows: 20, seed: 1 });
    let import_name = support::unique_name("reject_page_size_under_cursor");

    let output = run_raw(&[
        "--import-name",
        &import_name,
        "--chunk-size",
        "10",
        "--reader",
        "cursor",
        "--page-size",
        "100",
    ]);
    assert!(
        !output.status.success(),
        "--page-size under --reader cursor must fail, not be silently ignored"
    );

    let pool = support::pool().await;
    assert!(
        support::latest_execution_status(&pool, &import_name)
            .await
            .is_none(),
        "a rejected configuration must never be recorded as any kind of job execution, \
         completed or otherwise"
    );
}

#[tokio::test]
async fn fetch_size_is_rejected_under_paging_mode() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions { rows: 20, seed: 2 });
    let import_name = support::unique_name("reject_fetch_size_under_paging");

    let output = run_raw(&[
        "--import-name",
        &import_name,
        "--chunk-size",
        "10",
        "--reader",
        "paging",
        "--fetch-size",
        "100",
    ]);
    assert!(
        !output.status.success(),
        "--fetch-size under --reader paging must fail, not be silently ignored"
    );

    let pool = support::pool().await;
    assert!(
        support::latest_execution_status(&pool, &import_name)
            .await
            .is_none(),
        "a rejected configuration must never be recorded as any kind of job execution, \
         completed or otherwise"
    );
}

#[tokio::test]
async fn zero_page_size_is_rejected() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions { rows: 20, seed: 3 });
    let import_name = support::unique_name("reject_zero_page_size");

    let output = run_raw(&[
        "--import-name",
        &import_name,
        "--chunk-size",
        "10",
        "--reader",
        "paging",
        "--page-size",
        "0",
    ]);
    assert!(
        !output.status.success(),
        "--page-size 0 must fail -- the released postgres_paging_reader's own construction-time \
         validation rejects a zero page size"
    );

    let pool = support::pool().await;
    assert!(
        support::latest_execution_status(&pool, &import_name)
            .await
            .is_none(),
        "a rejected configuration must never be recorded as any kind of job execution, \
         completed or otherwise"
    );
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest");
    let destination_rows =
        support::destination_row_count(&pool, &import_name, &source_digest).await;
    assert_eq!(
        destination_rows, 0,
        "a rejected configuration must never write any destination rows"
    );
}

#[tokio::test]
async fn missing_reader_flag_is_rejected_by_the_cli() {
    // `--reader` is a required clap argument with no default_value: clap
    // itself must reject an invocation missing it, before postgres-postgres
    // ever touches a database connection -- so this test needs no
    // migrate/seed at all.
    let output = support::bin()
        .arg("run")
        .arg("--import-name")
        .arg("missing_reader_flag_test")
        .output()
        .expect("spawn postgres-postgres run");
    assert!(
        !output.status.success(),
        "omitting --reader entirely must fail (clap usage error), never silently pick a reader mode"
    );
}
