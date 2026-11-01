#![cfg(unix)]

//! Campaign #203 / #215 Slice D: durable campaign and per-iteration evidence
//! for one bounded clean or scheduled crash/recovery execution.

mod support;

use std::fs;
use std::path::{Path, PathBuf};

use postgres_postgres::endurance::evidence::{execute_recorded_iteration, EvidenceStore};
use postgres_postgres::endurance::{
    plan_iteration, CANONICAL_CHUNK_SIZE, CANONICAL_CURSOR_FETCH_SIZE, CANONICAL_PAGING_PAGE_SIZE,
    CANONICAL_SEED, CANONICAL_SOURCE_ROWS,
};
use serde_json::{json, Value};
use support::SeedOptions;

const CRASH_ROWS: u64 = 3_500;

fn workload_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres"))
}

fn temp_root(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "postgres-postgres-sliced-{label}-{}",
        support::nonce()
    ))
}

fn read_events(path: &Path) -> Vec<Value> {
    let text = fs::read_to_string(path).expect("read events.jsonl");
    text.lines()
        .map(|line| serde_json::from_str(line).expect("parse event JSON line"))
        .collect()
}

fn evidence_text(root: &Path) -> String {
    fn visit(path: &Path, output: &mut String) {
        for entry in fs::read_dir(path).expect("read evidence directory") {
            let entry = entry.expect("read evidence entry");
            let path = entry.path();
            if path.is_dir() {
                visit(&path, output);
            } else {
                output.push_str(&fs::read_to_string(&path).expect("read evidence file"));
            }
        }
    }

    let mut output = String::new();
    visit(root, &mut output);
    output
}

fn assert_no_temp_files(root: &Path) {
    fn visit(path: &Path) {
        for entry in fs::read_dir(path).expect("read evidence directory") {
            let entry = entry.expect("read evidence entry");
            let path = entry.path();
            if path.is_dir() {
                visit(&path);
                continue;
            }
            let name = path
                .file_name()
                .and_then(|name| name.to_str())
                .expect("UTF-8 evidence file name");
            assert!(
                !name.contains(".tmp-"),
                "atomic evidence writer left temporary file behind: {}",
                path.display()
            );
        }
    }

    visit(root);
}

#[test]
fn campaign_initialization_is_idempotent_append_only_and_conflict_safe() {
    let root = temp_root("init");
    let campaign_id = format!("evidence{}", support::nonce());
    let binary = workload_binary();

    let store = EvidenceStore::open(&root, &campaign_id, &binary).expect("initialize evidence");
    let campaign_path = store.campaign_dir().join("campaign.json");
    let first_campaign_bytes = fs::read(&campaign_path).expect("read campaign.json");

    assert!(campaign_path.is_file());
    assert!(store.events_path().is_file());
    assert!(store.campaign_dir().join("iterations").is_dir());
    assert!(store.campaign_dir().join("failures").is_dir());
    assert_eq!(store.campaign().campaign_id, campaign_id);
    assert_eq!(store.campaign().oxide_batch_version, "0.6.0");
    assert_eq!(store.campaign().source_rows, CANONICAL_SOURCE_ROWS);
    assert_eq!(store.campaign().seed, CANONICAL_SEED);
    assert_eq!(store.campaign().chunk_size, CANONICAL_CHUNK_SIZE);
    assert_eq!(
        store.campaign().cursor_fetch_size,
        CANONICAL_CURSOR_FETCH_SIZE
    );
    assert_eq!(
        store.campaign().paging_page_size,
        CANONICAL_PAGING_PAGE_SIZE
    );

    store
        .record_event(1, "test-first", json!({ "ordinal": 1 }))
        .expect("append first event");
    store
        .record_event(1, "test-second", json!({ "ordinal": 2 }))
        .expect("append second event");
    let events = read_events(store.events_path());
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["phase"], "test-first");
    assert_eq!(events[1]["phase"], "test-second");

    let reopened =
        EvidenceStore::open(&root, &campaign_id, &binary).expect("idempotently reopen evidence");
    assert_eq!(
        fs::read(&campaign_path).expect("re-read campaign.json"),
        first_campaign_bytes,
        "reopening the same campaign must not rewrite its immutable metadata"
    );
    assert_eq!(read_events(reopened.events_path()).len(), 2);

    let fake_binary = root.join("different-workload-binary");
    fs::write(&fake_binary, b"different binary identity").expect("write conflicting binary");
    let error = EvidenceStore::open(&root, &campaign_id, &fake_binary)
        .expect_err("conflicting campaign identity must fail closed");
    assert!(
        error.to_string().contains("conflicts"),
        "unexpected conflict error: {error:#}"
    );
    assert_eq!(
        fs::read(&campaign_path).expect("campaign remains readable"),
        first_campaign_bytes,
        "conflicting initialization must not overwrite campaign.json"
    );

    assert_no_temp_files(&root);
    fs::remove_dir_all(root).expect("remove Slice D init evidence");
}

