#![cfg(any(target_os = "linux", target_os = "android"))]

//! Campaign #203 / #224 Slice H: execute a finite batch of durable-resume-
//! selected sampled recorded logical iterations per invocation.

mod support;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use postgres_postgres::endurance::batch::execute_bounded_batch;
use postgres_postgres::endurance::evidence::EvidenceStore;
use postgres_postgres::endurance::resume::{reconcile_resume_status, ResumeState};
use postgres_postgres::endurance::{
    plan_iteration, EnduranceFailure, EnduranceReader, CANONICAL_SEED,
};
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
        "postgres-postgres-sliceh-{label}-{}",
        support::nonce()
    ))
}

fn initialize_campaign(root: &Path, campaign_id: &str, binary: &Path) {
    let store =
        EvidenceStore::open(root, campaign_id, binary).expect("initialize Slice H campaign");
    drop(store);
}

fn snapshot_tree(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    fn visit(root: &Path, path: &Path, output: &mut BTreeMap<PathBuf, Vec<u8>>) {
        let mut entries = fs::read_dir(path)
            .expect("read snapshot directory")
            .map(|entry| entry.expect("read snapshot entry"))
            .collect::<Vec<_>>();
        entries.sort_by_key(|entry| entry.file_name());

        for entry in entries {
            let path = entry.path();
            if path.is_dir() {
                visit(root, &path, output);
            } else {
                let relative = path
                    .strip_prefix(root)
                    .expect("snapshot path is below root")
                    .to_path_buf();
                output.insert(relative, fs::read(&path).expect("read snapshot file"));
            }
        }
    }

    let mut output = BTreeMap::new();
    visit(root, root, &mut output);
    output
}

fn write_success(store: &EvidenceStore, sequence: u64) {
    let record = json!({
        "schema": "oxide-batch-workloads.endurance-iteration",
        "schema_version": 1,
        "timestamp_unix_nanos_utc": 2_000_000_u64 + sequence,
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
        serde_json::to_vec_pretty(&record).expect("serialize Slice H success"),
    )
    .expect("write Slice H success singleton");
    store
        .record_event(
            sequence,
            "iteration-terminal-success",
            json!({ "test": "slice-h" }),
        )
        .expect("record Slice H terminal success");
}

fn iteration_kind(root: &Path, campaign_id: &str, sequence: u64) -> String {
    let path = root
        .join(campaign_id)
        .join(format!("iterations/{sequence:06}.json"));
    let bytes = fs::read(path).expect("read Slice H iteration evidence");
    let value: Value = serde_json::from_slice(&bytes).expect("parse Slice H iteration evidence");
    value
        .get("execution")
        .and_then(|execution| execution.get("kind"))
        .and_then(Value::as_str)
        .expect("iteration evidence has execution kind")
        .to_owned()
}

#[tokio::test]
async fn bounded_batches_advance_exactly_requested_sequences_and_resume_independently() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 500,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("progress");
    let campaign_id = format!("slicehprogress{}", support::nonce());
    let database_url = support::database_url();
    let binary = workload_binary();
    initialize_campaign(&root, &campaign_id, &binary);

    let first = execute_bounded_batch(
        &binary,
        &database_url,
        &root,
        &campaign_id,
        3,
        SAMPLE_INTERVAL,
    )
    .await
    .expect("execute first Slice H bounded batch");

    assert_eq!(first.requested_iterations, 3);
    assert_eq!(first.completed_iterations, 3);
    assert_eq!(first.starting_frontier_sequence, 0);
    assert_eq!(first.completed_sequences, vec![1, 2, 3]);
    assert_eq!(first.terminal_resume_status.state, ResumeState::Ready);
    assert_eq!(first.terminal_resume_status.frontier_sequence, 3);
    assert_eq!(first.terminal_resume_status.next_sequence, Some(4));
    assert!(!root
        .join(&campaign_id)
        .join("iterations/000004.json")
        .exists());

    let second = execute_bounded_batch(
        &binary,
        &database_url,
        &root,
        &campaign_id,
        2,
        SAMPLE_INTERVAL,
    )
    .await
    .expect("execute second independent Slice H bounded batch");

    assert_eq!(second.requested_iterations, 2);
    assert_eq!(second.completed_iterations, 2);
    assert_eq!(second.starting_frontier_sequence, 3);
    assert_eq!(second.completed_sequences, vec![4, 5]);
    assert_eq!(second.terminal_resume_status.state, ResumeState::Ready);
    assert_eq!(second.terminal_resume_status.frontier_sequence, 5);
    assert_eq!(second.terminal_resume_status.next_sequence, Some(6));
    assert!(!root
        .join(&campaign_id)
        .join("iterations/000006.json")
        .exists());

    let serialized = serde_json::to_string(&second).expect("serialize Slice H result");
    assert!(!serialized.contains(&database_url));
    assert!(!serialized.contains("oxide_batch_workload:oxide_batch_workload"));

    fs::remove_dir_all(root).expect("remove Slice H progression evidence");
}

