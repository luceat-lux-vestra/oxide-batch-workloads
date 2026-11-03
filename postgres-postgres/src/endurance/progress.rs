use std::future::Future;
use std::path::Path;
use std::time::Duration;

use anyhow::{bail, Result};
use serde::Serialize;

use super::resume::{reconcile_resume_status, ResumeState, ResumeStatus};
use super::sampling::{
    execute_sampled_recorded_iteration, SampledRecordedIterationOutput,
};
use super::IterationPlan;

const RUN_NEXT_SCHEMA: &str = "oxide-batch-workloads.endurance-run-next";
const SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Serialize)]
pub struct RunNextResult {
    pub schema: &'static str,
    pub schema_version: u32,
    pub campaign_id: String,
    pub pre_execution: ResumeStatus,
    pub selected_sequence: Option<u64>,
    pub selected_plan: Option<IterationPlan>,
    pub execution: Option<SampledRecordedIterationOutput>,
}

pub async fn execute_next_sampled_recorded_iteration(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    sample_interval: Duration,
) -> Result<RunNextResult> {
    execute_next_with(
        workload_binary,
        evidence_root,
        campaign_id,
        |plan| async move {
            execute_sampled_recorded_iteration(
                workload_binary,
                database_url,
                evidence_root,
                campaign_id,
                plan.sequence,
                sample_interval,
            )
            .await
        },
    )
    .await
}

