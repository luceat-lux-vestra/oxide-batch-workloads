//! Campaign #203 / #220 Slice F: read-only durable resume-frontier reconciliation.

mod support;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use postgres_postgres::endurance::evidence::EvidenceStore;
use postgres_postgres::endurance::resume::{reconcile_resume_status, ResumeState};
use postgres_postgres::endurance::{EnduranceFailure, EnduranceReader};
use serde_json::json;

fn workload_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres"))
}

fn temp_root(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "postgres-postgres-slicef-{label}-{}",
        support::nonce()
    ))
}

fn new_store(root: &Path, campaign_id: &str) -> EvidenceStore {
    EvidenceStore::open(root, campaign_id, &workload_binary()).expect("initialize Slice F evidence")
}

fn write_success(store: &EvidenceStore, sequence: u64, execution_kind: &str) {
    let record = json!({
        "schema": "oxide-batch-workloads.endurance-iteration",
        "schema_version": 1,
        "timestamp_unix_nanos_utc": 1_000_000_u64 + sequence,
        "campaign_id": store.campaign().campaign_id,
        "sequence": sequence,
        "terminal": "success",
        "execution": {
            "kind": execution_kind,
            "result": {
                "sequence": sequence
            }
        }
    });
    fs::write(
        store.iteration_path(sequence),
        serde_json::to_vec_pretty(&record).expect("serialize success evidence"),
    )
    .expect("write success iteration evidence");
    store
        .record_event(
            sequence,
            "iteration-terminal-success",
            json!({ "test": "slice-f" }),
        )
        .expect("record terminal success event");
}

