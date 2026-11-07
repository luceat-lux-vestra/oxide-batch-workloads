use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use anyhow::{bail, Result};
use serde::Serialize;
use tokio::process::Command;

use super::progress::{
    execute_selected_sampled_recorded_iteration, initialize_campaign_if_absent,
};
use super::resume::{reconcile_resume_status, ResumeState, ResumeStatus};

const GUARDED_BATCH_SCHEMA: &str = "oxide-batch-workloads.endurance-run-guarded-batch";
const SCHEMA_VERSION: u32 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum GuardDecision {
    Allow,
    Block,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum GuardBlockKind {
    NonzeroExit,
    Signal,
    SpawnFailure,
    WaitFailure,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct GuardAttempt {
    pub sequence: u64,
    pub decision: GuardDecision,
    pub block_kind: Option<GuardBlockKind>,
    pub exit_code: Option<i32>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum GuardedBatchStopReason {
    RequestedCountCompleted,
    ResumeBlocked,
    GuardBlocked,
}

#[derive(Debug, Serialize)]
pub struct GuardedBatchResult {
    pub schema: &'static str,
    pub schema_version: u32,
    pub campaign_id: String,
    pub requested_iterations: u64,
    pub completed_iterations: u64,
    pub starting_frontier_sequence: u64,
    pub completed_sequences: Vec<u64>,
    pub guard_attempts: Vec<GuardAttempt>,
    pub stop_reason: GuardedBatchStopReason,
    pub terminal_resume_status: ResumeStatus,
}

pub async fn execute_guarded_bounded_batch(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    requested_iterations: u64,
    sample_interval: Duration,
    pre_iteration_guard: &Path,
) -> Result<GuardedBatchResult> {
    if requested_iterations == 0 {
        bail!("requested iterations must be greater than zero");
    }

    initialize_campaign_if_absent(evidence_root, campaign_id, workload_binary)?;

    let mut completed_sequences = Vec::new();
    let mut guard_attempts = Vec::new();
    let mut starting_frontier_sequence = None;
    let mut terminal_resume_status = None;
    let mut stop_reason = GuardedBatchStopReason::RequestedCountCompleted;

    for _ in 0..requested_iterations {
        let pre_execution = reconcile_resume_status(evidence_root, campaign_id, workload_binary)?;
        if starting_frontier_sequence.is_none() {
            starting_frontier_sequence = Some(pre_execution.frontier_sequence);
        }

        if pre_execution.state != ResumeState::Ready {
            terminal_resume_status = Some(pre_execution);
            stop_reason = GuardedBatchStopReason::ResumeBlocked;
            break;
        }

        let selected_sequence = pre_execution
            .next_sequence
            .ok_or_else(|| anyhow::anyhow!("ready resume status is missing next_sequence"))?;
        let selected_plan = pre_execution
            .next_plan
            .as_ref()
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

        let guard = run_guard(pre_iteration_guard, selected_sequence).await;
        let allowed = guard.decision == GuardDecision::Allow;
        guard_attempts.push(guard);
        if !allowed {
            terminal_resume_status = Some(pre_execution);
            stop_reason = GuardedBatchStopReason::GuardBlocked;
            break;
        }

        let step = execute_selected_sampled_recorded_iteration(
            workload_binary,
            database_url,
            evidence_root,
            campaign_id,
            pre_execution,
            sample_interval,
        )
        .await?;

        if step.selected_sequence != Some(selected_sequence) {
            bail!("selected execution disagrees with guarded sequence {selected_sequence}");
        }
        if step.execution.is_none() {
            bail!(
                "guarded ready step returned no execution result for sequence {selected_sequence}"
            );
        }

        completed_sequences.push(selected_sequence);
    }

    let starting_frontier_sequence = starting_frontier_sequence
        .ok_or_else(|| anyhow::anyhow!("guarded batch did not evaluate a resume frontier"))?;
    let terminal_resume_status = match terminal_resume_status {
        Some(status) => status,
        None => reconcile_resume_status(evidence_root, campaign_id, workload_binary)?,
    };
    let completed_iterations = u64::try_from(completed_sequences.len())
        .map_err(|_| anyhow::anyhow!("completed iteration count does not fit u64"))?;

    Ok(GuardedBatchResult {
        schema: GUARDED_BATCH_SCHEMA,
        schema_version: SCHEMA_VERSION,
        campaign_id: campaign_id.to_owned(),
        requested_iterations,
        completed_iterations,
        starting_frontier_sequence,
        completed_sequences,
        guard_attempts,
        stop_reason,
        terminal_resume_status,
    })
}

async fn run_guard(path: &Path, sequence: u64) -> GuardAttempt {
    let mut command = Command::new(path);
    // Keep guard diagnostics visible to the supervising raw log while the
    // structured GuardAttempt remains the durable semantic record.
    command
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .kill_on_drop(true);

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(_) => {
            return blocked_guard(sequence, GuardBlockKind::SpawnFailure, None);
        }
    };

    let status = match child.wait().await {
        Ok(status) => status,
        Err(_) => {
            return blocked_guard(sequence, GuardBlockKind::WaitFailure, None);
        }
    };

    if status.success() {
        return GuardAttempt {
            sequence,
            decision: GuardDecision::Allow,
            block_kind: None,
            exit_code: status.code(),
        };
    }

    match status.code() {
        Some(code) => blocked_guard(sequence, GuardBlockKind::NonzeroExit, Some(code)),
        None => blocked_guard(sequence, GuardBlockKind::Signal, None),
    }
}

fn blocked_guard(
    sequence: u64,
    block_kind: GuardBlockKind,
    exit_code: Option<i32>,
) -> GuardAttempt {
    GuardAttempt {
        sequence,
        decision: GuardDecision::Block,
        block_kind: Some(block_kind),
        exit_code,
    }
}
