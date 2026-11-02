#![cfg(any(target_os = "linux", target_os = "android"))]

//! Campaign #203 / #218 Slice E: bounded append-only resource sampling for
//! one existing recorded clean or scheduled crash/recovery iteration.

mod support;

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use postgres_postgres::endurance::sampling::{
    execute_sampled_recorded_iteration, execute_sampled_recorded_iteration_with_probe,
    FilesystemObservation, LinuxResourceProbe, ProcessObservation, ResourceProbe,
};
use postgres_postgres::endurance::CANONICAL_SEED;
use serde_json::Value;
use support::SeedOptions;

const SAMPLE_INTERVAL: Duration = Duration::from_millis(10);
const CRASH_ROWS: u64 = 3_500;

fn workload_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_postgres-postgres"))
}

fn temp_root(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "postgres-postgres-slicee-{label}-{}",
        support::nonce()
    ))
}

fn read_samples(path: &Path) -> Vec<Value> {
    let text = fs::read_to_string(path).expect("read samples.jsonl");
    text.lines()
        .map(|line| serde_json::from_str(line).expect("parse resource sample JSON"))
        .collect()
}

fn assert_common_sample(sample: &Value, campaign_id: &str, sequence: u64) {
    assert_eq!(sample["schema"], "oxide-batch-workloads.endurance-sample");
    assert_eq!(sample["schema_version"], 1);
    assert_eq!(sample["campaign_id"], campaign_id);
    assert_eq!(sample["sequence"], sequence);
    assert_eq!(sample["sample_interval_millis"], 10);
    assert!(sample["timestamp_unix_nanos_utc"].as_u64().is_some());
    assert!(sample["sample_kind"].as_str().is_some());

    assert!(sample["postgres"]["total_connections"]
        .as_i64()
        .is_some_and(|value| value >= 1));
    assert!(sample["postgres"]["active_connections"]
        .as_i64()
        .is_some_and(|value| value >= 1));
    assert!(sample["postgres"]["database_size_bytes"]
        .as_i64()
        .is_some_and(|value| value > 0));

    for field in ["job_instances", "job_executions", "step_executions"] {
        assert!(
            sample["oxide_batch"][field]
                .as_i64()
                .is_some_and(|value| value >= 0),
            "missing metadata count {field}: {sample}"
        );
    }
    for field in [
        "job_instance_table_bytes",
        "job_execution_table_bytes",
        "step_execution_table_bytes",
    ] {
        assert!(
            sample["oxide_batch"][field]
                .as_i64()
                .is_some_and(|value| value > 0),
            "missing metadata table size {field}: {sample}"
        );
    }

    let filesystem = &sample["filesystem"];
    assert!(filesystem["total_bytes"]
        .as_u64()
        .is_some_and(|value| value > 0));
    assert!(filesystem["used_bytes"].as_u64().is_some());
    assert!(filesystem["available_bytes"].as_u64().is_some());
}

