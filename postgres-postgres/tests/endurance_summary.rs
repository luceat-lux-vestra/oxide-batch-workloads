#![cfg(any(target_os = "linux", target_os = "android"))]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use postgres_postgres::endurance::evidence::EvidenceStore;
use postgres_postgres::endurance::summary::{
    summarize_campaign_with_reconciliation_for_test, DatabaseReconciliation,
    RecoveryRequiredExecution, SummaryAcceptancePolicy, SummaryReasonCode, SummaryVerdict,
};
use postgres_postgres::endurance::{plan_iteration, EnduranceFailure};
use serde_json::{json, Value};

const CAMPAIGN_CREATED_AT: u64 = 1_000_000;

fn workload_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres"))
}

fn endurancectl_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres-endurancectl"))
}

fn temp_root(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "postgres-postgres-summary-{label}-{}-{}",
        std::process::id(),
        unique_nanos()
    ))
}

fn unique_nanos() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock after epoch")
        .as_nanos()
}

fn rewrite_campaign_start(store: &EvidenceStore) {
    let mut campaign = store.campaign().clone();
    campaign.created_at_unix_nanos_utc = CAMPAIGN_CREATED_AT;
    let path = store.campaign_dir().join("campaign.json");
    fs::write(
        path,
        serde_json::to_vec_pretty(&campaign).expect("serialize synthetic campaign"),
    )
    .expect("rewrite synthetic campaign start");
}

fn child(success: bool) -> Value {
    json!({
        "pid": 100,
        "success": success,
        "code": if success { Some(0) } else { Some(1) },
        "status": if success { "exit status: 0" } else { "exit status: 1" }
    })
}

fn write_success(store: &EvidenceStore, sequence: u64) {
    let plan = plan_iteration(&store.campaign().campaign_id, sequence)
        .expect("build deterministic summary fixture plan");

    let execution = match plan.failure {
        EnduranceFailure::Clean => json!({
            "kind": "clean",
            "result": {
                "campaign_id": plan.campaign_id,
                "sequence": sequence,
                "reader": plan.reader,
                "import_name": plan.import_name,
                "chunk_size": 1000,
                "fetch_size": if sequence % 2 == 1 { Some(500) } else { None::<usize> },
                "page_size": if sequence % 2 == 0 { Some(750) } else { None::<usize> },
                "run": child(true),
                "verification": child(true),
                "cleanup_deleted_rows": 200000
            }
        }),
        EnduranceFailure::BeforeCommit | EnduranceFailure::AfterCommit => json!({
            "kind": "crash",
            "result": {
                "campaign_id": plan.campaign_id,
                "sequence": sequence,
                "reader": plan.reader,
                "failure": plan.failure,
                "import_name": plan.import_name,
                "chunk_size": 1000,
                "fetch_size": if sequence % 2 == 1 { Some(500) } else { None::<usize> },
                "page_size": if sequence % 2 == 0 { Some(750) } else { None::<usize> },
                "fail_at_chunk": 3,
                "crash": {
                    "pid": 100,
                    "marker_pid": 100,
                    "marker_boundary": "synthetic",
                    "success": false,
                    "code": null,
                    "signal": 9,
                    "status": "signal: 9",
                    "reaped": true
                },
                "post_crash": {
                    "execution_status": "STARTED",
                    "execution_exit_code": "",
                    "commit_count": 2,
                    "checkpoint_position": 2000,
                    "business_rows": 2000
                },
                "plain_relaunch": child(false),
                "recovery": child(true),
                "continuation": child(true),
                "verification": child(true),
                "cleanup_deleted_rows": 200000
            }
        }),
    };

    let record = json!({
        "schema": "oxide-batch-workloads.endurance-iteration",
        "schema_version": 1,
        "timestamp_unix_nanos_utc": CAMPAIGN_CREATED_AT + 100 + sequence,
        "campaign_id": &store.campaign().campaign_id,
        "sequence": sequence,
        "terminal": "success",
        "execution": execution
    });
    fs::write(
        store.iteration_path(sequence),
        serde_json::to_vec_pretty(&record).expect("serialize synthetic iteration"),
    )
    .expect("write synthetic iteration");
    store
        .record_event(
            sequence,
            "iteration-terminal-success",
            json!({"fixture": "endurance-summary"}),
        )
        .expect("write synthetic terminal-success event");
}

