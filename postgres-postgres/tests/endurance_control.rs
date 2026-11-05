#![cfg(any(target_os = "linux", target_os = "android"))]

mod support;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use postgres_postgres::endurance::campaign::{
    execute_acceptance_campaign_with_policy, CampaignAcceptancePolicy, CampaignStopReason,
};
use postgres_postgres::endurance::evidence::EvidenceStore;
use postgres_postgres::endurance::{plan_iteration, EnduranceFailure, CANONICAL_SEED};
use serde_json::{json, Value};
use support::SeedOptions;

const SAMPLE_INTERVAL: Duration = Duration::from_millis(10);

fn workload_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres"))
}

fn endurancectl_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres-endurancectl"))
}

fn temp_root(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "postgres-postgres-control-{label}-{}",
        support::nonce()
    ))
}

fn write_guard_script(dir: &Path, name: &str, body: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    fs::create_dir_all(dir).expect("create controller guard directory");
    let path = dir.join(name);
    fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write controller guard");
    let mut permissions = fs::metadata(&path)
        .expect("stat controller guard")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&path, permissions).expect("chmod controller guard");
    path
}

fn quoted(path: &Path) -> String {
    let text = path.to_string_lossy();
    assert!(
        !text.contains('\''),
        "test helper path must not contain apostrophe"
    );
    format!("'{text}'")
}

fn invocation_count(path: &Path) -> usize {
    match fs::read_to_string(path) {
        Ok(text) => text.lines().count(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => panic!("read controller guard invocation log: {error}"),
    }
}

fn write_success(store: &EvidenceStore, sequence: u64) {
    let record = json!({
        "schema": "oxide-batch-workloads.endurance-iteration",
        "schema_version": 1,
        "timestamp_unix_nanos_utc": 3_000_000_u64 + sequence,
        "campaign_id": store.campaign().campaign_id,
        "sequence": sequence,
        "terminal": "success",
        "execution": {
            "kind": "clean",
            "result": {
                "sequence": sequence
            }
        }
    });
    fs::write(
        store.iteration_path(sequence),
        serde_json::to_vec_pretty(&record).expect("serialize controller success"),
    )
    .expect("write controller success singleton");
    store
        .record_event(
            sequence,
            "iteration-terminal-success",
            json!({ "test": "endurance-control" }),
        )
        .expect("record controller terminal success");
}

fn iteration_kind(root: &Path, campaign_id: &str, sequence: u64) -> String {
    let path = root
        .join(campaign_id)
        .join(format!("iterations/{sequence:06}.json"));
    let bytes = fs::read(path).expect("read controller iteration evidence");
    let value: Value = serde_json::from_slice(&bytes).expect("parse controller iteration evidence");
    value
        .get("execution")
        .and_then(|execution| execution.get("kind"))
        .and_then(Value::as_str)
        .expect("iteration evidence has execution kind")
        .to_owned()
}

#[tokio::test]
async fn durable_origin_and_frontier_survive_controller_restart() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 500,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("restart-evidence");
    let helper = temp_root("restart-helper");
    let calls = helper.join("calls.log");
    let guard = write_guard_script(
        &helper,
        "allow-guard",
        &format!("printf 'allow\\n' >> {}; exit 0", quoted(&calls)),
    );
    let campaign_id = format!("controlrestart{}", support::nonce());
    let binary = workload_binary();
    let database_url = support::database_url();

    let first = execute_acceptance_campaign_with_policy(
        &binary,
        &database_url,
        &root,
        &campaign_id,
        SAMPLE_INTERVAL,
        &guard,
        CampaignAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 1,
        },
    )
    .await
    .expect("execute first controller invocation");

    let second = execute_acceptance_campaign_with_policy(
        &binary,
        &database_url,
        &root,
        &campaign_id,
        SAMPLE_INTERVAL,
        &guard,
        CampaignAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 2,
        },
    )
    .await
    .expect("execute restarted controller invocation");

    assert_eq!(
        first.stop_reason,
        CampaignStopReason::AcceptanceFloorsSatisfied
    );
    assert_eq!(
        second.stop_reason,
        CampaignStopReason::AcceptanceFloorsSatisfied
    );
    assert_eq!(
        first.campaign_started_at_unix_nanos_utc,
        second.campaign_started_at_unix_nanos_utc
    );
    assert_eq!(first.initial_frontier_sequence, 0);
    assert_eq!(first.final_frontier_sequence, 1);
    assert_eq!(second.initial_frontier_sequence, 1);
    assert_eq!(second.final_frontier_sequence, 2);
    assert_eq!(second.iterations_completed_by_invocation, 1);
    assert_eq!(invocation_count(&calls), 2);

    fs::remove_dir_all(root).expect("remove restart evidence");
    fs::remove_dir_all(helper).expect("remove restart helper");
}

