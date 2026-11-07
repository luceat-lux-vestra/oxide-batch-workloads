#![cfg(any(target_os = "linux", target_os = "android"))]

//! Campaign #203 / #226 Slice I: fail-closed external pre-iteration guard
//! integration for bounded durable-resume-selected execution.

mod support;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use postgres_postgres::endurance::evidence::EvidenceStore;
use postgres_postgres::endurance::guarded_batch::{
    execute_guarded_bounded_batch, GuardBlockKind, GuardDecision, GuardedBatchStopReason,
};
use postgres_postgres::endurance::resume::{reconcile_resume_status, ResumeState};
use postgres_postgres::endurance::CANONICAL_SEED;
use serde_json::json;
use support::SeedOptions;

const SAMPLE_INTERVAL: Duration = Duration::from_millis(10);
const SECRET_MARKER: &str = "SLICE_I_GUARD_SECRET_6bba8f";

fn workload_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres"))
}

fn endurancectl_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres-endurancectl"))
}

fn temp_root(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "postgres-postgres-slicei-{label}-{}",
        support::nonce()
    ))
}

fn initialize_campaign(root: &Path, campaign_id: &str, binary: &Path) {
    let store =
        EvidenceStore::open(root, campaign_id, binary).expect("initialize Slice I campaign");
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

fn quoted(path: &Path) -> String {
    let text = path.to_string_lossy();
    assert!(
        !text.contains('\''),
        "test helper path must not contain apostrophe"
    );
    format!("'{text}'")
}

fn write_guard_script(dir: &Path, name: &str, body: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    fs::create_dir_all(dir).expect("create Slice I guard helper directory");
    let path = dir.join(name);
    fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write Slice I guard helper");
    let mut permissions = fs::metadata(&path)
        .expect("stat Slice I guard helper")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&path, permissions).expect("chmod Slice I guard helper");
    path
}

fn invocation_count(path: &Path) -> usize {
    match fs::read_to_string(path) {
        Ok(text) => text.lines().count(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => panic!("read Slice I guard invocation log: {error}"),
    }
}

fn assert_evidence_has_no_secret(root: &Path) {
    for bytes in snapshot_tree(root).values() {
        assert!(
            !String::from_utf8_lossy(bytes).contains(SECRET_MARKER),
            "guard output secret leaked into durable evidence"
        );
    }
}

#[tokio::test]
async fn fresh_campaign_bootstraps_before_first_guarded_iteration() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 200,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("fresh-bootstrap-evidence");
    let helper = temp_root("fresh-bootstrap-helper");
    let calls = helper.join("calls.log");
    let guard = write_guard_script(
        &helper,
        "allow-guard",
        &format!("printf 'allow\\n' >> {}; exit 0", quoted(&calls)),
    );
    let campaign_id = format!("sliceifresh{}", support::nonce());
    let binary = workload_binary();
    assert!(!root.exists());

    let result = execute_guarded_bounded_batch(
        &binary,
        &support::database_url(),
        &root,
        &campaign_id,
        2,
        SAMPLE_INTERVAL,
        &guard,
    )
    .await
    .expect("fresh guarded campaign must bootstrap before reconciliation");

    assert_eq!(
        result.stop_reason,
        GuardedBatchStopReason::RequestedCountCompleted
    );
    assert_eq!(result.starting_frontier_sequence, 0);
    assert_eq!(result.completed_sequences, vec![1, 2]);
    assert_eq!(result.completed_iterations, 2);
    assert_eq!(result.guard_attempts.len(), 2);
    assert!(result
        .guard_attempts
        .iter()
        .all(|attempt| attempt.decision == GuardDecision::Allow));
    assert_eq!(result.terminal_resume_status.state, ResumeState::Ready);
    assert_eq!(result.terminal_resume_status.next_sequence, Some(3));
    assert_eq!(invocation_count(&calls), 2);
    assert!(root.join(&campaign_id).join("campaign.json").is_file());

    fs::remove_dir_all(root).expect("remove fresh Slice I evidence");
    fs::remove_dir_all(helper).expect("remove fresh Slice I helper");
}