#[tokio::test]
async fn bounded_batches_cross_both_scheduled_crash_boundaries_without_overrun() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 3_500,
        seed: CANONICAL_SEED,
    });

    let binary = workload_binary();
    let database_url = support::database_url();
    let cases = [
        (
            23_u64,
            [24_u64, 25_u64],
            24_u64,
            EnduranceReader::Paging,
            EnduranceFailure::BeforeCommit,
        ),
        (
            48_u64,
            [49_u64, 50_u64],
            49_u64,
            EnduranceReader::Cursor,
            EnduranceFailure::AfterCommit,
        ),
    ];

    for (frontier, expected_sequences, crash_sequence, reader, failure) in cases {
        let root = temp_root(&format!("boundary-{crash_sequence}"));
        let campaign_id = format!("slicehboundary{crash_sequence}{}", support::nonce());
        let store = EvidenceStore::open(&root, &campaign_id, &binary)
            .expect("initialize Slice H boundary campaign");
        for completed in 1..=frontier {
            write_success(&store, completed);
        }

        let result = execute_bounded_batch(
            &binary,
            &database_url,
            &root,
            &campaign_id,
            2,
            SAMPLE_INTERVAL,
        )
        .await
        .expect("execute Slice H boundary batch");

        assert_eq!(result.starting_frontier_sequence, frontier);
        assert_eq!(result.completed_iterations, 2);
        assert_eq!(result.completed_sequences, expected_sequences);
        assert_eq!(
            result.terminal_resume_status.frontier_sequence,
            expected_sequences[1]
        );
        assert_eq!(
            result.terminal_resume_status.next_sequence,
            Some(expected_sequences[1] + 1)
        );

        let plan =
            plan_iteration(&campaign_id, crash_sequence).expect("resolve scheduled crash plan");
        assert_eq!(plan.reader, reader);
        assert_eq!(plan.failure, failure);
        assert_eq!(iteration_kind(&root, &campaign_id, crash_sequence), "crash");
        assert!(!root
            .join(&campaign_id)
            .join(format!("iterations/{:06}.json", expected_sequences[1] + 1))
            .exists());

        fs::remove_dir_all(root).expect("remove Slice H boundary evidence");
    }
}

#[tokio::test]
async fn blocked_resume_states_stop_before_executor_or_database_work() {
    for case in ["failure", "incomplete", "inconsistent"] {
        let root = temp_root(case);
        let campaign_id = format!("sliceh{case}{}", support::nonce());
        let binary = workload_binary();
        let store =
            EvidenceStore::open(&root, &campaign_id, &binary).expect("initialize blocked campaign");

        match case {
            "failure" => store
                .record_event(
                    1,
                    "iteration-terminal-failure",
                    json!({ "failure_class": "harness-environment" }),
                )
                .expect("record blocked failure"),
            "incomplete" => store
                .record_event(1, "iteration-started", json!({ "mode": "clean" }))
                .expect("record incomplete lifecycle"),
            "inconsistent" => {
                store
                    .record_event(1, "iteration-terminal-success", json!({ "duplicate": 1 }))
                    .expect("record first duplicate terminal");
                store
                    .record_event(1, "iteration-terminal-success", json!({ "duplicate": 2 }))
                    .expect("record second duplicate terminal");
            }
            _ => unreachable!(),
        }

        let before = snapshot_tree(&root);
        let result = execute_bounded_batch(
            &binary,
            "postgres://must-not-connect.invalid/blocked",
            &root,
            &campaign_id,
            3,
            SAMPLE_INTERVAL,
        )
        .await
        .expect("blocked Slice H batch returns structured result");
        let after = snapshot_tree(&root);

        assert_eq!(result.requested_iterations, 3);
        assert_eq!(result.completed_iterations, 0);
        assert_eq!(result.starting_frontier_sequence, 0);
        assert!(result.completed_sequences.is_empty());
        assert_ne!(result.terminal_resume_status.state, ResumeState::Ready);
        assert_eq!(
            before, after,
            "blocked {case} must remain byte-for-byte read-only"
        );

        fs::remove_dir_all(root).expect("remove Slice H blocked evidence");
    }
}

