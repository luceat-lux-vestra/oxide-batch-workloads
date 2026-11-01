#![cfg(unix)]

//! Campaign #203 / #213 Slice C: orchestrate one scheduled external SIGKILL,
//! public recovery, new-process continuation, independent verification, and
//! destination-only cleanup.

mod support;

use std::path::PathBuf;
use std::process::{Command, Output};

use postgres_postgres::endurance::crash::{execute_crash_iteration, CANONICAL_CRASH_CHUNK};
use postgres_postgres::endurance::{
    plan_iteration, CANONICAL_CHUNK_SIZE, CANONICAL_CURSOR_FETCH_SIZE, CANONICAL_PAGING_PAGE_SIZE,
};
use support::SeedOptions;

const ROWS: u64 = 3_500;

fn endurancectl() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_postgres-postgres-endurancectl"));
    command.env("DATABASE_URL", support::database_url());
    command
}

fn run_crash(campaign_id: &str, sequence: u64) -> Output {
    endurancectl()
        .arg("run-crash")
        .arg("--campaign-id")
        .arg(campaign_id)
        .arg("--sequence")
        .arg(sequence.to_string())
        .output()
        .expect("spawn postgres-postgres-endurancectl run-crash")
}

#[tokio::test]
async fn scheduled_crash_control_surface_recovers_both_boundaries() {
    support::migrate();
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: ROWS,
        seed: 20260904,
    });
    let campaign_id = format!("slicec{}", support::nonce());

    let cases = [
        (
            24_u64,
            "paging",
            "before-commit",
            "writer during-write",
            2_i64,
            2_000_i64,
        ),
        (
            49_u64,
            "cursor",
            "after-commit",
            "after-business-commit",
            3_i64,
            3_000_i64,
        ),
    ];

    for (sequence, reader, failure, marker_boundary, commits, position) in cases {
        let output = run_crash(&campaign_id, sequence);
        assert!(
            output.status.success(),
            "run-crash failed: status={:?}\nstdout={}\nstderr={}",
            output.status,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );

        let result: serde_json::Value =
            serde_json::from_slice(&output.stdout).expect("run-crash prints one JSON result");
        assert_eq!(result["campaign_id"].as_str(), Some(campaign_id.as_str()));
        assert_eq!(result["sequence"], sequence);
        assert_eq!(result["reader"], reader);
        assert_eq!(result["failure"], failure);
        assert_eq!(result["chunk_size"], CANONICAL_CHUNK_SIZE);
        assert_eq!(result["fail_at_chunk"], CANONICAL_CRASH_CHUNK);
        assert_eq!(result["crash"]["marker_boundary"], marker_boundary);
        assert_eq!(result["crash"]["pid"], result["crash"]["marker_pid"]);
        assert_eq!(result["crash"]["success"], false);
        assert_eq!(result["crash"]["signal"], 9);
        assert_eq!(result["crash"]["reaped"], true);
        assert_eq!(result["post_crash"]["commit_count"], commits);
        assert_eq!(result["post_crash"]["checkpoint_position"], position);
        assert_eq!(result["post_crash"]["business_rows"], position);
        assert_eq!(result["plain_relaunch"]["success"], false);
        assert_eq!(result["recovery"]["success"], true);
        assert_eq!(result["continuation"]["success"], true);
        assert_eq!(result["verification"]["success"], true);
        assert_ne!(result["crash"]["pid"], result["recovery"]["pid"]);
        assert_ne!(result["crash"]["pid"], result["continuation"]["pid"]);
        assert_ne!(result["crash"]["pid"], result["verification"]["pid"]);
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
            .expect("compute source digest after crash/recovery iteration");
        assert_eq!(
            support::destination_row_count(&pool, import_name, &source_digest).await,
            0,
            "verified business rows are cleaned only after the recovered iteration succeeds"
        );
        assert!(
            support::job_instance_count(&pool, import_name).await > 0,
            "framework metadata remains after business cleanup"
        );
        pool.close().await;
    }
}

#[test]
fn clean_sequences_fail_closed_on_crash_control_surface() {
    let campaign_id = format!("cleanreject{}", support::nonce());
    let output = run_crash(&campaign_id, 1);
    assert!(
        !output.status.success(),
        "a clean sequence must not execute through the crash path"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("sequence 1 is clean"),
        "expected fail-closed clean-sequence diagnostic, got: {stderr}"
    );
}

fn failing_phase_wrapper(phase: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    let actual = env!("CARGO_BIN_EXE_postgres-postgres");
    let path = std::env::temp_dir().join(format!(
        "postgres-postgres-slicec-{phase}-{}.sh",
        support::nonce()
    ));
    let script = match phase {
        "recover" => format!(
            "#!/bin/sh\nif [ \"$1\" = \"recover\" ]; then exit 71; fi\nexec \"{actual}\" \"$@\"\n"
        ),
        "continuation" => format!(
            "#!/bin/sh\nif [ \"$1\" = \"run\" ]; then\n  for arg in \"$@\"; do\n    if [ \"$arg\" = \"--pause-for-kill\" ]; then exec \"{actual}\" \"$@\"; fi\n  done\n  exit 74\nfi\nexec \"{actual}\" \"$@\"\n"
        ),
        "verify" => format!(
            "#!/bin/sh\nif [ \"$1\" = \"verify\" ]; then exit 73; fi\nexec \"{actual}\" \"$@\"\n"
        ),
        other => panic!("unknown forced-failure phase {other}"),
    };

    std::fs::write(&path, script).expect("write Slice C failure wrapper");
    let mut permissions = std::fs::metadata(&path)
        .expect("stat Slice C failure wrapper")
        .permissions();
    permissions.set_mode(0o700);
    std::fs::set_permissions(&path, permissions).expect("chmod Slice C failure wrapper");
    path
}

#[tokio::test]
async fn cleanup_is_deferred_until_recovery_continuation_and_verify_succeed() {
    for (phase, expected_rows, error_fragment) in [
        ("recover", 2_000_i64, "recover child failed"),
        ("continuation", 2_000_i64, "continuation child failed"),
        ("verify", ROWS as i64, "verify child failed"),
    ] {
        support::migrate();
        support::reset();
        support::seed(SeedOptions {
            rows: ROWS,
            seed: 20260904,
        });

        let campaign_id = format!("cleanupguard{phase}{}", support::nonce());
        let plan = plan_iteration(&campaign_id, 24).expect("sequence 24 is before-commit");
        let wrapper = failing_phase_wrapper(phase);

        let result =
            execute_crash_iteration(&wrapper, &support::database_url(), &campaign_id, 24).await;
        let error = result.expect_err("forced phase failure must abort Slice C");
        assert!(
            error.to_string().contains(error_fragment),
            "unexpected {phase} failure: {error:#}"
        );

        let pool = support::pool().await;
        let source_digest = postgres_postgres::source_digest::compute(&pool)
            .await
            .expect("compute source digest after forced Slice C failure");
        assert_eq!(
            support::destination_row_count(&pool, &plan.import_name, &source_digest).await,
            expected_rows,
            "cleanup must not run when {phase} fails"
        );
        pool.close().await;

        std::fs::remove_file(wrapper).expect("remove Slice C failure wrapper");
    }
}