#[tokio::test]
async fn satisfied_guard_block_and_resume_block_paths_are_fail_closed() {
    let binary = workload_binary();

    let satisfied_root = temp_root("already-satisfied");
    let satisfied_helper = temp_root("already-satisfied-helper");
    let satisfied_calls = satisfied_helper.join("calls.log");
    let satisfied_guard = write_guard_script(
        &satisfied_helper,
        "must-not-run",
        &format!("printf 'called\\n' >> {}; exit 0", quoted(&satisfied_calls)),
    );
    let satisfied_campaign = format!("controlsatisfied{}", support::nonce());
    let satisfied_store = EvidenceStore::open(&satisfied_root, &satisfied_campaign, &binary)
        .expect("initialize already-satisfied campaign");
    write_success(&satisfied_store, 1);
    drop(satisfied_store);

    let satisfied = execute_acceptance_campaign_with_policy(
        &binary,
        "postgres://must-not-connect.invalid/already-satisfied",
        &satisfied_root,
        &satisfied_campaign,
        SAMPLE_INTERVAL,
        &satisfied_guard,
        CampaignAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 1,
        },
    )
    .await
    .expect("already-satisfied controller should not execute");

    assert_eq!(
        satisfied.stop_reason,
        CampaignStopReason::AcceptanceFloorsSatisfied
    );
    assert_eq!(satisfied.iterations_completed_by_invocation, 0);
    assert_eq!(invocation_count(&satisfied_calls), 0);

    let guard_root = temp_root("guard-block");
    let guard_helper = temp_root("guard-block-helper");
    let guard = write_guard_script(&guard_helper, "block-guard", "exit 42");
    let guard_campaign = format!("controlguardblock{}", support::nonce());

    let guard_blocked = execute_acceptance_campaign_with_policy(
        &binary,
        "postgres://must-not-connect.invalid/guard-block",
        &guard_root,
        &guard_campaign,
        SAMPLE_INTERVAL,
        &guard,
        CampaignAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 1,
        },
    )
    .await
    .expect("guard block should be structured");

    assert_eq!(guard_blocked.stop_reason, CampaignStopReason::GuardBlocked);
    assert_eq!(guard_blocked.final_frontier_sequence, 0);

    let resume_root = temp_root("resume-block");
    let resume_helper = temp_root("resume-block-helper");
    let resume_calls = resume_helper.join("calls.log");
    let resume_guard = write_guard_script(
        &resume_helper,
        "must-not-run",
        &format!("printf 'called\\n' >> {}; exit 0", quoted(&resume_calls)),
    );
    let resume_campaign = format!("controlresumeblock{}", support::nonce());
    let resume_store = EvidenceStore::open(&resume_root, &resume_campaign, &binary)
        .expect("initialize resume-block campaign");
    resume_store
        .record_event(
            1,
            "iteration-terminal-failure",
            json!({ "failure_class": "harness-environment" }),
        )
        .expect("record blocking failure");
    drop(resume_store);

    let resume_blocked = execute_acceptance_campaign_with_policy(
        &binary,
        "postgres://must-not-connect.invalid/resume-block",
        &resume_root,
        &resume_campaign,
        SAMPLE_INTERVAL,
        &resume_guard,
        CampaignAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 1,
        },
    )
    .await
    .expect("resume block should be structured");

    assert_eq!(
        resume_blocked.stop_reason,
        CampaignStopReason::ResumeBlocked
    );
    assert_eq!(resume_blocked.final_frontier_sequence, 0);
    assert_eq!(invocation_count(&resume_calls), 0);

    fs::remove_dir_all(satisfied_root).expect("remove satisfied evidence");
    fs::remove_dir_all(satisfied_helper).expect("remove satisfied helper");
    fs::remove_dir_all(guard_root).expect("remove guard-block evidence");
    fs::remove_dir_all(guard_helper).expect("remove guard-block helper");
    fs::remove_dir_all(resume_root).expect("remove resume-block evidence");
    fs::remove_dir_all(resume_helper).expect("remove resume-block helper");
}

#[tokio::test]
async fn controller_reuses_existing_crash_schedule_at_boundary_24() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 3_500,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("crash-boundary");
    let helper = temp_root("crash-boundary-helper");
    let guard = write_guard_script(&helper, "allow-guard", "exit 0");
    let campaign_id = format!("controlboundary24{}", support::nonce());
    let binary = workload_binary();
    let store =
        EvidenceStore::open(&root, &campaign_id, &binary).expect("initialize boundary campaign");
    for sequence in 1..=23 {
        write_success(&store, sequence);
    }
    drop(store);

    let result = execute_acceptance_campaign_with_policy(
        &binary,
        &support::database_url(),
        &root,
        &campaign_id,
        SAMPLE_INTERVAL,
        &guard,
        CampaignAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 24,
        },
    )
    .await
    .expect("execute controller across crash boundary");

    assert_eq!(result.initial_frontier_sequence, 23);
    assert_eq!(result.final_frontier_sequence, 24);
    assert_eq!(result.iterations_completed_by_invocation, 1);

    let plan = plan_iteration(&campaign_id, 24).expect("resolve sequence 24 plan");
    assert_eq!(plan.failure, EnduranceFailure::BeforeCommit);
    assert_eq!(iteration_kind(&root, &campaign_id, 24), "crash");

    fs::remove_dir_all(root).expect("remove boundary evidence");
    fs::remove_dir_all(helper).expect("remove boundary helper");
}

#[test]
fn run_campaign_cli_exposes_no_sequence_or_floor_override() {
    let root = temp_root("cli");
    let base_args = [
        "run-campaign",
        "--database-url",
        "postgres://unused",
        "--evidence-root",
    ];

    for forbidden in [
        "--sequence",
        "--minimum-iterations",
        "--minimum-elapsed-seconds",
    ] {
        let output = Command::new(endurancectl_binary())
            .args(base_args)
            .arg(&root)
            .arg("--campaign-id")
            .arg("controlcli")
            .arg("--pre-iteration-guard")
            .arg("unused-guard")
            .arg(forbidden)
            .arg("1")
            .output()
            .expect("invoke campaign CLI with forbidden override");

        assert!(
            !output.status.success(),
            "run-campaign unexpectedly accepted {forbidden}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("unexpected argument"),
            "run-campaign must reject {forbidden}: {stderr}"
        );
    }
}