#[tokio::test]
async fn preexisting_directory_without_campaign_metadata_remains_fail_closed() {
    let root = temp_root("partial-existing-evidence");
    let helper = temp_root("partial-existing-helper");
    let calls = helper.join("calls.log");
    let guard = write_guard_script(
        &helper,
        "must-not-run",
        &format!("printf 'called\\n' >> {}; exit 0", quoted(&calls)),
    );
    let campaign_id = format!("sliceipartial{}", support::nonce());
    let campaign_dir = root.join(&campaign_id);
    fs::create_dir_all(&campaign_dir).expect("create partial campaign directory");
    fs::write(campaign_dir.join("sentinel"), b"preserve-me")
        .expect("write partial campaign sentinel");
    let before = snapshot_tree(&root);

    let result = execute_guarded_bounded_batch(
        &workload_binary(),
        "postgres://must-not-connect.invalid/partial-existing",
        &root,
        &campaign_id,
        2,
        SAMPLE_INTERVAL,
        &guard,
    )
    .await
    .expect("partial existing campaign must return structured resume block");

    assert_eq!(result.stop_reason, GuardedBatchStopReason::ResumeBlocked);
    assert_eq!(result.completed_iterations, 0);
    assert!(result.completed_sequences.is_empty());
    assert!(result.guard_attempts.is_empty());
    assert_eq!(
        result.terminal_resume_status.state,
        ResumeState::BlockedInconsistentEvidence
    );
    assert_eq!(invocation_count(&calls), 0);
    assert_eq!(before, snapshot_tree(&root));
    assert!(!campaign_dir.join("campaign.json").exists());

    fs::remove_dir_all(root).expect("remove partial Slice I evidence");
    fs::remove_dir_all(helper).expect("remove partial Slice I helper");
}

#[tokio::test]
async fn allow_runs_once_per_iteration_and_independent_invocation_resumes() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 500,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("allow-evidence");
    let helper = temp_root("allow-helper");
    let calls = helper.join("calls.log");
    let guard = write_guard_script(
        &helper,
        "allow-guard",
        &format!("printf 'allow\\n' >> {}; exit 0", quoted(&calls)),
    );
    let campaign_id = format!("sliceiallow{}", support::nonce());
    let database_url = support::database_url();
    let binary = workload_binary();
    initialize_campaign(&root, &campaign_id, &binary);

    let first = execute_guarded_bounded_batch(
        &binary,
        &database_url,
        &root,
        &campaign_id,
        2,
        SAMPLE_INTERVAL,
        &guard,
    )
    .await
    .expect("execute first guarded Slice I batch");

    assert_eq!(
        first.stop_reason,
        GuardedBatchStopReason::RequestedCountCompleted
    );
    assert_eq!(first.completed_sequences, vec![1, 2]);
    assert_eq!(first.completed_iterations, 2);
    assert_eq!(first.guard_attempts.len(), 2);
    assert!(first
        .guard_attempts
        .iter()
        .all(|attempt| attempt.decision == GuardDecision::Allow));
    assert_eq!(first.terminal_resume_status.next_sequence, Some(3));
    assert_eq!(invocation_count(&calls), 2);

    let second = execute_guarded_bounded_batch(
        &binary,
        &database_url,
        &root,
        &campaign_id,
        1,
        SAMPLE_INTERVAL,
        &guard,
    )
    .await
    .expect("execute second guarded Slice I batch");

    assert_eq!(second.starting_frontier_sequence, 2);
    assert_eq!(second.completed_sequences, vec![3]);
    assert_eq!(second.terminal_resume_status.next_sequence, Some(4));
    assert_eq!(invocation_count(&calls), 3);

    fs::remove_dir_all(root).expect("remove Slice I allow evidence");
    fs::remove_dir_all(helper).expect("remove Slice I allow helper");
}

#[tokio::test]
async fn mid_batch_guard_block_preserves_blocked_frontier_and_secret_safety() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 300,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("mid-block-evidence");
    let helper = temp_root("mid-block-helper");
    let count = helper.join("count");
    let guard_body = format!(
        "count=0\nif [ -f {0} ]; then count=$(cat {0}); fi\n\
         count=$((count + 1))\nprintf '%s\\n' \"$count\" > {0}\n\
         if [ \"$count\" -eq 1 ]; then exit 0; fi\n\
         echo '{1}'\necho '{1}' >&2\nexit 42",
        quoted(&count),
        SECRET_MARKER
    );
    let guard = write_guard_script(&helper, "mid-block-guard", &guard_body);
    let campaign_id = format!("sliceimidblock{}", support::nonce());
    let binary = workload_binary();
    initialize_campaign(&root, &campaign_id, &binary);

    let result = execute_guarded_bounded_batch(
        &binary,
        &support::database_url(),
        &root,
        &campaign_id,
        3,
        SAMPLE_INTERVAL,
        &guard,
    )
    .await
    .expect("mid-batch guard block returns structured result");

    assert_eq!(result.stop_reason, GuardedBatchStopReason::GuardBlocked);
    assert_eq!(result.completed_sequences, vec![1]);
    assert_eq!(result.completed_iterations, 1);
    assert_eq!(result.guard_attempts.len(), 2);
    assert_eq!(result.guard_attempts[1].sequence, 2);
    assert_eq!(result.guard_attempts[1].decision, GuardDecision::Block);
    assert_eq!(
        result.guard_attempts[1].block_kind,
        Some(GuardBlockKind::NonzeroExit)
    );
    assert_eq!(result.guard_attempts[1].exit_code, Some(42));
    assert_eq!(result.terminal_resume_status.state, ResumeState::Ready);
    assert_eq!(result.terminal_resume_status.next_sequence, Some(2));
    assert!(!root
        .join(&campaign_id)
        .join("iterations/000002.json")
        .exists());

    let serialized = serde_json::to_string(&result).expect("serialize guarded Slice I result");
    assert!(!serialized.contains(SECRET_MARKER));
    assert_evidence_has_no_secret(&root);

    fs::remove_dir_all(root).expect("remove Slice I mid-block evidence");
    fs::remove_dir_all(helper).expect("remove Slice I mid-block helper");
}