#[tokio::test]
async fn recorded_clean_iteration_persists_ordered_terminal_evidence_without_secrets() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 137,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("clean");
    let campaign_id = format!("recordclean{}", support::nonce());
    let database_url = support::database_url();
    let result =
        execute_recorded_iteration(&workload_binary(), &database_url, &root, &campaign_id, 1)
            .await
            .expect("execute recorded clean iteration");

    let iteration_path = PathBuf::from(&result.iteration_path);
    let iteration: Value =
        serde_json::from_reader(fs::File::open(&iteration_path).expect("open iteration evidence"))
            .expect("parse iteration evidence");
    assert_eq!(iteration["terminal"], "success");
    assert_eq!(iteration["execution"]["kind"], "clean");
    assert_eq!(iteration["execution"]["result"]["sequence"], 1);
    assert_eq!(
        iteration["execution"]["result"]["verification"]["success"],
        true
    );

    let events = read_events(Path::new(&result.events_path));
    let phases: Vec<&str> = events
        .iter()
        .map(|event| event["phase"].as_str().expect("event phase"))
        .collect();
    assert_eq!(
        phases,
        vec![
            "iteration-planned",
            "iteration-started",
            "workload-child-started",
            "workload-child-completed",
            "verification-child-started",
            "verification-child-completed",
            "cleanup-completed",
            "iteration-terminal-success",
        ]
    );

    let all_evidence = evidence_text(&root);
    assert!(
        !all_evidence.contains(&database_url),
        "evidence must not contain the database URL"
    );
    assert!(
        !all_evidence.contains("oxide_batch_workload:oxide_batch_workload"),
        "evidence must not contain database credentials"
    );
    assert_no_temp_files(&root);

    let import_name = plan_iteration(&campaign_id, 1)
        .expect("clean plan")
        .import_name;
    let pool = support::pool().await;
    assert!(
        support::job_instance_count(&pool, &import_name).await > 0,
        "evidence persistence must not remove framework metadata"
    );
    pool.close().await;

    fs::remove_dir_all(root).expect("remove Slice D clean evidence");
}

#[tokio::test]
async fn recorded_crash_iteration_persists_sigkill_recovery_lifecycle() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: CRASH_ROWS,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("crash");
    let campaign_id = format!("recordcrash{}", support::nonce());
    let result = execute_recorded_iteration(
        &workload_binary(),
        &support::database_url(),
        &root,
        &campaign_id,
        24,
    )
    .await
    .expect("execute recorded crash iteration");

    let iteration: Value = serde_json::from_reader(
        fs::File::open(&result.iteration_path).expect("open crash iteration evidence"),
    )
    .expect("parse crash iteration evidence");
    assert_eq!(iteration["terminal"], "success");
    assert_eq!(iteration["execution"]["kind"], "crash");
    assert_eq!(iteration["execution"]["result"]["failure"], "before-commit");
    assert_eq!(iteration["execution"]["result"]["crash"]["signal"], 9);
    assert_eq!(
        iteration["execution"]["result"]["verification"]["success"],
        true
    );

    let events = read_events(Path::new(&result.events_path));
    let phases: Vec<&str> = events
        .iter()
        .map(|event| event["phase"].as_str().expect("event phase"))
        .collect();
    for required in [
        "crash-marker-observed",
        "sigkill-delivered",
        "crash-child-reaped",
        "post-crash-state-observed",
        "plain-relaunch-child-completed",
        "recovery-child-completed",
        "continuation-child-completed",
        "verification-child-completed",
        "cleanup-completed",
        "iteration-terminal-success",
    ] {
        assert!(
            phases.contains(&required),
            "recorded crash evidence is missing phase {required}: {phases:?}"
        );
    }

    let marker_index = phases
        .iter()
        .position(|phase| *phase == "crash-marker-observed")
        .expect("marker phase");
    let kill_index = phases
        .iter()
        .position(|phase| *phase == "sigkill-delivered")
        .expect("kill phase");
    let recovery_index = phases
        .iter()
        .position(|phase| *phase == "recovery-child-completed")
        .expect("recovery phase");
    let verify_index = phases
        .iter()
        .position(|phase| *phase == "verification-child-completed")
        .expect("verify phase");
    assert!(marker_index < kill_index);
    assert!(kill_index < recovery_index);
    assert!(recovery_index < verify_index);

    assert_no_temp_files(&root);
    fs::remove_dir_all(root).expect("remove Slice D crash evidence");
}