fn failing_verify_wrapper() -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    let actual = env!("CARGO_BIN_EXE_postgres-postgres");
    let path = std::env::temp_dir().join(format!(
        "postgres-postgres-sliceh-fail-{}.sh",
        support::nonce()
    ));
    let script = format!(
        "#!/bin/sh\nif [ \"$1\" = \"verify\" ]; then exit 73; fi\nexec \"{actual}\" \"$@\"\n"
    );
    fs::write(&path, script).expect("write Slice H verify-failure wrapper");
    let mut permissions = fs::metadata(&path)
        .expect("stat Slice H verify-failure wrapper")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&path, permissions).expect("chmod Slice H verify-failure wrapper");
    path
}

#[tokio::test]
async fn selected_iteration_failure_stops_batch_without_retry_skip_or_later_sequence() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 83,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("failure");
    let campaign_id = format!("slicehfailure{}", support::nonce());
    let database_url = support::database_url();
    let wrapper = failing_verify_wrapper();
    initialize_campaign(&root, &campaign_id, &wrapper);

    let error = execute_bounded_batch(
        &wrapper,
        &database_url,
        &root,
        &campaign_id,
        3,
        SAMPLE_INTERVAL,
    )
    .await
    .expect_err("forced Slice H verifier failure must stop the batch");
    assert!(
        error.to_string().contains("verify child failed"),
        "unexpected Slice H failure: {error:#}"
    );

    let campaign_dir = root.join(&campaign_id);
    assert!(!campaign_dir.join("iterations/000001.json").exists());
    assert!(!campaign_dir.join("iterations/000002.json").exists());
    let failures = fs::read_dir(campaign_dir.join("failures"))
        .expect("read Slice H failure directory")
        .map(|entry| entry.expect("read Slice H failure entry").path())
        .collect::<Vec<_>>();
    assert_eq!(failures.len(), 1);
    assert!(failures[0]
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("000001-")));

    let blocked = reconcile_resume_status(&root, &campaign_id, &wrapper)
        .expect("reconcile Slice H failed sequence");
    assert_eq!(blocked.state, ResumeState::BlockedFailure);
    assert_eq!(blocked.frontier_sequence, 0);
    assert_eq!(blocked.blocked_sequence, Some(1));
    assert_eq!(blocked.next_sequence, None);

    fs::remove_file(wrapper).expect("remove Slice H verify-failure wrapper");
    fs::remove_dir_all(root).expect("remove Slice H failure evidence");
}

#[tokio::test]
async fn zero_iteration_count_fails_before_campaign_mutation() {
    let root = temp_root("zero");
    let campaign_id = format!("slicehzero{}", support::nonce());
    let binary = workload_binary();

    let error = execute_bounded_batch(
        &binary,
        "postgres://must-not-connect.invalid/zero",
        &root,
        &campaign_id,
        0,
        SAMPLE_INTERVAL,
    )
    .await
    .expect_err("zero Slice H iteration count must fail");
    assert!(
        error
            .to_string()
            .contains("requested iterations must be greater than zero"),
        "unexpected zero-count error: {error:#}"
    );
    assert!(
        !root.exists(),
        "zero-count failure must not create evidence"
    );
}

#[test]
fn run_batch_cli_exposes_no_arbitrary_sequence_argument() {
    let root = temp_root("cli");
    let output = Command::new(endurancectl_binary())
        .arg("run-batch")
        .arg("--database-url")
        .arg("postgres://unused")
        .arg("--evidence-root")
        .arg(&root)
        .arg("--campaign-id")
        .arg("slicehcli")
        .arg("--iterations")
        .arg("1")
        .arg("--sequence")
        .arg("99")
        .output()
        .expect("invoke run-batch CLI with forbidden sequence");

    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("unexpected argument '--sequence'"),
        "run-batch must reject arbitrary sequence input: {stderr}"
    );
}