#[tokio::test]
async fn first_step_guard_failures_are_fail_closed_and_do_not_mutate_evidence() {
    let cases = ["nonzero", "signal", "missing"];

    for case in cases {
        let root = temp_root(&format!("{case}-evidence"));
        let helper = temp_root(&format!("{case}-helper"));
        let campaign_id = format!("sliceifailclosed{case}{}", support::nonce());
        let binary = workload_binary();
        initialize_campaign(&root, &campaign_id, &binary);
        let before = snapshot_tree(&root);

        let guard = match case {
            "nonzero" => write_guard_script(
                &helper,
                "guard;literal-metachar",
                &format!("echo '{0}'\necho '{0}' >&2\nexit 42", SECRET_MARKER),
            ),
            "signal" => write_guard_script(&helper, "signal-guard", "kill -TERM $$"),
            "missing" => helper.join("does-not-exist"),
            _ => unreachable!(),
        };

        let result = execute_guarded_bounded_batch(
            &binary,
            "postgres://must-not-connect.invalid/guard-block",
            &root,
            &campaign_id,
            2,
            SAMPLE_INTERVAL,
            &guard,
        )
        .await
        .expect("guard failure must become structured fail-closed block");

        assert_eq!(result.stop_reason, GuardedBatchStopReason::GuardBlocked);
        assert_eq!(result.completed_iterations, 0);
        assert!(result.completed_sequences.is_empty());
        assert_eq!(result.guard_attempts.len(), 1);
        assert_eq!(result.guard_attempts[0].sequence, 1);
        assert_eq!(result.terminal_resume_status.next_sequence, Some(1));
        assert_eq!(
            before,
            snapshot_tree(&root),
            "first-step guard block must not mutate campaign evidence"
        );

        match case {
            "nonzero" => {
                assert_eq!(
                    result.guard_attempts[0].block_kind,
                    Some(GuardBlockKind::NonzeroExit)
                );
                assert_eq!(result.guard_attempts[0].exit_code, Some(42));
            }
            "signal" => {
                assert_eq!(
                    result.guard_attempts[0].block_kind,
                    Some(GuardBlockKind::Signal)
                );
                assert_eq!(result.guard_attempts[0].exit_code, None);
            }
            "missing" => {
                assert_eq!(
                    result.guard_attempts[0].block_kind,
                    Some(GuardBlockKind::SpawnFailure)
                );
                assert_eq!(result.guard_attempts[0].exit_code, None);
            }
            _ => unreachable!(),
        }

        let serialized = serde_json::to_string(&result).expect("serialize guard block result");
        assert!(!serialized.contains(SECRET_MARKER));
        assert_evidence_has_no_secret(&root);

        fs::remove_dir_all(root).expect("remove Slice I fail-closed evidence");
        if helper.exists() {
            fs::remove_dir_all(helper).expect("remove Slice I fail-closed helper");
        }
    }
}

#[tokio::test]
async fn resume_block_prevents_guard_invocation() {
    let root = temp_root("resume-block-evidence");
    let helper = temp_root("resume-block-helper");
    let calls = helper.join("calls.log");
    let guard = write_guard_script(
        &helper,
        "must-not-run",
        &format!("printf 'called\\n' >> {}; exit 0", quoted(&calls)),
    );
    let campaign_id = format!("sliceiresumeblock{}", support::nonce());
    let binary = workload_binary();
    let store = EvidenceStore::open(&root, &campaign_id, &binary)
        .expect("initialize resume-block campaign");
    store
        .record_event(
            1,
            "iteration-terminal-failure",
            json!({ "failure_class": "harness-environment" }),
        )
        .expect("record blocking failure");
    let before = snapshot_tree(&root);

    let result = execute_guarded_bounded_batch(
        &binary,
        "postgres://must-not-connect.invalid/resume-block",
        &root,
        &campaign_id,
        2,
        SAMPLE_INTERVAL,
        &guard,
    )
    .await
    .expect("resume block returns structured result");

    assert_eq!(result.stop_reason, GuardedBatchStopReason::ResumeBlocked);
    assert_eq!(result.completed_iterations, 0);
    assert!(result.guard_attempts.is_empty());
    assert_eq!(
        result.terminal_resume_status.state,
        ResumeState::BlockedFailure
    );
    assert_eq!(invocation_count(&calls), 0);
    assert_eq!(before, snapshot_tree(&root));

    fs::remove_dir_all(root).expect("remove Slice I resume-block evidence");
    fs::remove_dir_all(helper).expect("remove Slice I resume-block helper");
}