fn write_samples(store: &EvidenceStore) {
    let samples = [
        json!({
            "schema": "oxide-batch-workloads.endurance-sample",
            "schema_version": 1,
            "timestamp_unix_nanos_utc": CAMPAIGN_CREATED_AT + 1,
            "campaign_id": &store.campaign().campaign_id,
            "sequence": 1,
            "sample_kind": "periodic",
            "sample_interval_millis": 10,
            "process": {
                "pid": 100,
                "role": "clean-workload",
                "rss_bytes": null,
                "cpu_time_ticks": null,
                "unavailable_reason": "process-exited-before-sample"
            },
            "postgres": {
                "total_connections": 2,
                "active_connections": 1,
                "database_size_bytes": 100
            },
            "oxide_batch": {
                "job_instances": 1,
                "job_executions": 1,
                "step_executions": 1,
                "job_instance_table_bytes": 10,
                "job_execution_table_bytes": 20,
                "step_execution_table_bytes": 30
            },
            "filesystem": {
                "total_bytes": 1000,
                "used_bytes": 100,
                "available_bytes": 900
            }
        }),
        json!({
            "schema": "oxide-batch-workloads.endurance-sample",
            "schema_version": 1,
            "timestamp_unix_nanos_utc": CAMPAIGN_CREATED_AT + 2,
            "campaign_id": &store.campaign().campaign_id,
            "sequence": 1,
            "sample_kind": "terminal-success",
            "sample_interval_millis": 10,
            "process": {
                "pid": 101,
                "role": "clean-workload",
                "rss_bytes": 4096,
                "cpu_time_ticks": 42,
                "unavailable_reason": null
            },
            "postgres": {
                "total_connections": 3,
                "active_connections": 2,
                "database_size_bytes": 120
            },
            "oxide_batch": {
                "job_instances": 49,
                "job_executions": 51,
                "step_executions": 51,
                "job_instance_table_bytes": 110,
                "job_execution_table_bytes": 220,
                "step_execution_table_bytes": 330
            },
            "filesystem": {
                "total_bytes": 1000,
                "used_bytes": 150,
                "available_bytes": 850
            }
        }),
    ];
    let text = samples
        .iter()
        .map(|sample| serde_json::to_string(sample).expect("serialize sample line"))
        .collect::<Vec<_>>()
        .join("\n");
    fs::write(store.samples_path(), format!("{text}\n")).expect("write synthetic samples");
}

fn success_fixture(label: &str, frontier: u64) -> (PathBuf, String) {
    let root = temp_root(label);
    let campaign_id = format!("summary{}{}", label.replace('-', ""), unique_nanos());
    let binary = workload_binary();
    let store = EvidenceStore::open(&root, &campaign_id, &binary)
        .expect("initialize synthetic summary campaign");
    rewrite_campaign_start(&store);
    for sequence in 1..=frontier {
        write_success(&store, sequence);
    }
    write_samples(&store);
    (root, campaign_id)
}

fn passing_database(frontier: u64) -> DatabaseReconciliation {
    DatabaseReconciliation {
        logical_identities_checked: frontier,
        execution_rows_observed: frontier,
        missing_logical_identities: Vec::new(),
        recovery_required_executions: Vec::new(),
    }
}

fn short_policy(frontier: u64) -> SummaryAcceptancePolicy {
    SummaryAcceptancePolicy {
        minimum_elapsed: Duration::from_nanos(10),
        minimum_completed_iterations: frontier,
        minimum_before_commit_crashes: if frontier >= 24 { 1 } else { 0 },
        minimum_after_commit_crashes: if frontier >= 49 { 1 } else { 0 },
    }
}

#[test]
fn accepted_fixture_reconciles_schedule_resources_and_atomic_summary() {
    let (root, campaign_id) = success_fixture("accepted", 49);
    let campaign_path = root.join(&campaign_id).join("campaign.json");
    let events_path = root.join(&campaign_id).join("events.jsonl");
    let iteration_path = root.join(&campaign_id).join("iterations/000024.json");
    let campaign_before = fs::read(&campaign_path).expect("read campaign before summary");
    let events_before = fs::read(&events_path).expect("read events before summary");
    let iteration_before = fs::read(&iteration_path).expect("read iteration before summary");

    let summary = summarize_campaign_with_reconciliation_for_test(
        &workload_binary(),
        &root,
        &campaign_id,
        short_policy(49),
        CAMPAIGN_CREATED_AT + 10,
        passing_database(49),
    )
    .expect("summarize accepted synthetic campaign");

    assert_eq!(summary.verdict, SummaryVerdict::Accepted);
    assert!(summary.reason_codes.is_empty());
    assert_eq!(summary.final_frontier_sequence, 49);
    assert_eq!(summary.executions.before_commit_crash, 1);
    assert_eq!(summary.executions.after_commit_crash, 1);
    assert_eq!(summary.lifecycle.terminal_success, 49);
    assert_eq!(summary.lifecycle.terminal_failure, 0);
    assert_eq!(summary.failures.total, 0);
    assert_eq!(summary.resources.sample_count, 2);
    assert_eq!(summary.resources.process_unavailable_observations, 1);
    assert_eq!(
        summary
            .resources
            .peak_rss_bytes_by_role
            .get("clean-workload"),
        Some(&4096)
    );
    assert_eq!(summary.resources.database_size_bytes.delta, Some(20));
    assert_eq!(summary.resources.filesystem_available_bytes.minimum, Some(850));
    assert_eq!(summary.campaign.oxide_batch_version, "0.6.0");

    let summary_path = root.join(&campaign_id).join("summary.json");
    assert!(summary_path.is_file());
    let persisted: Value = serde_json::from_slice(
        &fs::read(&summary_path).expect("read persisted summary"),
    )
    .expect("parse persisted summary");
    assert_eq!(persisted["verdict"], "accepted");

    assert_eq!(
        fs::read(&campaign_path).expect("read campaign after summary"),
        campaign_before
    );
    assert_eq!(
        fs::read(&events_path).expect("read events after summary"),
        events_before
    );
    assert_eq!(
        fs::read(&iteration_path).expect("read iteration after summary"),
        iteration_before
    );

    fs::remove_dir_all(root).expect("remove accepted summary fixture");
}