fn write_failure(store: &EvidenceStore, sequence: u64) {
    let timestamp = 2_000_000_u64 + sequence;
    let path = store
        .campaign_dir()
        .join("failures")
        .join(format!("{sequence:06}-{timestamp}.json"));
    let record = json!({
        "schema": "oxide-batch-workloads.endurance-failure",
        "schema_version": 1,
        "timestamp_unix_nanos_utc": timestamp,
        "campaign_id": store.campaign().campaign_id,
        "sequence": sequence,
        "terminal": "failure",
        "failure_class": "harness-environment",
        "error": "test failure"
    });
    fs::write(
        path,
        serde_json::to_vec_pretty(&record).expect("serialize failure evidence"),
    )
    .expect("write failure evidence");
    store
        .record_event(
            sequence,
            "iteration-terminal-failure",
            json!({ "failure_class": "harness-environment" }),
        )
        .expect("record terminal failure event");
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

#[test]
fn fresh_campaign_is_ready_for_one_samples_are_observational_and_inspection_is_read_only() {
    let root = temp_root("fresh");
    let campaign_id = format!("resumefresh{}", support::nonce());
    let store = new_store(&root, &campaign_id);
    fs::write(
        store.samples_path(),
        br#"{"schema":"oxide-batch-workloads.endurance-sample","schema_version":1,"sequence":999,"sample_kind":"periodic"}
"#,
    )
    .expect("write observational samples");

    let before = snapshot_tree(&root);
    let first = reconcile_resume_status(&root, &campaign_id, &workload_binary())
        .expect("reconcile fresh campaign");
    let second = reconcile_resume_status(&root, &campaign_id, &workload_binary())
        .expect("repeat fresh campaign reconciliation");
    let after = snapshot_tree(&root);

    assert_eq!(first, second);
    assert_eq!(first.state, ResumeState::Ready);
    assert_eq!(first.frontier_sequence, 0);
    assert_eq!(first.next_sequence, Some(1));
    let plan = first.next_plan.as_ref().expect("ready status has next plan");
    assert_eq!(plan.sequence, 1);
    assert_eq!(plan.reader, EnduranceReader::Cursor);
    assert_eq!(plan.failure, EnduranceFailure::Clean);
    assert_eq!(before, after, "resume inspection must not mutate evidence");

    let serialized = serde_json::to_string(&first).expect("serialize resume status");
    assert!(!serialized.contains("DATABASE_URL"));
    assert!(!serialized.contains("postgres://"));
    assert!(!serialized.contains("password"));

    fs::remove_dir_all(root).expect("remove Slice F fresh evidence");
}

#[test]
fn contiguous_success_frontier_preserves_reader_and_crash_schedule() {
    let root = temp_root("frontier");
    let campaign_id = format!("resumefrontier{}", support::nonce());
    let store = new_store(&root, &campaign_id);

    for sequence in 1..=23 {
        write_success(&store, sequence, "clean");
    }

    let before_crash = reconcile_resume_status(&root, &campaign_id, &workload_binary())
        .expect("reconcile before scheduled crash");
    assert_eq!(before_crash.state, ResumeState::Ready);
    assert_eq!(before_crash.frontier_sequence, 23);
    assert_eq!(before_crash.next_sequence, Some(24));
    let crash_plan = before_crash
        .next_plan
        .as_ref()
        .expect("ready status has crash plan");
    assert_eq!(crash_plan.reader, EnduranceReader::Paging);
    assert_eq!(crash_plan.failure, EnduranceFailure::BeforeCommit);

    write_success(&store, 24, "crash");
    let after_crash = reconcile_resume_status(&root, &campaign_id, &workload_binary())
        .expect("reconcile after scheduled crash success");
    assert_eq!(after_crash.state, ResumeState::Ready);
    assert_eq!(after_crash.frontier_sequence, 24);
    assert_eq!(after_crash.next_sequence, Some(25));
    let next_plan = after_crash
        .next_plan
        .as_ref()
        .expect("ready status has post-crash plan");
    assert_eq!(next_plan.reader, EnduranceReader::Cursor);
    assert_eq!(next_plan.failure, EnduranceFailure::Clean);

    fs::remove_dir_all(root).expect("remove Slice F frontier evidence");
}

#[test]
fn failure_and_incomplete_frontiers_block_without_advancing() {
    let failure_root = temp_root("failure");
    let failure_campaign = format!("resumefailure{}", support::nonce());
    let failure_store = new_store(&failure_root, &failure_campaign);
    write_success(&failure_store, 1, "clean");
    write_failure(&failure_store, 2);

    let failed = reconcile_resume_status(
        &failure_root,
        &failure_campaign,
        &workload_binary(),
    )
    .expect("reconcile failed frontier");
    assert_eq!(failed.state, ResumeState::BlockedFailure);
    assert_eq!(failed.frontier_sequence, 1);
    assert_eq!(failed.blocked_sequence, Some(2));
    assert_eq!(failed.next_sequence, None);
    assert_eq!(failed.next_plan, None);

    let incomplete_root = temp_root("incomplete");
    let incomplete_campaign = format!("resumeincomplete{}", support::nonce());
    let incomplete_store = new_store(&incomplete_root, &incomplete_campaign);
    incomplete_store
        .record_event(1, "iteration-planned", json!({ "sequence": 1 }))
        .expect("record planned event");
    incomplete_store
        .record_event(1, "iteration-started", json!({ "mode": "clean" }))
        .expect("record started event");

    let incomplete = reconcile_resume_status(
        &incomplete_root,
        &incomplete_campaign,
        &workload_binary(),
    )
    .expect("reconcile incomplete frontier");
    assert_eq!(incomplete.state, ResumeState::BlockedIncomplete);
    assert_eq!(incomplete.frontier_sequence, 0);
    assert_eq!(incomplete.blocked_sequence, Some(1));

    fs::remove_dir_all(failure_root).expect("remove Slice F failure evidence");
    fs::remove_dir_all(incomplete_root).expect("remove Slice F incomplete evidence");
}

#[test]
fn singleton_event_disagreement_gap_and_duplicate_terminal_fail_closed() {
    let disagreement_root = temp_root("disagreement");
    let disagreement_campaign = format!("resumedisagree{}", support::nonce());
    let disagreement_store = new_store(&disagreement_root, &disagreement_campaign);
    let singleton = json!({
        "schema": "oxide-batch-workloads.endurance-iteration",
        "schema_version": 1,
        "timestamp_unix_nanos_utc": 10_u64,
        "campaign_id": disagreement_campaign,
        "sequence": 1,
        "terminal": "success",
        "execution": { "kind": "clean" }
    });
    fs::write(
        disagreement_store.iteration_path(1),
        serde_json::to_vec_pretty(&singleton).expect("serialize singleton"),
    )
    .expect("write singleton without terminal event");
    let disagreement = reconcile_resume_status(
        &disagreement_root,
        &disagreement_campaign,
        &workload_binary(),
    )
    .expect("reconcile singleton/event disagreement");
    assert_eq!(
        disagreement.state,
        ResumeState::BlockedInconsistentEvidence
    );

    let gap_root = temp_root("gap");
    let gap_campaign = format!("resumegap{}", support::nonce());
    let gap_store = new_store(&gap_root, &gap_campaign);
    write_success(&gap_store, 1, "clean");
    write_success(&gap_store, 3, "clean");
    let gap = reconcile_resume_status(&gap_root, &gap_campaign, &workload_binary())
        .expect("reconcile evidence gap");
    assert_eq!(gap.state, ResumeState::BlockedInconsistentEvidence);
    assert_eq!(gap.frontier_sequence, 1);
    assert_eq!(gap.blocked_sequence, Some(2));

    let duplicate_root = temp_root("duplicate");
    let duplicate_campaign = format!("resumeduplicate{}", support::nonce());
    let duplicate_store = new_store(&duplicate_root, &duplicate_campaign);
    write_success(&duplicate_store, 1, "clean");
    duplicate_store
        .record_event(1, "iteration-terminal-success", json!({ "duplicate": true }))
        .expect("record duplicate success terminal");
    let duplicate = reconcile_resume_status(
        &duplicate_root,
        &duplicate_campaign,
        &workload_binary(),
    )
    .expect("reconcile duplicate terminal");
    assert_eq!(
        duplicate.state,
        ResumeState::BlockedInconsistentEvidence
    );

    fs::remove_dir_all(disagreement_root).expect("remove disagreement evidence");
    fs::remove_dir_all(gap_root).expect("remove gap evidence");
    fs::remove_dir_all(duplicate_root).expect("remove duplicate evidence");
}

#[test]
fn malformed_or_conflicting_evidence_fails_closed() {
    let malformed_root = temp_root("malformed");
    let malformed_campaign = format!("resumemalformed{}", support::nonce());
    let malformed_store = new_store(&malformed_root, &malformed_campaign);
    fs::write(
        malformed_store
            .campaign_dir()
            .join("failures")
            .join("not-a-sequence.json"),
        b"{}",
    )
    .expect("write malformed failure evidence");
    let malformed = reconcile_resume_status(
        &malformed_root,
        &malformed_campaign,
        &workload_binary(),
    )
    .expect("reconcile malformed evidence");
    assert_eq!(
        malformed.state,
        ResumeState::BlockedInconsistentEvidence
    );

    let conflict_root = temp_root("conflict");
    let conflict_campaign = format!("resumeconflict{}", support::nonce());
    let conflict_store = new_store(&conflict_root, &conflict_campaign);
    write_success(&conflict_store, 1, "clean");
    write_failure(&conflict_store, 1);
    let conflict = reconcile_resume_status(
        &conflict_root,
        &conflict_campaign,
        &workload_binary(),
    )
    .expect("reconcile conflicting terminal evidence");
    assert_eq!(
        conflict.state,
        ResumeState::BlockedInconsistentEvidence
    );

    fs::remove_dir_all(malformed_root).expect("remove malformed evidence");
    fs::remove_dir_all(conflict_root).expect("remove conflict evidence");
}
