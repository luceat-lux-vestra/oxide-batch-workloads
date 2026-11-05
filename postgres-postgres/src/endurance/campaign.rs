use std::future::Future;
use std::path::Path;
use std::time::Duration;

use anyhow::{bail, Result};
use serde::Serialize;

use super::evidence::{now_unix_nanos, EvidenceStore};
use super::guarded_batch::{
    execute_guarded_bounded_batch, GuardedBatchResult, GuardedBatchStopReason,
};
use super::resume::{reconcile_resume_status, ResumeState, ResumeStatus};

const RUN_CAMPAIGN_SCHEMA: &str = "oxide-batch-workloads.endurance-run-campaign";
const SCHEMA_VERSION: u32 = 1;

pub const CANONICAL_MINIMUM_ELAPSED: Duration = Duration::from_secs(24 * 60 * 60);
pub const CANONICAL_MINIMUM_COMPLETED_ITERATIONS: u64 = 100;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CampaignAcceptancePolicy {
    pub minimum_elapsed: Duration,
    pub minimum_completed_iterations: u64,
}

impl CampaignAcceptancePolicy {
    pub const fn canonical() -> Self {
        Self {
            minimum_elapsed: CANONICAL_MINIMUM_ELAPSED,
            minimum_completed_iterations: CANONICAL_MINIMUM_COMPLETED_ITERATIONS,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum CampaignStopReason {
    AcceptanceFloorsSatisfied,
    ResumeBlocked,
    GuardBlocked,
}

#[derive(Debug, Serialize)]
pub struct CampaignRunResult {
    pub schema: &'static str,
    pub schema_version: u32,
    pub campaign_id: String,
    pub campaign_started_at_unix_nanos_utc: u64,
    pub observed_at_unix_nanos_utc: u64,
    pub elapsed_nanos: u64,
    pub minimum_elapsed_nanos: u64,
    pub minimum_completed_iterations: u64,
    pub initial_frontier_sequence: u64,
    pub final_frontier_sequence: u64,
    pub initial_completed_iterations: u64,
    pub final_completed_iterations: u64,
    pub iterations_completed_by_invocation: u64,
    pub stop_reason: CampaignStopReason,
    pub terminal_resume_status: ResumeStatus,
}

pub async fn execute_acceptance_campaign(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    sample_interval: Duration,
    pre_iteration_guard: &Path,
) -> Result<CampaignRunResult> {
    execute_acceptance_campaign_with_policy(
        workload_binary,
        database_url,
        evidence_root,
        campaign_id,
        sample_interval,
        pre_iteration_guard,
        CampaignAcceptancePolicy::canonical(),
    )
    .await
}

/// Non-canonical policy seam for deterministic tests and bounded smoke only.
///
/// Production CLI execution intentionally calls [execute_acceptance_campaign] and therefore
/// cannot lower the #203 24-hour / 100-iteration acceptance floor.
#[doc(hidden)]
pub async fn execute_acceptance_campaign_with_policy(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    sample_interval: Duration,
    pre_iteration_guard: &Path,
    policy: CampaignAcceptancePolicy,
) -> Result<CampaignRunResult> {
    validate_policy(policy)?;

    let campaign_started_at_unix_nanos_utc = {
        let store = EvidenceStore::open(evidence_root, campaign_id, workload_binary)?;
        store.campaign().created_at_unix_nanos_utc
    };
    let initial_resume = reconcile_resume_status(evidence_root, campaign_id, workload_binary)?;

    run_acceptance_loop(
        campaign_id,
        campaign_started_at_unix_nanos_utc,
        initial_resume,
        policy,
        now_unix_nanos,
        || {
            execute_guarded_bounded_batch(
                workload_binary,
                database_url,
                evidence_root,
                campaign_id,
                1,
                sample_interval,
                pre_iteration_guard,
            )
        },
    )
    .await
}

async fn run_acceptance_loop<Now, Step, StepFuture>(
    campaign_id: &str,
    campaign_started_at_unix_nanos_utc: u64,
    initial_resume: ResumeStatus,
    policy: CampaignAcceptancePolicy,
    mut now: Now,
    mut next_step: Step,
) -> Result<CampaignRunResult>
where
    Now: FnMut() -> Result<u64>,
    Step: FnMut() -> StepFuture,
    StepFuture: Future<Output = Result<GuardedBatchResult>>,
{
    validate_policy(policy)?;

    if initial_resume.campaign_id != campaign_id {
        bail!(
            "initial resume campaign {:?} disagrees with requested campaign {:?}",
            initial_resume.campaign_id,
            campaign_id
        );
    }

    let initial_frontier_sequence = initial_resume.frontier_sequence;
    let mut current_resume = initial_resume;

    loop {
        let observed_at_unix_nanos_utc = now()?;
        let elapsed_nanos = checked_elapsed_nanos(
            campaign_started_at_unix_nanos_utc,
            observed_at_unix_nanos_utc,
        )?;

        if current_resume.state != ResumeState::Ready {
            return finish_result(
                campaign_id,
                campaign_started_at_unix_nanos_utc,
                observed_at_unix_nanos_utc,
                policy,
                initial_frontier_sequence,
                CampaignStopReason::ResumeBlocked,
                current_resume,
            );
        }

        let minimum_elapsed_nanos = policy_minimum_elapsed_nanos(policy)?;
        if elapsed_nanos >= minimum_elapsed_nanos
            && current_resume.frontier_sequence >= policy.minimum_completed_iterations
        {
            return finish_result(
                campaign_id,
                campaign_started_at_unix_nanos_utc,
                observed_at_unix_nanos_utc,
                policy,
                initial_frontier_sequence,
                CampaignStopReason::AcceptanceFloorsSatisfied,
                current_resume,
            );
        }

        let step = next_step().await?;
        if step.campaign_id != campaign_id {
            bail!(
                "guarded step campaign {:?} disagrees with requested campaign {:?}",
                step.campaign_id,
                campaign_id
            );
        }
        if step.starting_frontier_sequence != current_resume.frontier_sequence {
            bail!(
                "guarded step started from frontier {} but controller expected {}",
                step.starting_frontier_sequence,
                current_resume.frontier_sequence
            );
        }

        match step.stop_reason {
            GuardedBatchStopReason::RequestedCountCompleted => {
                if step.completed_iterations != 1 || step.completed_sequences.len() != 1 {
                    bail!(
                        "one-step guarded execution completed {} iterations with {} sequences",
                        step.completed_iterations,
                        step.completed_sequences.len()
                    );
                }

                let expected_sequence = current_resume
                    .frontier_sequence
                    .checked_add(1)
                    .ok_or_else(|| anyhow::anyhow!("durable frontier sequence overflow"))?;
                if step.completed_sequences[0] != expected_sequence {
                    bail!(
                        "guarded step completed sequence {} but controller expected {}",
                        step.completed_sequences[0],
                        expected_sequence
                    );
                }
                if step.terminal_resume_status.frontier_sequence != expected_sequence {
                    bail!(
                        "guarded terminal frontier {} did not advance exactly to {}",
                        step.terminal_resume_status.frontier_sequence,
                        expected_sequence
                    );
                }
                current_resume = step.terminal_resume_status;
            }
            GuardedBatchStopReason::ResumeBlocked => {
                let observed_at_unix_nanos_utc = now()?;
                return finish_result(
                    campaign_id,
                    campaign_started_at_unix_nanos_utc,
                    observed_at_unix_nanos_utc,
                    policy,
                    initial_frontier_sequence,
                    CampaignStopReason::ResumeBlocked,
                    step.terminal_resume_status,
                );
            }
            GuardedBatchStopReason::GuardBlocked => {
                let observed_at_unix_nanos_utc = now()?;
                return finish_result(
                    campaign_id,
                    campaign_started_at_unix_nanos_utc,
                    observed_at_unix_nanos_utc,
                    policy,
                    initial_frontier_sequence,
                    CampaignStopReason::GuardBlocked,
                    step.terminal_resume_status,
                );
            }
        }
    }
}

fn validate_policy(policy: CampaignAcceptancePolicy) -> Result<()> {
    if policy.minimum_completed_iterations == 0 {
        bail!("minimum completed iterations must be greater than zero");
    }
    let _ = policy_minimum_elapsed_nanos(policy)?;
    Ok(())
}

fn policy_minimum_elapsed_nanos(policy: CampaignAcceptancePolicy) -> Result<u64> {
    u64::try_from(policy.minimum_elapsed.as_nanos())
        .map_err(|_| anyhow::anyhow!("minimum elapsed duration does not fit u64 nanoseconds"))
}

fn checked_elapsed_nanos(started_at: u64, observed_at: u64) -> Result<u64> {
    observed_at.checked_sub(started_at).ok_or_else(|| {
        anyhow::anyhow!(
            "observed campaign clock {observed_at} precedes durable campaign start {started_at}"
        )
    })
}

fn finish_result(
    campaign_id: &str,
    campaign_started_at_unix_nanos_utc: u64,
    observed_at_unix_nanos_utc: u64,
    policy: CampaignAcceptancePolicy,
    initial_frontier_sequence: u64,
    stop_reason: CampaignStopReason,
    terminal_resume_status: ResumeStatus,
) -> Result<CampaignRunResult> {
    let elapsed_nanos = checked_elapsed_nanos(
        campaign_started_at_unix_nanos_utc,
        observed_at_unix_nanos_utc,
    )?;
    let iterations_completed_by_invocation = terminal_resume_status
        .frontier_sequence
        .checked_sub(initial_frontier_sequence)
        .ok_or_else(|| anyhow::anyhow!("durable frontier moved backwards during campaign run"))?;

    Ok(CampaignRunResult {
        schema: RUN_CAMPAIGN_SCHEMA,
        schema_version: SCHEMA_VERSION,
        campaign_id: campaign_id.to_owned(),
        campaign_started_at_unix_nanos_utc,
        observed_at_unix_nanos_utc,
        elapsed_nanos,
        minimum_elapsed_nanos: policy_minimum_elapsed_nanos(policy)?,
        minimum_completed_iterations: policy.minimum_completed_iterations,
        initial_frontier_sequence,
        final_frontier_sequence: terminal_resume_status.frontier_sequence,
        initial_completed_iterations: initial_frontier_sequence,
        final_completed_iterations: terminal_resume_status.frontier_sequence,
        iterations_completed_by_invocation,
        stop_reason,
        terminal_resume_status,
    })
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::future::ready;
    use std::time::Duration;

    use anyhow::{anyhow, Result};

    use super::{run_acceptance_loop, CampaignAcceptancePolicy, CampaignStopReason};
    use crate::endurance::guarded_batch::{GuardedBatchResult, GuardedBatchStopReason};
    use crate::endurance::plan_iteration;
    use crate::endurance::resume::{ResumeState, ResumeStatus};

    const CAMPAIGN_ID: &str = "controlunit";

    fn ready_status(frontier: u64) -> ResumeStatus {
        let next_sequence = frontier + 1;
        ResumeStatus {
            schema: "test.resume",
            schema_version: 1,
            campaign_id: CAMPAIGN_ID.to_owned(),
            state: ResumeState::Ready,
            frontier_sequence: frontier,
            next_sequence: Some(next_sequence),
            next_plan: Some(
                plan_iteration(CAMPAIGN_ID, next_sequence).expect("build unit-test next plan"),
            ),
            blocked_sequence: None,
            reason: None,
        }
    }

    fn blocked_status(frontier: u64) -> ResumeStatus {
        ResumeStatus {
            schema: "test.resume",
            schema_version: 1,
            campaign_id: CAMPAIGN_ID.to_owned(),
            state: ResumeState::BlockedFailure,
            frontier_sequence: frontier,
            next_sequence: None,
            next_plan: None,
            blocked_sequence: Some(frontier + 1),
            reason: Some("test block".to_owned()),
        }
    }

    fn successful_step(starting_frontier: u64) -> GuardedBatchResult {
        let completed = starting_frontier + 1;
        GuardedBatchResult {
            schema: "test.guarded",
            schema_version: 1,
            campaign_id: CAMPAIGN_ID.to_owned(),
            requested_iterations: 1,
            completed_iterations: 1,
            starting_frontier_sequence: starting_frontier,
            completed_sequences: vec![completed],
            guard_attempts: Vec::new(),
            stop_reason: GuardedBatchStopReason::RequestedCountCompleted,
            terminal_resume_status: ready_status(completed),
        }
    }

    fn blocked_step(starting_frontier: u64, reason: GuardedBatchStopReason) -> GuardedBatchResult {
        GuardedBatchResult {
            schema: "test.guarded",
            schema_version: 1,
            campaign_id: CAMPAIGN_ID.to_owned(),
            requested_iterations: 1,
            completed_iterations: 0,
            starting_frontier_sequence: starting_frontier,
            completed_sequences: Vec::new(),
            guard_attempts: Vec::new(),
            stop_reason: reason,
            terminal_resume_status: ready_status(starting_frontier),
        }
    }

    fn clock(values: impl IntoIterator<Item = u64>) -> impl FnMut() -> Result<u64> {
        let mut values = values.into_iter().collect::<VecDeque<_>>();
        move || {
            values
                .pop_front()
                .ok_or_else(|| anyhow!("fake clock exhausted"))
        }
    }

    #[tokio::test]
    async fn both_floors_must_be_satisfied_before_success() {
        let policy = CampaignAcceptancePolicy {
            minimum_elapsed: Duration::from_nanos(10),
            minimum_completed_iterations: 100,
        };

        let already_satisfied = run_acceptance_loop(
            CAMPAIGN_ID,
            0,
            ready_status(100),
            policy,
            clock([10]),
            || ready(Err(anyhow!("step must not run"))),
        )
        .await
        .expect("both satisfied should stop immediately");
        assert_eq!(
            already_satisfied.stop_reason,
            CampaignStopReason::AcceptanceFloorsSatisfied
        );
        assert_eq!(already_satisfied.iterations_completed_by_invocation, 0);

        let mut steps = VecDeque::from([successful_step(100)]);
        let iteration_floor_first = run_acceptance_loop(
            CAMPAIGN_ID,
            0,
            ready_status(100),
            policy,
            clock([5, 10]),
            || {
                ready(
                    steps
                        .pop_front()
                        .ok_or_else(|| anyhow!("missing scripted step")),
                )
            },
        )
        .await
        .expect("iteration floor first should keep running");
        assert_eq!(iteration_floor_first.final_frontier_sequence, 101);
        assert_eq!(iteration_floor_first.iterations_completed_by_invocation, 1);

        let mut steps = VecDeque::from([successful_step(99)]);
        let time_floor_first = run_acceptance_loop(
            CAMPAIGN_ID,
            0,
            ready_status(99),
            policy,
            clock([10, 10]),
            || {
                ready(
                    steps
                        .pop_front()
                        .ok_or_else(|| anyhow!("missing scripted step")),
                )
            },
        )
        .await
        .expect("time floor first should keep running");
        assert_eq!(time_floor_first.final_frontier_sequence, 100);
        assert_eq!(time_floor_first.iterations_completed_by_invocation, 1);
    }

    #[tokio::test]
    async fn neither_floor_advances_until_both_are_satisfied() {
        let policy = CampaignAcceptancePolicy {
            minimum_elapsed: Duration::from_nanos(10),
            minimum_completed_iterations: 100,
        };
        let mut steps = VecDeque::from([successful_step(98), successful_step(99)]);

        let result = match run_acceptance_loop(
            CAMPAIGN_ID,
            0,
            ready_status(98),
            policy,
            clock([0, 5, 10]),
            || {
                ready(
                    steps
                        .pop_front()
                        .ok_or_else(|| anyhow!("missing scripted step")),
                )
            },
        )
        .await
        {
            Ok(result) => result,
            Err(error) => panic!("controller should reach both floors: {error:#}"),
        };

        assert_eq!(
            result.stop_reason,
            CampaignStopReason::AcceptanceFloorsSatisfied
        );
        assert_eq!(result.initial_frontier_sequence, 98);
        assert_eq!(result.final_frontier_sequence, 100);
        assert_eq!(result.iterations_completed_by_invocation, 2);
    }

    #[tokio::test]
    async fn resume_and_guard_blocks_stop_without_later_steps() {
        let policy = CampaignAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 1,
        };

        let blocked = match run_acceptance_loop(
            CAMPAIGN_ID,
            0,
            blocked_status(0),
            policy,
            clock([0]),
            || ready(Err(anyhow!("resume block must not execute a step"))),
        )
        .await
        {
            Ok(result) => result,
            Err(error) => panic!("resume block should be structured: {error:#}"),
        };
        assert_eq!(blocked.stop_reason, CampaignStopReason::ResumeBlocked);

        let mut steps = VecDeque::from([blocked_step(0, GuardedBatchStopReason::GuardBlocked)]);
        let guard_blocked = match run_acceptance_loop(
            CAMPAIGN_ID,
            0,
            ready_status(0),
            policy,
            clock([0, 0]),
            || {
                ready(
                    steps
                        .pop_front()
                        .ok_or_else(|| anyhow!("missing scripted guard block")),
                )
            },
        )
        .await
        {
            Ok(result) => result,
            Err(error) => panic!("guard block should be structured: {error:#}"),
        };
        assert_eq!(guard_blocked.stop_reason, CampaignStopReason::GuardBlocked);
        assert_eq!(guard_blocked.final_frontier_sequence, 0);
    }

    #[tokio::test]
    async fn selected_iteration_error_propagates_without_retry() {
        let policy = CampaignAcceptancePolicy {
            minimum_elapsed: Duration::ZERO,
            minimum_completed_iterations: 1,
        };
        let mut calls = 0_u64;

        let error =
            run_acceptance_loop(CAMPAIGN_ID, 0, ready_status(0), policy, clock([0]), || {
                calls += 1;
                ready(Err(anyhow!("selected iteration failed")))
            })
            .await
            .expect_err("selected iteration failure must propagate");

        assert!(error.to_string().contains("selected iteration failed"));
        assert_eq!(calls, 1);
    }
}