#[test]
fn floor_and_crash_count_failures_are_explicit_rejection_reasons() {
    let (root, campaign_id) = success_fixture("floors", 49);

    let time_rejected = summarize_campaign_with_reconciliation_for_test(
        &workload_binary(),
        &root,
        &campaign_id,
        SummaryAcceptancePolicy {
            minimum_elapsed: Duration::from_nanos(11),
            ..short_policy(49)
        },
        CAMPAIGN_CREATED_AT + 10,
        passing_database(49),
    )
    .expect("summarize below-time-floor fixture");
    assert!(time_rejected
        .reason_codes
        .contains(&SummaryReasonCode::ElapsedBelowMinimum));

    let count_rejected = summarize_campaign_with_reconciliation_for_test(
        &workload_binary(),
        &root,
        &campaign_id,
        SummaryAcceptancePolicy {
            minimum_completed_iterations: 50,
            ..short_policy(49)
        },
        CAMPAIGN_CREATED_AT + 10,
        passing_database(49),
    )
    .expect("summarize below-count-floor fixture");
    assert!(count_rejected
        .reason_codes
        .contains(&SummaryReasonCode::CompletedIterationsBelowMinimum));

    let crash_rejected = summarize_campaign_with_reconciliation_for_test(
        &workload_binary(),
        &root,
        &campaign_id,
        SummaryAcceptancePolicy {
            minimum_before_commit_crashes: 2,
            minimum_after_commit_crashes: 2,
            ..short_policy(49)
        },
        CAMPAIGN_CREATED_AT + 10,
        passing_database(49),
    )
    .expect("summarize insufficient-crash-count fixture");
    assert!(crash_rejected
        .reason_codes
        .contains(&SummaryReasonCode::BeforeCommitCrashCountBelowMinimum));
    assert!(crash_rejected
        .reason_codes
        .contains(&SummaryReasonCode::AfterCommitCrashCountBelowMinimum));

    fs::remove_dir_all(root).expect("remove floor summary fixture");
}

#[test]
fn semantic_schedule_mismatch_is_rejected_without_becoming_framework_attribution() {
    let (root, campaign_id) = success_fixture("semantic", 1);
    let path = root.join(&campaign_id).join("iterations/000001.json");
    let mut value: Value =
        serde_json::from_slice(&fs::read(&path).expect("read semantic fixture"))
            .expect("parse semantic fixture");
    value["execution"]["result"]["reader"] = json!("paging");
    fs::write(
        &path,
        serde_json::to_vec_pretty(&value).expect("serialize semantic mismatch"),
    )
    .expect("write semantic mismatch");

    let summary = summarize_campaign_with_reconciliation_for_test(
        &workload_binary(),
        &root,
        &campaign_id,
        short_policy(1),
        CAMPAIGN_CREATED_AT + 10,
        passing_database(1),
    )
    .expect("summarize semantic mismatch");

    assert_eq!(summary.verdict, SummaryVerdict::Rejected);
    assert!(summary
        .reason_codes
        .contains(&SummaryReasonCode::EvidenceSemanticMismatch));
    assert!(!summary.evidence_semantic_violations.is_empty());

    fs::remove_dir_all(root).expect("remove semantic summary fixture");
}

