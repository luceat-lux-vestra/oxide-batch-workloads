#![cfg(any(target_os = "linux", target_os = "android"))]

//! Campaign #203 / #222 Slice G: execute exactly one durable-resume-selected
//! sampled recorded logical iteration per invocation.

mod support;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use postgres_postgres::endurance::evidence::EvidenceStore;
use postgres_postgres::endurance::progress::execute_next_sampled_recorded_iteration;
use postgres_postgres::endurance::resume::{reconcile_resume_status, ResumeState};
use postgres_postgres::endurance::CANONICAL_SEED;
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
        "postgres-postgres-sliceg-{label}-{}",
        support::nonce()
    ))
}

fn initialize_campaign(root: &Path, campaign_id: &str, binary: &Path) {
    let store =
        EvidenceStore::open(root, campaign_id, binary).expect("initialize Slice G campaign");
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

#[tokio::test]
async fn successful_invocations_advance_exactly_one_sequence_each_and_stay_secret_safe() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 500,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("success");
    let campaign_id = format!("slicegsuccess{}", support::nonce());
    let database_url = support::database_url();
    let binary = workload_binary();
    initialize_campaign(&root, &campaign_id, &binary);

    let first = execute_next_sampled_recorded_iteration(
        &binary,
        &database_url,
        &root,
        &campaign_id,
        SAMPLE_INTERVAL,
    )
    .await
    .expect("execute Slice G sequence 1");
    assert_eq!(first.pre_execution.state, ResumeState::Ready);
    assert_eq!(first.selected_sequence, Some(1));
    let first_plan = first
        .selected_plan
        .as_ref()
        .expect("sequence 1 selected plan");
    assert_eq!(first_plan.sequence, 1);
    assert_eq!(
        first
            .execution
            .as_ref()
            .expect("sequence 1 execution")
            .recorded
            .sequence,
        1
    );
    assert!(root
        .join(&campaign_id)
        .join("iterations/000001.json")
        .is_file());
    assert!(!root
        .join(&campaign_id)
        .join("iterations/000002.json")
        .exists());

    let after_first = reconcile_resume_status(&root, &campaign_id, &binary)
        .expect("reconcile after first Slice G invocation");
    assert_eq!(after_first.next_sequence, Some(2));

    let second = execute_next_sampled_recorded_iteration(
        &binary,
        &database_url,
        &root,
        &campaign_id,
        SAMPLE_INTERVAL,
    )
    .await
    .expect("execute Slice G sequence 2");
    assert_eq!(second.selected_sequence, Some(2));
    assert_eq!(
        second
            .execution
            .as_ref()
            .expect("sequence 2 execution")
            .recorded
            .sequence,
        2
    );
    assert!(root
        .join(&campaign_id)
        .join("iterations/000002.json")
        .is_file());
    assert!(!root
        .join(&campaign_id)
        .join("iterations/000003.json")
        .exists());

    let after_second = reconcile_resume_status(&root, &campaign_id, &binary)
        .expect("reconcile after second Slice G invocation");
    assert_eq!(after_second.next_sequence, Some(3));

    let serialized = serde_json::to_string(&second).expect("serialize Slice G result");
    assert!(!serialized.contains(&database_url));
    assert!(!serialized.contains("oxide_batch_workload:oxide_batch_workload"));

    fs::remove_dir_all(root).expect("remove Slice G success evidence");
}

fn failing_verify_wrapper() -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    let actual = env!("CARGO_BIN_EXE_postgres-postgres");
    let path = std::env::temp_dir().join(format!(
        "postgres-postgres-sliceg-fail-{}.sh",
        support::nonce()
    ));
    let script = format!(
        "#!/bin/sh\nif [ \"$1\" = \"verify\" ]; then exit 73; fi\nexec \"{actual}\" \"$@\"\n"
    );
    fs::write(&path, script).expect("write Slice G verify-failure wrapper");
    let mut permissions = fs::metadata(&path)
        .expect("stat Slice G verify-failure wrapper")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&path, permissions).expect("chmod Slice G verify-failure wrapper");
    path
}

#[tokio::test]
async fn selected_iteration_failure_preserves_one_failed_sequence_without_retry_or_skip() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 83,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("failure");
    let campaign_id = format!("slicegfailure{}", support::nonce());
    let database_url = support::database_url();
    let wrapper = failing_verify_wrapper();
    initialize_campaign(&root, &campaign_id, &wrapper);

    let error = execute_next_sampled_recorded_iteration(
        &wrapper,
        &database_url,
        &root,
        &campaign_id,
        SAMPLE_INTERVAL,
    )
    .await
    .expect_err("forced Slice G verifier failure must fail");
    assert!(
        error.to_string().contains("verify child failed"),
        "unexpected Slice G failure: {error:#}"
    );

    let campaign_dir = root.join(&campaign_id);
    assert!(!campaign_dir.join("iterations/000001.json").exists());
    assert!(!campaign_dir.join("iterations/000002.json").exists());
    let failures = fs::read_dir(campaign_dir.join("failures"))
        .expect("read Slice G failure directory")
        .map(|entry| entry.expect("read Slice G failure entry").path())
        .collect::<Vec<_>>();
    assert_eq!(failures.len(), 1);
    assert!(failures[0]
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("000001-")));

    let blocked = reconcile_resume_status(&root, &campaign_id, &wrapper)
        .expect("reconcile Slice G failed sequence");
    assert_eq!(blocked.state, ResumeState::BlockedFailure);
    assert_eq!(blocked.frontier_sequence, 0);
    assert_eq!(blocked.blocked_sequence, Some(1));
    assert_eq!(blocked.next_sequence, None);

    fs::remove_file(wrapper).expect("remove Slice G verify-failure wrapper");
    fs::remove_dir_all(root).expect("remove Slice G failure evidence");
}

#[tokio::test]
async fn run_next_reuses_slice_e_sample_interval_validation_without_mutation() {
    let root = temp_root("interval");
    let campaign_id = format!("sliceginterval{}", support::nonce());
    let binary = workload_binary();
    initialize_campaign(&root, &campaign_id, &binary);
    let before = snapshot_tree(&root);

    let error = execute_next_sampled_recorded_iteration(
        &binary,
        &support::database_url(),
        &root,
        &campaign_id,
        Duration::from_millis(1),
    )
    .await
    .expect_err("Slice E sample interval validation must remain authoritative");
    assert!(
        error
            .to_string()
            .contains("sample interval must be between 10 and 60000 milliseconds"),
        "unexpected sample interval error: {error:#}"
    );
    assert_eq!(
        before,
        snapshot_tree(&root),
        "invalid sample interval must not mutate campaign evidence"
    );

    fs::remove_dir_all(root).expect("remove Slice G interval evidence");
}

#[test]
fn run_next_cli_exposes_no_arbitrary_sequence_argument() {
    let root = temp_root("cli");
    let output = Command::new(endurancectl_binary())
        .arg("run-next")
        .arg("--database-url")
        .arg("postgres://unused")
        .arg("--evidence-root")
        .arg(&root)
        .arg("--campaign-id")
        .arg("slicegcli")
        .arg("--sequence")
        .arg("99")
        .output()
        .expect("invoke run-next CLI with forbidden sequence");

    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("unexpected argument '--sequence'"),
        "run-next must reject arbitrary sequence input: {stderr}"
    );
}