fn failing_verify_wrapper() -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    let actual = env!("CARGO_BIN_EXE_postgres-postgres");
    let path = std::env::temp_dir().join(format!(
        "postgres-postgres-slicei-fail-{}.sh",
        support::nonce()
    ));
    let script = format!(
        "#!/bin/sh\nif [ \"$1\" = \"verify\" ]; then exit 73; fi\nexec \"{actual}\" \"$@\"\n"
    );
    fs::write(&path, script).expect("write Slice I verify-failure wrapper");
    let mut permissions = fs::metadata(&path)
        .expect("stat Slice I verify-failure wrapper")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&path, permissions).expect("chmod Slice I verify-failure wrapper");
    path
}

#[tokio::test]
async fn selected_iteration_failure_after_allow_stops_without_retry_or_skip() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 83,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("selected-failure-evidence");
    let helper = temp_root("selected-failure-helper");
    let calls = helper.join("calls.log");
    let guard = write_guard_script(
        &helper,
        "allow-guard",
        &format!("printf 'allow\\n' >> {}; exit 0", quoted(&calls)),
    );
    let campaign_id = format!("sliceiselectedfailure{}", support::nonce());
    let wrapper = failing_verify_wrapper();
    initialize_campaign(&root, &campaign_id, &wrapper);

    let error = execute_guarded_bounded_batch(
        &wrapper,
        &support::database_url(),
        &root,
        &campaign_id,
        3,
        SAMPLE_INTERVAL,
        &guard,
    )
    .await
    .expect_err("selected verifier failure must stop guarded batch");
    assert!(
        error.to_string().contains("verify child failed"),
        "unexpected Slice I selected failure: {error:#}"
    );
    assert_eq!(invocation_count(&calls), 1);
    assert!(!root
        .join(&campaign_id)
        .join("iterations/000002.json")
        .exists());

    let blocked = reconcile_resume_status(&root, &campaign_id, &wrapper)
        .expect("reconcile Slice I selected failure");
    assert_eq!(blocked.state, ResumeState::BlockedFailure);
    assert_eq!(blocked.frontier_sequence, 0);
    assert_eq!(blocked.blocked_sequence, Some(1));

    fs::remove_file(wrapper).expect("remove Slice I verify-failure wrapper");
    fs::remove_dir_all(root).expect("remove Slice I selected-failure evidence");
    fs::remove_dir_all(helper).expect("remove Slice I selected-failure helper");
}

#[tokio::test]
async fn zero_iteration_count_fails_before_guard_or_campaign_mutation() {
    let root = temp_root("zero-evidence");
    let helper = temp_root("zero-helper");
    let calls = helper.join("calls.log");
    let guard = write_guard_script(
        &helper,
        "must-not-run",
        &format!("printf 'called\\n' >> {}; exit 0", quoted(&calls)),
    );

    let error = execute_guarded_bounded_batch(
        &workload_binary(),
        "postgres://must-not-connect.invalid/zero",
        &root,
        "sliceizero",
        0,
        SAMPLE_INTERVAL,
        &guard,
    )
    .await
    .expect_err("zero guarded iteration count must fail");
    assert!(
        error
            .to_string()
            .contains("requested iterations must be greater than zero"),
        "unexpected Slice I zero-count error: {error:#}"
    );
    assert!(!root.exists());
    assert_eq!(invocation_count(&calls), 0);

    fs::remove_dir_all(helper).expect("remove Slice I zero helper");
}

#[test]
fn run_guarded_batch_cli_exposes_no_arbitrary_sequence_argument() {
    let root = temp_root("cli");
    let output = Command::new(endurancectl_binary())
        .arg("run-guarded-batch")
        .arg("--database-url")
        .arg("postgres://unused")
        .arg("--evidence-root")
        .arg(&root)
        .arg("--campaign-id")
        .arg("sliceicli")
        .arg("--iterations")
        .arg("1")
        .arg("--pre-iteration-guard")
        .arg("unused-guard")
        .arg("--sequence")
        .arg("99")
        .output()
        .expect("invoke guarded Slice I CLI with forbidden sequence");

    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("unexpected argument '--sequence'"),
        "run-guarded-batch must reject arbitrary sequence input: {stderr}"
    );
}