fn failing_verify_wrapper() -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    let actual = env!("CARGO_BIN_EXE_postgres-postgres");
    let path = std::env::temp_dir().join(format!(
        "postgres-postgres-sliced-fail-{}.sh",
        support::nonce()
    ));
    let script = format!(
        "#!/bin/sh\nif [ \"$1\" = \"verify\" ]; then exit 73; fi\nexec \"{actual}\" \"$@\"\n"
    );
    fs::write(&path, script).expect("write Slice D verify-failure wrapper");
    let mut permissions = fs::metadata(&path)
        .expect("stat Slice D verify-failure wrapper")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&path, permissions).expect("chmod Slice D verify-failure wrapper");
    path
}

#[tokio::test]
async fn recorded_failure_preserves_diagnostics_without_success_or_secret_leakage() {
    support::migrate();
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: 83,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("failure");
    let campaign_id = format!("recordfail{}", support::nonce());
    let database_url = support::database_url();
    let wrapper = failing_verify_wrapper();
    let plan = plan_iteration(&campaign_id, 1).expect("clean failure plan");

    let error = execute_recorded_iteration(&wrapper, &database_url, &root, &campaign_id, 1)
        .await
        .expect_err("forced verifier failure must fail the recorded iteration");
    assert!(
        error.to_string().contains("verify child failed"),
        "unexpected execution error: {error:#}"
    );

    let campaign_dir = root.join(&campaign_id);
    assert!(
        !campaign_dir.join("iterations/000001.json").exists(),
        "failed iteration must not emit terminal success evidence"
    );

    let failures: Vec<PathBuf> = fs::read_dir(campaign_dir.join("failures"))
        .expect("read failures directory")
        .map(|entry| entry.expect("read failure entry").path())
        .collect();
    assert_eq!(failures.len(), 1);
    let failure: Value =
        serde_json::from_reader(fs::File::open(&failures[0]).expect("open failure evidence"))
            .expect("parse failure evidence");
    assert_eq!(failure["terminal"], "failure");
    assert_eq!(failure["failure_class"], "verification");
    let failure_text = fs::read_to_string(&failures[0]).expect("read failure evidence");
    assert!(!failure_text.contains(&database_url));
    assert!(!failure_text.contains("oxide_batch_workload:oxide_batch_workload"));

    let events = read_events(&campaign_dir.join("events.jsonl"));
    assert_eq!(
        events.last().and_then(|event| event["phase"].as_str()),
        Some("iteration-terminal-failure")
    );
    assert!(!events
        .iter()
        .any(|event| event["phase"] == "iteration-terminal-success"));

    let pool = support::pool().await;
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest after recorded verifier failure");
    assert_eq!(
        support::destination_row_count(&pool, &plan.import_name, &source_digest).await,
        dataset.rows as i64,
        "failed verification must preserve business rows for diagnosis"
    );
    pool.close().await;

    assert_no_temp_files(&root);
    fs::remove_file(wrapper).expect("remove Slice D verify-failure wrapper");
    fs::remove_dir_all(root).expect("remove Slice D failure evidence");
}