#[tokio::test]
async fn sampled_clean_iterations_append_measurements_and_stop_after_return() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 500,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("clean");
    let campaign_id = format!("sampleclean{}", support::nonce());
    let database_url = support::database_url();

    let first = execute_sampled_recorded_iteration(
        &workload_binary(),
        &database_url,
        &root,
        &campaign_id,
        1,
        SAMPLE_INTERVAL,
    )
    .await
    .expect("execute first sampled clean iteration");
    assert!(first.samples_written > 0);

    let samples_path = PathBuf::from(&first.samples_path);
    let first_text = fs::read_to_string(&samples_path).expect("read first samples");
    let first_samples = read_samples(&samples_path);
    assert!(!first_samples.is_empty());
    for sample in &first_samples {
        assert_common_sample(sample, &campaign_id, 1);
    }

    let clean_process = first_samples
        .iter()
        .filter_map(|sample| sample["process"].as_object())
        .find(|process| process["role"] == "clean-workload")
        .expect("clean-workload process sample");
    assert!(clean_process["pid"].as_u64().is_some());
    let rss_recorded = clean_process["rss_bytes"].as_u64().is_some();
    let cpu_recorded = clean_process["cpu_time_ticks"].as_u64().is_some();
    let explicit_unavailable = clean_process["unavailable_reason"].as_str().is_some();
    assert!(rss_recorded || explicit_unavailable);
    assert!(cpu_recorded || explicit_unavailable);

    assert!(first_samples
        .iter()
        .any(|sample| sample["sample_kind"] == "sampler-start"));
    assert!(first_samples
        .iter()
        .any(|sample| sample["sample_kind"] == "post-cleanup"));
    assert!(first_samples
        .iter()
        .any(|sample| sample["sample_kind"] == "terminal-success"));
    assert!(
        first_samples.iter().any(|sample| {
            sample["oxide_batch"]["job_instances"]
                .as_i64()
                .is_some_and(|value| value > 0)
        }),
        "successful execution must leave observable framework metadata"
    );

    let second = execute_sampled_recorded_iteration(
        &workload_binary(),
        &database_url,
        &root,
        &campaign_id,
        2,
        SAMPLE_INTERVAL,
    )
    .await
    .expect("execute second sampled clean iteration");
    assert!(second.samples_written > 0);
    assert_eq!(first.samples_path, second.samples_path);

    let second_text = fs::read_to_string(&samples_path).expect("read appended samples");
    assert!(
        second_text.starts_with(&first_text),
        "starting another sampled iteration must never truncate prior samples"
    );
    let all_samples = read_samples(&samples_path);
    assert!(all_samples.len() > first_samples.len());
    assert!(all_samples
        .iter()
        .any(|sample| sample["sequence"] == 2 && sample["sample_kind"] == "terminal-success"));

    let stable_text = fs::read_to_string(&samples_path).expect("snapshot stopped sampler output");
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(
        fs::read_to_string(&samples_path).expect("re-read stopped sampler output"),
        stable_text,
        "sampler must not keep running after bounded iteration returns"
    );

    let evidence = fs::read_to_string(&samples_path).expect("read secret-safe samples");
    assert!(!evidence.contains(&database_url));
    assert!(!evidence.contains("oxide_batch_workload:oxide_batch_workload"));

    fs::remove_dir_all(root).expect("remove Slice E clean evidence");
}

#[tokio::test]
async fn sampled_crash_iteration_attributes_real_process_roles() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: CRASH_ROWS,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("crash");
    let campaign_id = format!("samplecrash{}", support::nonce());
    let database_url = support::database_url();
    let result = execute_sampled_recorded_iteration(
        &workload_binary(),
        &database_url,
        &root,
        &campaign_id,
        24,
        SAMPLE_INTERVAL,
    )
    .await
    .expect("execute sampled crash iteration");

    let samples = read_samples(Path::new(&result.samples_path));
    for sample in &samples {
        assert_common_sample(sample, &campaign_id, 24);
    }

    let roles = samples
        .iter()
        .filter_map(|sample| sample["process"]["role"].as_str())
        .collect::<Vec<_>>();
    for required in [
        "crash-target",
        "plain-relaunch",
        "recovery",
        "continuation",
        "verifier",
    ] {
        assert!(
            roles.contains(&required),
            "missing sampled process role {required}: {roles:?}"
        );
    }

    for required in [
        "crash-marker-observed",
        "post-crash-state",
        "post-cleanup",
        "terminal-success",
    ] {
        assert!(
            samples
                .iter()
                .any(|sample| sample["sample_kind"] == required),
            "missing crash resource boundary {required}"
        );
    }

    let evidence = fs::read_to_string(&result.samples_path).expect("read crash samples");
    assert!(!evidence.contains(&database_url));
    assert!(!evidence.contains("oxide_batch_workload:oxide_batch_workload"));

    fs::remove_dir_all(root).expect("remove Slice E crash evidence");
}

fn failing_verify_wrapper() -> PathBuf {
    use std::os::unix::fs::PermissionsExt;

    let actual = env!("CARGO_BIN_EXE_postgres-postgres");
    let path = std::env::temp_dir().join(format!(
        "postgres-postgres-slicee-fail-{}.sh",
        support::nonce()
    ));
    let script = format!(
        "#!/bin/sh\nif [ \"$1\" = \"verify\" ]; then exit 73; fi\nexec \"{actual}\" \"$@\"\n"
    );
    fs::write(&path, script).expect("write Slice E verify-failure wrapper");
    let mut permissions = fs::metadata(&path)
        .expect("stat Slice E verify-failure wrapper")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&path, permissions).expect("chmod Slice E verify-failure wrapper");
    path
}

