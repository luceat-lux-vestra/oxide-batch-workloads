use std::future::Future;
use std::path::Path;
use std::time::Duration;

use anyhow::{bail, Result};
use serde::Serialize;

use super::evidence::EvidenceStore;
use super::resume::{reconcile_resume_status, ResumeState, ResumeStatus};
use super::sampling::{execute_sampled_recorded_iteration, SampledRecordedIterationOutput};
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

pub(crate) fn initialize_campaign_if_absent(
    evidence_root: &Path,
    campaign_id: &str,
    workload_binary: &Path,
) -> Result<()> {
    let campaign_dir = evidence_root.join(campaign_id);
    if campaign_dir.try_exists()? {
        return Ok(());
    }

    let store = EvidenceStore::open(evidence_root, campaign_id, workload_binary)?;
    drop(store);
    Ok(())
}

pub async fn execute_next_sampled_recorded_iteration(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    sample_interval: Duration,
) -> Result<RunNextResult> {
    initialize_campaign_if_absent(evidence_root, campaign_id, workload_binary)?;
    let pre_execution = reconcile_resume_status(evidence_root, campaign_id, workload_binary)?;
    execute_selected_sampled_recorded_iteration(
        workload_binary,
        database_url,
        evidence_root,
        campaign_id,
        pre_execution,
        sample_interval,
    )
    .await
}

pub async fn execute_selected_sampled_recorded_iteration(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    pre_execution: ResumeStatus,
    sample_interval: Duration,
) -> Result<RunNextResult> {
    execute_selected_with(pre_execution, campaign_id, |plan| async move {
        execute_sampled_recorded_iteration(
            workload_binary,
            database_url,
            evidence_root,
            campaign_id,
            plan.sequence,
            sample_interval,
        )
        .await
    })
    .await
}

async fn execute_selected_with<F, Fut>(
    pre_execution: ResumeStatus,
    campaign_id: &str,
    executor: F,
) -> Result<RunNextResult>
where
    F: FnOnce(IterationPlan) -> Fut,
    Fut: Future<Output = Result<SampledRecordedIterationOutput>>,
{
    if pre_execution.campaign_id != campaign_id {
        bail!(
            "resume status campaign {:?} disagrees with requested campaign {:?}",
            pre_execution.campaign_id,
            campaign_id
        );
    }

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
