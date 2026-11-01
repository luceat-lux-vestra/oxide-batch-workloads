//! Campaign #203 / #211 Slice B: execute one clean logical iteration
//! through real child processes, independently verify, then clean business
//! output only after verification succeeds.

mod support;

#[cfg(unix)]
use std::path::PathBuf;
use std::process::{Command, Output};

use postgres_postgres::endurance::{
    execute_clean_iteration, plan_iteration, CANONICAL_CHUNK_SIZE, CANONICAL_CURSOR_FETCH_SIZE,
    CANONICAL_PAGING_PAGE_SIZE,
};
use support::SeedOptions;

fn endurancectl() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_postgres-postgres-endurancectl"));
    command.env("DATABASE_URL", support::database_url());
    command
}

fn run_clean(campaign_id: &str, sequence: u64) -> Output {
    endurancectl()
        .arg("run-clean")
        .arg("--campaign-id")
        .arg(campaign_id)
        .arg("--sequence")
        .arg(sequence.to_string())
        .output()
        .expect("spawn postgres-postgres-endurancectl run-clean")
}

#[tokio::test]
async fn clean_control_surface_runs_cursor_and_paging_in_real_children() {
    support::migrate();
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: 137,
        seed: 20260904,
    });
    let campaign_id = format!("sliceb{}", support::nonce());

    for (sequence, reader) in [(1_u64, "cursor"), (2_u64, "paging")] {
        let output = run_clean(&campaign_id, sequence);
        assert!(
            output.status.success(),
            "run-clean failed: status={:?}\nstdout={}\nstderr={}",
            output.status,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );

        let result: serde_json::Value =
            serde_json::from_slice(&output.stdout).expect("run-clean prints one JSON result");
        assert_eq!(result["campaign_id"].as_str(), Some(campaign_id.as_str()));
        assert_eq!(result["sequence"], sequence);
        assert_eq!(result["reader"], reader);
        assert_eq!(result["chunk_size"], CANONICAL_CHUNK_SIZE);
        assert_eq!(result["run"]["success"], true);
        assert_eq!(result["run"]["code"], 0);
        assert_eq!(result["verification"]["success"], true);
        assert_eq!(result["verification"]["code"], 0);
        assert_ne!(
            result["run"]["pid"], result["verification"]["pid"],
            "run and independent verify must be separate OS child processes"
        );
        assert_eq!(result["cleanup_deleted_rows"], dataset.rows);

        match reader {
            "cursor" => {
                assert_eq!(result["fetch_size"], CANONICAL_CURSOR_FETCH_SIZE);
                assert!(result["page_size"].is_null());
            }
            "paging" => {
                assert!(result["fetch_size"].is_null());
                assert_eq!(result["page_size"], CANONICAL_PAGING_PAGE_SIZE);
            }
            other => panic!("unexpected reader {other}"),
        }

        let import_name = result["import_name"]
            .as_str()
            .expect("result contains import_name");
        let pool = support::pool().await;
        let source_digest = postgres_postgres::source_digest::compute(&pool)
            .await
            .expect("compute source digest after clean iteration");

        assert_eq!(
            support::destination_row_count(&pool, import_name, &source_digest).await,
            0,
            "verified business rows are cleaned after the iteration"
        );
        assert!(
            support::job_instance_count(&pool, import_name).await > 0,
            "framework metadata remains after business cleanup"
        );
        pool.close().await;
    }
}

#[test]
fn scheduled_crash_slots_fail_closed_before_execution() {
    let campaign_id = format!("slots{}", support::nonce());

    for (sequence, label) in [(24_u64, "before-commit"), (49_u64, "after-commit")] {
        let output = run_clean(&campaign_id, sequence);
        assert!(
            !output.status.success(),
            "scheduled crash slot {sequence} must not execute through the clean path"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("reserved for") && stderr.contains(label),
            "expected fail-closed crash-slot diagnostic for {label}, got: {stderr}"
        );
    }
}

#[cfg(unix)]
fn failing_verify_wrapper() -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    let actual = env!("CARGO_BIN_EXE_postgres-postgres");
    let path =
        std::env::temp_dir().join(format!("postgres-postgres-sliceb-{}.sh", support::nonce()));
    let script = format!(
        "#!/bin/sh\nif [ \"$1\" = \"verify\" ]; then\n  exit 73\nfi\nexec \"{actual}\" \"$@\"\n"
    );
    std::fs::write(&path, script).expect("write verify-failure wrapper");
    let mut permissions = std::fs::metadata(&path)
        .expect("stat verify-failure wrapper")
        .permissions();
    permissions.set_mode(0o700);
    std::fs::set_permissions(&path, permissions).expect("chmod verify-failure wrapper");
    path
}

#[cfg(unix)]
#[tokio::test]
async fn verification_failure_preserves_business_rows_for_diagnosis() {
    support::migrate();
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: 83,
        seed: 20260904,
    });
    let campaign_id = format!("verifyfail{}", support::nonce());
    let plan = plan_iteration(&campaign_id, 1).expect("sequence 1 is a clean cursor plan");
    let wrapper = failing_verify_wrapper();

    let result = execute_clean_iteration(&wrapper, &support::database_url(), &campaign_id, 1).await;
    let error = result.expect_err("forced verify failure must abort the clean iteration");
    assert!(
        error.to_string().contains("verify child failed"),
        "unexpected error: {error:#}"
    );

    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest after forced verify failure");
    assert_eq!(
        support::destination_row_count(&pool, &plan.import_name, &source_digest).await,
        dataset.rows as i64,
        "cleanup must not run when independent verification fails"
    );
    pool.close().await;

    std::fs::remove_file(wrapper).expect("remove verify-failure wrapper");
}