#[tokio::test]
async fn sampled_execution_failure_stops_sampler_and_preserves_failure_evidence() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 83,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("execution-failure");
    let campaign_id = format!("samplefail{}", support::nonce());
    let database_url = support::database_url();
    let wrapper = failing_verify_wrapper();

    let error = execute_sampled_recorded_iteration(
        &wrapper,
        &database_url,
        &root,
        &campaign_id,
        1,
        SAMPLE_INTERVAL,
    )
    .await
    .expect_err("forced verifier failure must fail sampled iteration");
    assert!(
        error.to_string().contains("verify child failed"),
        "unexpected sampled execution error: {error:#}"
    );

    let campaign_dir = root.join(&campaign_id);
    assert!(!campaign_dir.join("iterations/000001.json").exists());

    let failures = fs::read_dir(campaign_dir.join("failures"))
        .expect("read sampled failure directory")
        .map(|entry| entry.expect("read sampled failure entry").path())
        .collect::<Vec<_>>();
    assert_eq!(failures.len(), 1);
    let failure: Value =
        serde_json::from_reader(fs::File::open(&failures[0]).expect("open sampled failure"))
            .expect("parse sampled failure");
    assert_eq!(failure["terminal"], "failure");
    assert_eq!(failure["failure_class"], "verification");

    let samples_path = campaign_dir.join("samples.jsonl");
    let samples = read_samples(&samples_path);
    assert!(!samples.is_empty());
    assert_eq!(
        samples
            .last()
            .and_then(|sample| sample["sample_kind"].as_str()),
        Some("terminal-failure")
    );

    let stable_text = fs::read_to_string(&samples_path).expect("snapshot failure samples");
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(
        fs::read_to_string(&samples_path).expect("re-read failure samples"),
        stable_text
    );
    assert!(!stable_text.contains(&database_url));
    assert!(!stable_text.contains("oxide_batch_workload:oxide_batch_workload"));

    fs::remove_file(wrapper).expect("remove Slice E verify-failure wrapper");
    fs::remove_dir_all(root).expect("remove Slice E execution-failure evidence");
}

struct FailAfterOneFilesystemProbe {
    inner: LinuxResourceProbe,
    filesystem_calls: AtomicUsize,
}

impl ResourceProbe for FailAfterOneFilesystemProbe {
    fn process(&self, pid: u32, role: &str) -> anyhow::Result<ProcessObservation> {
        self.inner.process(pid, role)
    }

    fn filesystem(&self, path: &Path) -> anyhow::Result<FilesystemObservation> {
        let call = self.filesystem_calls.fetch_add(1, Ordering::SeqCst);
        if call >= 1 {
            anyhow::bail!("injected resource sampler filesystem failure");
        }
        self.inner.filesystem(path)
    }
}

#[tokio::test]
async fn sampler_failure_is_harness_failure_and_never_emits_success_iteration() {
    support::migrate();
    support::reset();
    support::seed(SeedOptions {
        rows: 137,
        seed: CANONICAL_SEED,
    });

    let root = temp_root("sampler-failure");
    let campaign_id = format!("samplerfail{}", support::nonce());
    let database_url = support::database_url();
    let probe = Arc::new(FailAfterOneFilesystemProbe {
        inner: LinuxResourceProbe,
        filesystem_calls: AtomicUsize::new(0),
    });

    let error = execute_sampled_recorded_iteration_with_probe(
        &workload_binary(),
        &database_url,
        &root,
        &campaign_id,
        1,
        SAMPLE_INTERVAL,
        probe,
    )
    .await
    .expect_err("injected sampler failure must fail the recorded iteration");
    assert!(
        format!("{error:#}").contains("injected resource sampler filesystem failure"),
        "unexpected sampler failure: {error:#}"
    );

    let campaign_dir = root.join(&campaign_id);
    assert!(!campaign_dir.join("iterations/000001.json").exists());

    let failures = fs::read_dir(campaign_dir.join("failures"))
        .expect("read sampler failure directory")
        .map(|entry| entry.expect("read sampler failure entry").path())
        .collect::<Vec<_>>();
    assert_eq!(failures.len(), 1);
    let failure: Value =
        serde_json::from_reader(fs::File::open(&failures[0]).expect("open sampler failure"))
            .expect("parse sampler failure");
    assert_eq!(failure["failure_class"], "harness-environment");

    let samples_path = campaign_dir.join("samples.jsonl");
    let samples = read_samples(&samples_path);
    assert!(
        !samples.is_empty(),
        "the sampler must retain samples collected before its injected failure"
    );
    let sample_text = fs::read_to_string(&samples_path).expect("read retained samples");
    assert!(!sample_text.contains(&database_url));
    assert!(!sample_text.contains("oxide_batch_workload:oxide_batch_workload"));

    fs::remove_dir_all(root).expect("remove Slice E sampler-failure evidence");
}