#[test]
fn durable_failure_and_gap_states_fail_closed() {
    let root = temp_root("failure");
    let campaign_id = format!("summaryfailure{}", unique_nanos());
    let binary = workload_binary();
    let store =
        EvidenceStore::open(&root, &campaign_id, &binary).expect("initialize failure fixture");
    rewrite_campaign_start(&store);
    write_samples(&store);
    let failure = json!({
        "schema": "oxide-batch-workloads.endurance-failure",
        "schema_version": 1,
        "timestamp_unix_nanos_utc": CAMPAIGN_CREATED_AT + 3,
        "campaign_id": &campaign_id,
        "sequence": 1,
        "terminal": "failure",
        "failure_class": "harness-environment",
        "error": "synthetic"
    });
    fs::write(
        root.join(&campaign_id).join("failures/000001-1000003.json"),
        serde_json::to_vec_pretty(&failure).expect("serialize failure fixture"),
    )
    .expect("write failure fixture");
    store
        .record_event(
            1,
            "iteration-terminal-failure",
            json!({"failure_class": "harness-environment"}),
        )
        .expect("write terminal failure event");
    drop(store);

    let summary = summarize_campaign_with_reconciliation_for_test(
        &binary,
        &root,
        &campaign_id,
        SummaryAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 1,
            minimum_before_commit_crashes: 0,
            minimum_after_commit_crashes: 0,
        },
        CAMPAIGN_CREATED_AT,
        passing_database(0),
    )
    .expect("summarize durable failure fixture");

    assert_eq!(summary.failures.harness_environment, 1);
    assert!(summary
        .reason_codes
        .contains(&SummaryReasonCode::UnexpectedTerminalFailureEvidence));
    assert!(summary
        .reason_codes
        .contains(&SummaryReasonCode::DurableResumeNotReady));

    fs::remove_dir_all(&root).expect("remove failure summary fixture");

    let gap_root = temp_root("gap");
    let gap_campaign = format!("summarygap{}", unique_nanos());
    let gap_store = EvidenceStore::open(&gap_root, &gap_campaign, &binary)
        .expect("initialize gap fixture");
    rewrite_campaign_start(&gap_store);
    write_success(&gap_store, 2);
    write_samples(&gap_store);
    drop(gap_store);

    let gap_summary = summarize_campaign_with_reconciliation_for_test(
        &binary,
        &gap_root,
        &gap_campaign,
        SummaryAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 1,
            minimum_before_commit_crashes: 0,
            minimum_after_commit_crashes: 0,
        },
        CAMPAIGN_CREATED_AT,
        passing_database(0),
    )
    .expect("summarize gap fixture");
    assert!(gap_summary
        .reason_codes
        .contains(&SummaryReasonCode::DurableResumeNotReady));
    assert_eq!(gap_summary.verdict, SummaryVerdict::Rejected);

    fs::remove_dir_all(gap_root).expect("remove gap summary fixture");
}

#[test]
fn database_reconciliation_rejects_missing_and_recovery_required_executions() {
    let (root, campaign_id) = success_fixture("database", 1);
    let database = DatabaseReconciliation {
        logical_identities_checked: 1,
        execution_rows_observed: 1,
        missing_logical_identities: vec!["endurance_missing".to_owned()],
        recovery_required_executions: vec![RecoveryRequiredExecution {
            execution_id: "execution-1".to_owned(),
            import_name: "endurance_started".to_owned(),
            status: "Started".to_owned(),
        }],
    };

    let summary = summarize_campaign_with_reconciliation_for_test(
        &workload_binary(),
        &root,
        &campaign_id,
        short_policy(1),
        CAMPAIGN_CREATED_AT + 10,
        database,
    )
    .expect("summarize database reconciliation failure");

    assert_eq!(summary.verdict, SummaryVerdict::Rejected);
    assert!(summary
        .reason_codes
        .contains(&SummaryReasonCode::MissingDatabaseExecutionHistory));
    assert!(summary
        .reason_codes
        .contains(&SummaryReasonCode::RecoveryRequiredExecutionsRemain));

    fs::remove_dir_all(root).expect("remove database summary fixture");
}

#[test]
fn summarize_campaign_cli_rejects_sequence_and_floor_overrides() {
    let root = temp_root("cli");
    for forbidden in [
        "--sequence",
        "--minimum-iterations",
        "--minimum-elapsed-seconds",
        "--minimum-before-commit-crashes",
        "--minimum-after-commit-crashes",
    ] {
        let output = Command::new(endurancectl_binary())
            .arg("summarize-campaign")
            .arg("--database-url")
            .arg("postgres://unused")
            .arg("--evidence-root")
            .arg(&root)
            .arg("--campaign-id")
            .arg("summarycli")
            .arg(forbidden)
            .arg("1")
            .output()
            .expect("invoke summary CLI with forbidden override");

        assert!(!output.status.success(), "{forbidden} unexpectedly accepted");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("unexpected argument"),
            "{forbidden} rejection missing clap diagnostic: {stderr}"
        );
    }
}
