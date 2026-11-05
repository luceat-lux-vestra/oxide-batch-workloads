use std::path::Path;
use std::time::Duration;

use anyhow::{bail, Result};
use serde::Serialize;

use super::progress::execute_next_sampled_recorded_iteration;
use super::resume::{reconcile_resume_status, ResumeState, ResumeStatus};

const RUN_BATCH_SCHEMA: &str = "oxide-batch-workloads.endurance-run-batch";
const SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Serialize)]
pub struct RunBatchResult {
    pub schema: &'static str,
    pub schema_version: u32,
    pub campaign_id: String,
    pub requested_iterations: u64,
    pub completed_iterations: u64,
    pub starting_frontier_sequence: u64,
    pub completed_sequences: Vec<u64>,
    pub terminal_resume_status: ResumeStatus,
}

pub async fn execute_bounded_batch(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    requested_iterations: u64,
    sample_interval: Duration,
) -> Result<RunBatchResult> {
    if requested_iterations == 0 {
        bail!("requested iterations must be greater than zero");
    }

    let mut completed_sequences = Vec::new();
    let mut starting_frontier_sequence = None;
    let mut terminal_resume_status = None;

    for _ in 0..requested_iterations {
        let step = execute_next_sampled_recorded_iteration(
            workload_binary,
            database_url,
            evidence_root,
            campaign_id,
            sample_interval,
        )
        .await?;

        if starting_frontier_sequence.is_none() {
            starting_frontier_sequence = Some(step.pre_execution.frontier_sequence);
        }

        if step.pre_execution.state != ResumeState::Ready {
            terminal_resume_status = Some(step.pre_execution);
            break;
        }

        let selected_sequence = step
            .selected_sequence
            .ok_or_else(|| anyhow::anyhow!("ready Slice G step is missing selected_sequence"))?;
        if step.execution.is_none() {
            bail!(
                "ready Slice G step returned no execution result for sequence {selected_sequence}"
            );
        }

        completed_sequences.push(selected_sequence);
    }

    let starting_frontier_sequence = starting_frontier_sequence
        .ok_or_else(|| anyhow::anyhow!("bounded batch did not evaluate a resume frontier"))?;
    let terminal_resume_status = match terminal_resume_status {
        Some(status) => status,
        None => reconcile_resume_status(evidence_root, campaign_id, workload_binary)?,
    };
    let completed_iterations = u64::try_from(completed_sequences.len())
        .map_err(|_| anyhow::anyhow!("completed iteration count does not fit u64"))?;

    Ok(RunBatchResult {
        schema: RUN_BATCH_SCHEMA,
        schema_version: SCHEMA_VERSION,
        campaign_id: campaign_id.to_owned(),
        requested_iterations,
        completed_iterations,
        starting_frontier_sequence,
        completed_sequences,
        terminal_resume_status,
    })
}