async fn execute_next_with<F, Fut>(
    workload_binary: &Path,
    evidence_root: &Path,
    campaign_id: &str,
    executor: F,
) -> Result<RunNextResult>
where
    F: FnOnce(IterationPlan) -> Fut,
    Fut: Future<Output = Result<SampledRecordedIterationOutput>>,
{
    let pre_execution =
        reconcile_resume_status(evidence_root, campaign_id, workload_binary)?;

    if pre_execution.state != ResumeState::Ready {
        return Ok(RunNextResult {
            schema: RUN_NEXT_SCHEMA,
            schema_version: SCHEMA_VERSION,
            campaign_id: campaign_id.to_owned(),
            pre_execution,
            selected_sequence: None,
            selected_plan: None,
            execution: None,
        });
    }

    let selected_sequence = pre_execution
        .next_sequence
        .ok_or_else(|| anyhow::anyhow!("ready resume status is missing next_sequence"))?;
    let selected_plan = pre_execution
        .next_plan
        .clone()
        .ok_or_else(|| anyhow::anyhow!("ready resume status is missing next_plan"))?;

    if selected_plan.sequence != selected_sequence {
        bail!(
            "ready resume status disagrees on next sequence: status={} plan={}",
            selected_sequence,
            selected_plan.sequence
        );
    }
    if selected_plan.campaign_id != campaign_id {
        bail!(
            "ready resume plan campaign {:?} disagrees with requested campaign {:?}",
            selected_plan.campaign_id,
            campaign_id
        );
    }

    let execution = executor(selected_plan.clone()).await?;

    Ok(RunNextResult {
        schema: RUN_NEXT_SCHEMA,
        schema_version: SCHEMA_VERSION,
        campaign_id: campaign_id.to_owned(),
        pre_execution,
        selected_sequence: Some(selected_sequence),
        selected_plan: Some(selected_plan),
        execution: Some(execution),
    })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    use anyhow::anyhow;
    use serde_json::json;

    use super::{execute_next_with, SampledRecordedIterationOutput};
    use crate::endurance::evidence::EvidenceStore;
    use crate::endurance::{EnduranceFailure, EnduranceReader};

    static NONCE: AtomicU64 = AtomicU64::new(1);

    fn workload_binary() -> PathBuf {
        std::env::current_exe().expect("resolve current test executable")
    }

    fn temp_root(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "postgres-postgres-sliceg-unit-{label}-{}-{}",
            std::process::id(),
            NONCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    fn new_store(root: &Path, campaign_id: &str) -> EvidenceStore {
        EvidenceStore::open(root, campaign_id, &workload_binary())
            .expect("initialize Slice G unit evidence")
    }

    fn write_success(store: &EvidenceStore, sequence: u64) {
        let record = json!({
            "schema": "oxide-batch-workloads.endurance-iteration",
            "schema_version": 1,
            "timestamp_unix_nanos_utc": 1_000_000_u64 + sequence,
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
            serde_json::to_vec_pretty(&record).expect("serialize Slice G success"),
        )
        .expect("write Slice G success singleton");
        store
            .record_event(
                sequence,
                "iteration-terminal-success",
                json!({ "test": "slice-g" }),
            )
            .expect("record Slice G terminal success");
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
    async fn ready_frontier_hands_off_exact_existing_plan_once() {
        let root = temp_root("ready");
        let campaign_id = format!("slicegready{}", NONCE.fetch_add(1, Ordering::Relaxed));
        let store = new_store(&root, &campaign_id);

        let calls = Arc::new(AtomicUsize::new(0));
        let captured = Arc::new(Mutex::new(None));
        let calls_for_executor = calls.clone();
        let captured_for_executor = captured.clone();
        let error = execute_next_with(
            &workload_binary(),
            &root,
            &campaign_id,
            move |plan| {
                calls_for_executor.fetch_add(1, Ordering::SeqCst);
                *captured_for_executor.lock().expect("lock captured plan") = Some(plan);
                async {
                    Err::<SampledRecordedIterationOutput, _>(anyhow!("sentinel executor stop"))
                }
            },
        )
        .await
        .expect_err("sentinel executor must stop fresh handoff");
        assert!(error.to_string().contains("sentinel executor stop"));
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let fresh = captured
            .lock()
            .expect("lock fresh plan")
            .take()
            .expect("capture fresh plan");
        assert_eq!(fresh.sequence, 1);
        assert_eq!(fresh.reader, EnduranceReader::Cursor);
        assert_eq!(fresh.failure, EnduranceFailure::Clean);

        for sequence in 1..=23 {
            write_success(&store, sequence);
        }

        calls.store(0, Ordering::SeqCst);
        let captured_for_executor = captured.clone();
        let calls_for_executor = calls.clone();
        let error = execute_next_with(
            &workload_binary(),
            &root,
            &campaign_id,
            move |plan| {
                calls_for_executor.fetch_add(1, Ordering::SeqCst);
                *captured_for_executor.lock().expect("lock crash plan") = Some(plan);
                async {
                    Err::<SampledRecordedIterationOutput, _>(anyhow!("sentinel crash stop"))
                }
            },
        )
        .await
        .expect_err("sentinel executor must stop crash handoff");
        assert!(error.to_string().contains("sentinel crash stop"));
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let before_commit = captured
            .lock()
            .expect("lock before-commit plan")
            .take()
            .expect("capture before-commit plan");
        assert_eq!(before_commit.sequence, 24);
        assert_eq!(before_commit.reader, EnduranceReader::Paging);
        assert_eq!(before_commit.failure, EnduranceFailure::BeforeCommit);

        for sequence in 24..=48 {
            write_success(&store, sequence);
        }

        calls.store(0, Ordering::SeqCst);
        let captured_for_executor = captured.clone();
        let calls_for_executor = calls.clone();
        let error = execute_next_with(
            &workload_binary(),
            &root,
            &campaign_id,
            move |plan| {
                calls_for_executor.fetch_add(1, Ordering::SeqCst);
                *captured_for_executor.lock().expect("lock after-commit plan") = Some(plan);
                async {
                    Err::<SampledRecordedIterationOutput, _>(anyhow!("sentinel after-commit stop"))
                }
            },
        )
        .await
        .expect_err("sentinel executor must stop after-commit handoff");
        assert!(error.to_string().contains("sentinel after-commit stop"));
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let after_commit = captured
            .lock()
            .expect("lock after-commit plan")
            .take()
            .expect("capture after-commit plan");
        assert_eq!(after_commit.sequence, 49);
        assert_eq!(after_commit.reader, EnduranceReader::Cursor);
        assert_eq!(after_commit.failure, EnduranceFailure::AfterCommit);

        fs::remove_dir_all(root).expect("remove Slice G ready unit evidence");
    }

    #[tokio::test]
    async fn blocked_resume_states_never_invoke_executor_or_mutate_evidence() {
        for case in ["failure", "incomplete", "inconsistent"] {
            let root = temp_root(case);
            let campaign_id = format!(
                "sliceg{case}{}",
                NONCE.fetch_add(1, Ordering::Relaxed)
            );
            let store = new_store(&root, &campaign_id);
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
            let calls = Arc::new(AtomicUsize::new(0));
            let calls_for_executor = calls.clone();
            let result = execute_next_with(
                &workload_binary(),
                &root,
                &campaign_id,
                move |_| {
                    calls_for_executor.fetch_add(1, Ordering::SeqCst);
                    async {
                        Err::<SampledRecordedIterationOutput, _>(anyhow!(
                            "blocked executor must not run"
                        ))
                    }
                },
            )
            .await
            .expect("blocked state returns structured result");
            let after = snapshot_tree(&root);

            assert_ne!(result.pre_execution.state, crate::endurance::resume::ResumeState::Ready);
            assert_eq!(result.selected_sequence, None);
            assert!(result.selected_plan.is_none());
            assert!(result.execution.is_none());
            assert_eq!(calls.load(Ordering::SeqCst), 0);
            assert_eq!(before, after, "blocked {case} must remain byte-for-byte read-only");

            fs::remove_dir_all(root).expect("remove Slice G blocked unit evidence");
        }
    }
}
