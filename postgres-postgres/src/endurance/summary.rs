use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde::Serialize;
use serde_json::Value;
use sqlx::postgres::PgPoolOptions;

use super::campaign::{CANONICAL_MINIMUM_COMPLETED_ITERATIONS, CANONICAL_MINIMUM_ELAPSED};
use super::evidence::{
    atomic_write_json, load_existing_campaign_readonly, now_unix_nanos, CampaignRecord,
};
use super::resume::{reconcile_resume_status, ResumeState, ResumeStatus};
use super::{plan_iteration, EnduranceFailure};

const SUMMARY_SCHEMA: &str = "oxide-batch-workloads.endurance-summary";
const SCHEMA_VERSION: u32 = 1;
const CAMPAIGN_SCHEMA: &str = "oxide-batch-workloads.endurance-campaign";
const EVENT_SCHEMA: &str = "oxide-batch-workloads.endurance-event";
const ITERATION_SCHEMA: &str = "oxide-batch-workloads.endurance-iteration";
const FAILURE_SCHEMA: &str = "oxide-batch-workloads.endurance-failure";
const SAMPLE_SCHEMA: &str = "oxide-batch-workloads.endurance-sample";
const CANONICAL_MINIMUM_BEFORE_COMMIT_CRASHES: u64 = 2;
const CANONICAL_MINIMUM_AFTER_COMMIT_CRASHES: u64 = 2;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SummaryAcceptancePolicy {
    pub minimum_elapsed: Duration,
    pub minimum_completed_iterations: u64,
    pub minimum_before_commit_crashes: u64,
    pub minimum_after_commit_crashes: u64,
}

impl SummaryAcceptancePolicy {
    pub const fn canonical() -> Self {
        Self {
            minimum_elapsed: CANONICAL_MINIMUM_ELAPSED,
            minimum_completed_iterations: CANONICAL_MINIMUM_COMPLETED_ITERATIONS,
            minimum_before_commit_crashes: CANONICAL_MINIMUM_BEFORE_COMMIT_CRASHES,
            minimum_after_commit_crashes: CANONICAL_MINIMUM_AFTER_COMMIT_CRASHES,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SummaryVerdict {
    Accepted,
    Rejected,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SummaryReasonCode {
    ElapsedBelowMinimum,
    CompletedIterationsBelowMinimum,
    BeforeCommitCrashCountBelowMinimum,
    AfterCommitCrashCountBelowMinimum,
    EvidenceSemanticMismatch,
    UnexpectedTerminalFailureEvidence,
    DurableResumeNotReady,
    MissingDatabaseExecutionHistory,
    RecoveryRequiredExecutionsRemain,
    NoResourceSamples,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct ReaderCounts {
    pub cursor: u64,
    pub paging: u64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct ExecutionCounts {
    pub clean: u64,
    pub before_commit_crash: u64,
    pub after_commit_crash: u64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct LifecycleCounts {
    pub terminal_success: u64,
    pub terminal_failure: u64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct FailureCounts {
    pub total: u64,
    pub harness_environment: u64,
    pub workload_child: u64,
    pub verification: u64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct I64Trend {
    pub first: Option<i64>,
    pub last: Option<i64>,
    pub minimum: Option<i64>,
    pub maximum: Option<i64>,
    pub delta: Option<i64>,
}

impl I64Trend {
    fn observe(&mut self, value: i64) {
        if self.first.is_none() {
            self.first = Some(value);
        }
        self.last = Some(value);
        self.minimum = Some(self.minimum.map_or(value, |current| current.min(value)));
        self.maximum = Some(self.maximum.map_or(value, |current| current.max(value)));
        self.delta = self
            .first
            .zip(self.last)
            .map(|(first, last)| last.saturating_sub(first));
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct U64Trend {
    pub first: Option<u64>,
    pub last: Option<u64>,
    pub minimum: Option<u64>,
    pub maximum: Option<u64>,
    pub delta: Option<i128>,
}

impl U64Trend {
    fn observe(&mut self, value: u64) {
        if self.first.is_none() {
            self.first = Some(value);
        }
        self.last = Some(value);
        self.minimum = Some(self.minimum.map_or(value, |current| current.min(value)));
        self.maximum = Some(self.maximum.map_or(value, |current| current.max(value)));
        self.delta = self
            .first
            .zip(self.last)
            .map(|(first, last)| i128::from(last) - i128::from(first));
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct MetadataResourceSummary {
    pub job_instances: I64Trend,
    pub job_executions: I64Trend,
    pub step_executions: I64Trend,
    pub job_instance_table_bytes: I64Trend,
    pub job_execution_table_bytes: I64Trend,
    pub step_execution_table_bytes: I64Trend,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct ResourceSummary {
    pub sample_count: u64,
    pub first_timestamp_unix_nanos_utc: Option<u64>,
    pub last_timestamp_unix_nanos_utc: Option<u64>,
    pub process_observations: u64,
    pub process_unavailable_observations: u64,
    pub peak_rss_bytes_by_role: BTreeMap<String, u64>,
    pub postgres_total_connections: I64Trend,
    pub postgres_active_connections: I64Trend,
    pub database_size_bytes: I64Trend,
    pub oxide_batch: MetadataResourceSummary,
    pub filesystem_available_bytes: U64Trend,
}

#[derive(Clone, Debug, Serialize)]
pub struct RecoveryRequiredExecution {
    pub execution_id: String,
    pub import_name: String,
    pub status: String,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct DatabaseReconciliation {
    pub logical_identities_checked: u64,
    pub execution_rows_observed: u64,
    pub missing_logical_identities: Vec<String>,
    pub recovery_required_executions: Vec<RecoveryRequiredExecution>,
}

#[derive(Clone, Debug, Serialize)]
pub struct CampaignSummary {
    pub schema: &'static str,
    pub schema_version: u32,
    pub summary_path: String,
    pub campaign: CampaignRecord,
    pub observed_at_unix_nanos_utc: u64,
    pub elapsed_nanos: u64,
    pub minimum_elapsed_nanos: u64,
    pub minimum_completed_iterations: u64,
    pub minimum_before_commit_crashes: u64,
    pub minimum_after_commit_crashes: u64,
    pub final_frontier_sequence: u64,
    pub completed_logical_iterations: u64,
    pub readers: ReaderCounts,
    pub executions: ExecutionCounts,
    pub lifecycle: LifecycleCounts,
    pub failures: FailureCounts,
    pub resources: ResourceSummary,
    pub final_resume_status: ResumeStatus,
    pub evidence_semantic_violations: Vec<String>,
    pub database: DatabaseReconciliation,
    pub reason_codes: Vec<SummaryReasonCode>,
    pub verdict: SummaryVerdict,
}

struct EvidenceSnapshot {
    campaign: CampaignRecord,
    resume: ResumeStatus,
    readers: ReaderCounts,
    executions: ExecutionCounts,
    lifecycle: LifecycleCounts,
    failures: FailureCounts,
    resources: ResourceSummary,
    semantic_violations: Vec<String>,
}

pub async fn summarize_campaign(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
) -> Result<CampaignSummary> {
    let observed_at_unix_nanos_utc = now_unix_nanos()?;
    let snapshot = collect_evidence(workload_binary, evidence_root, campaign_id)?;
    let database =
        reconcile_database(database_url, campaign_id, snapshot.resume.frontier_sequence).await?;
    finalize_summary(
        evidence_root,
        snapshot,
        SummaryAcceptancePolicy::canonical(),
        observed_at_unix_nanos_utc,
        database,
    )
}

/// Deterministic test seam for summary policy and database-reconciliation outcomes.
///
/// Production CLI execution intentionally calls [summarize_campaign], which always uses
/// the canonical 24-hour / 100-iteration / 2+2 crash acceptance policy and a real
/// read-only PostgreSQL reconciliation.
#[doc(hidden)]
pub fn summarize_campaign_with_reconciliation_for_test(
    workload_binary: &Path,
    evidence_root: &Path,
    campaign_id: &str,
    policy: SummaryAcceptancePolicy,
    observed_at_unix_nanos_utc: u64,
    database: DatabaseReconciliation,
) -> Result<CampaignSummary> {
    let snapshot = collect_evidence(workload_binary, evidence_root, campaign_id)?;
    finalize_summary(
        evidence_root,
        snapshot,
        policy,
        observed_at_unix_nanos_utc,
        database,
    )
}

fn collect_evidence(
    workload_binary: &Path,
    evidence_root: &Path,
    campaign_id: &str,
) -> Result<EvidenceSnapshot> {
    let campaign = load_existing_campaign_readonly(evidence_root, campaign_id, workload_binary)?;
    if campaign.schema != CAMPAIGN_SCHEMA || campaign.schema_version != SCHEMA_VERSION {
        bail!(
            "campaign metadata schema/version is not supported by summary finalization: {}/{}",
            campaign.schema,
            campaign.schema_version
        );
    }

    let resume = reconcile_resume_status(evidence_root, campaign_id, workload_binary)?;
    let campaign_dir = evidence_root.join(campaign_id);
    let lifecycle = scan_events(&campaign_dir.join("events.jsonl"), campaign_id)?;
    let failures = scan_failures(&campaign_dir.join("failures"), campaign_id)?;
    let (readers, executions, mut semantic_violations) = scan_iterations(
        &campaign_dir.join("iterations"),
        campaign_id,
        resume.frontier_sequence,
    )?;
    let resources = scan_samples(&campaign_dir.join("samples.jsonl"), campaign_id)?;

    if lifecycle.terminal_success != resume.frontier_sequence {
        semantic_violations.push(format!(
            "terminal-success event count {} does not equal durable frontier {}",
            lifecycle.terminal_success, resume.frontier_sequence
        ));
    }
    if lifecycle.terminal_failure != failures.total {
        semantic_violations.push(format!(
            "terminal-failure event count {} does not equal failure artifact count {}",
            lifecycle.terminal_failure, failures.total
        ));
    }

    Ok(EvidenceSnapshot {
        campaign,
        resume,
        readers,
        executions,
        lifecycle,
        failures,
        resources,
        semantic_violations,
    })
}

fn finalize_summary(
    evidence_root: &Path,
    snapshot: EvidenceSnapshot,
    policy: SummaryAcceptancePolicy,
    observed_at_unix_nanos_utc: u64,
    database: DatabaseReconciliation,
) -> Result<CampaignSummary> {
    let minimum_elapsed_nanos = duration_nanos(policy.minimum_elapsed)?;
    let elapsed_nanos = observed_at_unix_nanos_utc
        .checked_sub(snapshot.campaign.created_at_unix_nanos_utc)
        .ok_or_else(|| {
            anyhow::anyhow!(
                "summary observation clock {} precedes durable campaign start {}",
                observed_at_unix_nanos_utc,
                snapshot.campaign.created_at_unix_nanos_utc
            )
        })?;

    let mut reason_codes = Vec::new();
    if elapsed_nanos < minimum_elapsed_nanos {
        push_reason(&mut reason_codes, SummaryReasonCode::ElapsedBelowMinimum);
    }
    if snapshot.resume.frontier_sequence < policy.minimum_completed_iterations {
        push_reason(
            &mut reason_codes,
            SummaryReasonCode::CompletedIterationsBelowMinimum,
        );
    }
    if snapshot.executions.before_commit_crash < policy.minimum_before_commit_crashes {
        push_reason(
            &mut reason_codes,
            SummaryReasonCode::BeforeCommitCrashCountBelowMinimum,
        );
    }
    if snapshot.executions.after_commit_crash < policy.minimum_after_commit_crashes {
        push_reason(
            &mut reason_codes,
            SummaryReasonCode::AfterCommitCrashCountBelowMinimum,
        );
    }
    if !snapshot.semantic_violations.is_empty() {
        push_reason(
            &mut reason_codes,
            SummaryReasonCode::EvidenceSemanticMismatch,
        );
    }
    if snapshot.lifecycle.terminal_failure > 0 || snapshot.failures.total > 0 {
        push_reason(
            &mut reason_codes,
            SummaryReasonCode::UnexpectedTerminalFailureEvidence,
        );
    }
    if snapshot.resume.state != ResumeState::Ready {
        push_reason(&mut reason_codes, SummaryReasonCode::DurableResumeNotReady);
    }
    if !database.missing_logical_identities.is_empty() {
        push_reason(
            &mut reason_codes,
            SummaryReasonCode::MissingDatabaseExecutionHistory,
        );
    }
    if !database.recovery_required_executions.is_empty() {
        push_reason(
            &mut reason_codes,
            SummaryReasonCode::RecoveryRequiredExecutionsRemain,
        );
    }
    if snapshot.resources.sample_count == 0 {
        push_reason(&mut reason_codes, SummaryReasonCode::NoResourceSamples);
    }

    let verdict = if reason_codes.is_empty() {
        SummaryVerdict::Accepted
    } else {
        SummaryVerdict::Rejected
    };
    let summary_path = evidence_root
        .join(&snapshot.campaign.campaign_id)
        .join("summary.json");

    let summary = CampaignSummary {
        schema: SUMMARY_SCHEMA,
        schema_version: SCHEMA_VERSION,
        summary_path: summary_path.display().to_string(),
        observed_at_unix_nanos_utc,
        elapsed_nanos,
        minimum_elapsed_nanos,
        minimum_completed_iterations: policy.minimum_completed_iterations,
        minimum_before_commit_crashes: policy.minimum_before_commit_crashes,
        minimum_after_commit_crashes: policy.minimum_after_commit_crashes,
        final_frontier_sequence: snapshot.resume.frontier_sequence,
        completed_logical_iterations: snapshot.resume.frontier_sequence,
        campaign: snapshot.campaign,
        readers: snapshot.readers,
        executions: snapshot.executions,
        lifecycle: snapshot.lifecycle,
        failures: snapshot.failures,
        resources: snapshot.resources,
        final_resume_status: snapshot.resume,
        evidence_semantic_violations: snapshot.semantic_violations,
        database,
        reason_codes,
        verdict,
    };

    atomic_write_json(&summary_path, &summary)?;
    Ok(summary)
}

fn push_reason(reason_codes: &mut Vec<SummaryReasonCode>, reason: SummaryReasonCode) {
    if !reason_codes.contains(&reason) {
        reason_codes.push(reason);
    }
}

fn duration_nanos(duration: Duration) -> Result<u64> {
    u64::try_from(duration.as_nanos()).map_err(|_| {
        anyhow::anyhow!("summary minimum elapsed duration does not fit u64 nanoseconds")
    })
}

fn scan_events(path: &Path, campaign_id: &str) -> Result<LifecycleCounts> {
    let file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    let mut counts = LifecycleCounts::default();
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line_number = index + 1;
        let line = line.with_context(|| format!("read {} line {line_number}", path.display()))?;
        if line.trim().is_empty() {
            bail!(
                "{} line {line_number} is unexpectedly blank",
                path.display()
            );
        }
        let value: Value = serde_json::from_str(&line)
            .with_context(|| format!("parse {} line {line_number}", path.display()))?;
        let source = format!("{} line {line_number}", path.display());
        validate_common(&value, EVENT_SCHEMA, campaign_id, &source)?;
        let _ = positive_sequence(&value, &source)?;
        match string_at(&value, "/phase", &source)? {
            "iteration-terminal-success" => counts.terminal_success += 1,
            "iteration-terminal-failure" => counts.terminal_failure += 1,
            _ => {}
        }
    }
    Ok(counts)
}

fn scan_failures(dir: &Path, campaign_id: &str) -> Result<FailureCounts> {
    let mut paths = directory_files(dir)?;
    paths.sort();
    let mut counts = FailureCounts::default();

    for path in paths {
        let sequence = failure_file_sequence(&path)?;
        let value: Value = serde_json::from_reader(
            File::open(&path).with_context(|| format!("open {}", path.display()))?,
        )
        .with_context(|| format!("parse {}", path.display()))?;
        let source = path.display().to_string();
        validate_common(&value, FAILURE_SCHEMA, campaign_id, &source)?;
        if positive_sequence(&value, &source)? != sequence {
            bail!(
                "failure record sequence disagrees with canonical filename: {}",
                path.display()
            );
        }
        if string_at(&value, "/terminal", &source)? != "failure" {
            bail!(
                "failure artifact is not terminal=failure: {}",
                path.display()
            );
        }

        counts.total += 1;
        match string_at(&value, "/failure_class", &source)? {
            "harness-environment" => counts.harness_environment += 1,
            "workload-child" => counts.workload_child += 1,
            "verification" => counts.verification += 1,
            other => bail!(
                "unknown durable failure class {other:?}: {}",
                path.display()
            ),
        }
    }
    Ok(counts)
}

fn scan_iterations(
    dir: &Path,
    campaign_id: &str,
    frontier: u64,
) -> Result<(ReaderCounts, ExecutionCounts, Vec<String>)> {
    let mut readers = ReaderCounts::default();
    let mut executions = ExecutionCounts::default();
    let mut violations = Vec::new();

    for sequence in 1..=frontier {
        let path = dir.join(format!("{sequence:06}.json"));
        let value: Value = serde_json::from_reader(
            File::open(&path).with_context(|| format!("open {}", path.display()))?,
        )
        .with_context(|| format!("parse {}", path.display()))?;
        let source = path.display().to_string();
        validate_common(&value, ITERATION_SCHEMA, campaign_id, &source)?;
        if positive_sequence(&value, &source)? != sequence {
            bail!(
                "iteration record sequence disagrees with canonical filename: {}",
                path.display()
            );
        }
        if string_at(&value, "/terminal", &source)? != "success" {
            bail!(
                "iteration singleton is not terminal=success: {}",
                path.display()
            );
        }

        let plan = plan_iteration(campaign_id, sequence)?;
        let result_sequence = u64_at(&value, "/execution/result/sequence", &source)?;
        if result_sequence != sequence {
            violations.push(format!(
                "sequence {sequence}: execution result sequence is {result_sequence}"
            ));
        }

        let reader = string_at(&value, "/execution/result/reader", &source)?;
        match reader {
            "cursor" => readers.cursor += 1,
            "paging" => readers.paging += 1,
            other => violations.push(format!("sequence {sequence}: unsupported reader {other:?}")),
        }
        if reader != plan.reader.as_str() {
            violations.push(format!(
                "sequence {sequence}: reader {reader:?} disagrees with deterministic plan {:?}",
                plan.reader.as_str()
            ));
        }

        let import_name = string_at(&value, "/execution/result/import_name", &source)?;
        if import_name != plan.import_name {
            violations.push(format!(
                "sequence {sequence}: import_name {import_name:?} disagrees with {:?}",
                plan.import_name
            ));
        }

        let kind = string_at(&value, "/execution/kind", &source)?;
        match kind {
            "clean" => {
                executions.clean += 1;
                if plan.failure != EnduranceFailure::Clean {
                    violations.push(format!(
                        "sequence {sequence}: clean execution occupies scheduled {} slot",
                        failure_name(plan.failure)
                    ));
                }
                validate_child_success(
                    &value,
                    "/execution/result/run/success",
                    true,
                    sequence,
                    "workload run",
                    &source,
                    &mut violations,
                )?;
                validate_child_success(
                    &value,
                    "/execution/result/verification/success",
                    true,
                    sequence,
                    "verification",
                    &source,
                    &mut violations,
                )?;
                let _ = u64_at(&value, "/execution/result/cleanup_deleted_rows", &source)?;
            }
            "crash" => {
                let actual_failure = string_at(&value, "/execution/result/failure", &source)?;
                let expected_failure = failure_name(plan.failure);
                if actual_failure != expected_failure {
                    violations.push(format!(
                        "sequence {sequence}: crash failure {actual_failure:?} disagrees with deterministic plan {expected_failure:?}"
                    ));
                }
                match actual_failure {
                    "before-commit" if plan.failure == EnduranceFailure::BeforeCommit => {
                        executions.before_commit_crash += 1;
                    }
                    "after-commit" if plan.failure == EnduranceFailure::AfterCommit => {
                        executions.after_commit_crash += 1;
                    }
                    _ => {}
                }
                if plan.failure == EnduranceFailure::Clean {
                    violations.push(format!(
                        "sequence {sequence}: crash execution occupies a clean schedule slot"
                    ));
                }

                validate_child_success(
                    &value,
                    "/execution/result/crash/success",
                    false,
                    sequence,
                    "SIGKILL target",
                    &source,
                    &mut violations,
                )?;
                if !bool_at(&value, "/execution/result/crash/reaped", &source)? {
                    violations.push(format!(
                        "sequence {sequence}: crashed child was not recorded as reaped"
                    ));
                }
                if i64_at(&value, "/execution/result/crash/signal", &source)? != 9 {
                    violations.push(format!(
                        "sequence {sequence}: planned hard crash is not recorded as SIGKILL(9)"
                    ));
                }
                validate_child_success(
                    &value,
                    "/execution/result/plain_relaunch/success",
                    false,
                    sequence,
                    "plain relaunch",
                    &source,
                    &mut violations,
                )?;
                validate_child_success(
                    &value,
                    "/execution/result/recovery/success",
                    true,
                    sequence,
                    "recovery",
                    &source,
                    &mut violations,
                )?;
                validate_child_success(
                    &value,
                    "/execution/result/continuation/success",
                    true,
                    sequence,
                    "continuation",
                    &source,
                    &mut violations,
                )?;
                validate_child_success(
                    &value,
                    "/execution/result/verification/success",
                    true,
                    sequence,
                    "verification",
                    &source,
                    &mut violations,
                )?;
                let _ = u64_at(&value, "/execution/result/cleanup_deleted_rows", &source)?;

                let post_crash_status = string_at(
                    &value,
                    "/execution/result/post_crash/execution_status",
                    &source,
                )?;
                if !is_recovery_required_status(post_crash_status) {
                    violations.push(format!(
                        "sequence {sequence}: post-crash status {post_crash_status:?} is not recovery-required"
                    ));
                }
            }
            other => violations.push(format!(
                "sequence {sequence}: unsupported recorded execution kind {other:?}"
            )),
        }
    }

    Ok((readers, executions, violations))
}

fn validate_child_success(
    value: &Value,
    pointer: &str,
    expected: bool,
    sequence: u64,
    label: &str,
    source: &str,
    violations: &mut Vec<String>,
) -> Result<()> {
    let actual = bool_at(value, pointer, source)?;
    if actual != expected {
        violations.push(format!(
            "sequence {sequence}: {label} success={actual} expected {expected}"
        ));
    }
    Ok(())
}

fn scan_samples(path: &Path, campaign_id: &str) -> Result<ResourceSummary> {
    if !path.exists() {
        return Ok(ResourceSummary::default());
    }

    let file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    let mut summary = ResourceSummary::default();
    let mut previous_timestamp = None;

    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line_number = index + 1;
        let line = line.with_context(|| format!("read {} line {line_number}", path.display()))?;
        if line.trim().is_empty() {
            bail!(
                "{} line {line_number} is unexpectedly blank",
                path.display()
            );
        }
        let value: Value = serde_json::from_str(&line)
            .with_context(|| format!("parse {} line {line_number}", path.display()))?;
        let source = format!("{} line {line_number}", path.display());
        validate_common(&value, SAMPLE_SCHEMA, campaign_id, &source)?;
        let _ = positive_sequence(&value, &source)?;
        let timestamp = u64_at(&value, "/timestamp_unix_nanos_utc", &source)?;
        if previous_timestamp.is_some_and(|previous| timestamp < previous) {
            bail!(
                "{} line {line_number} timestamp moved backwards",
                path.display()
            );
        }
        previous_timestamp = Some(timestamp);

        summary.sample_count += 1;
        if summary.first_timestamp_unix_nanos_utc.is_none() {
            summary.first_timestamp_unix_nanos_utc = Some(timestamp);
        }
        summary.last_timestamp_unix_nanos_utc = Some(timestamp);

        if let Some(process) = value.get("process").filter(|process| !process.is_null()) {
            summary.process_observations += 1;
            let role = string_at(process, "/role", &source)?;
            let rss = process.get("rss_bytes").and_then(Value::as_u64);
            let unavailable = process
                .get("unavailable_reason")
                .is_some_and(|reason| !reason.is_null())
                || rss.is_none();
            if unavailable {
                summary.process_unavailable_observations += 1;
            }
            if let Some(rss) = rss {
                let peak = summary
                    .peak_rss_bytes_by_role
                    .entry(role.to_owned())
                    .or_insert(rss);
                *peak = (*peak).max(rss);
            }
        }

        summary.postgres_total_connections.observe(i64_at(
            &value,
            "/postgres/total_connections",
            &source,
        )?);
        summary.postgres_active_connections.observe(i64_at(
            &value,
            "/postgres/active_connections",
            &source,
        )?);
        summary.database_size_bytes.observe(i64_at(
            &value,
            "/postgres/database_size_bytes",
            &source,
        )?);

        summary.oxide_batch.job_instances.observe(i64_at(
            &value,
            "/oxide_batch/job_instances",
            &source,
        )?);
        summary.oxide_batch.job_executions.observe(i64_at(
            &value,
            "/oxide_batch/job_executions",
            &source,
        )?);
        summary.oxide_batch.step_executions.observe(i64_at(
            &value,
            "/oxide_batch/step_executions",
            &source,
        )?);
        summary.oxide_batch.job_instance_table_bytes.observe(i64_at(
            &value,
            "/oxide_batch/job_instance_table_bytes",
            &source,
        )?);
        summary
            .oxide_batch
            .job_execution_table_bytes
            .observe(i64_at(
                &value,
                "/oxide_batch/job_execution_table_bytes",
                &source,
            )?);
        summary
            .oxide_batch
            .step_execution_table_bytes
            .observe(i64_at(
                &value,
                "/oxide_batch/step_execution_table_bytes",
                &source,
            )?);

        summary.filesystem_available_bytes.observe(u64_at(
            &value,
            "/filesystem/available_bytes",
            &source,
        )?);
    }

    Ok(summary)
}

async fn reconcile_database(
    database_url: &str,
    campaign_id: &str,
    frontier: u64,
) -> Result<DatabaseReconciliation> {
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(database_url)
        .await
        .context("connect PostgreSQL for final campaign reconciliation")?;

    let mut result = DatabaseReconciliation {
        logical_identities_checked: frontier,
        ..DatabaseReconciliation::default()
    };

    for sequence in 1..=frontier {
        let plan = plan_iteration(campaign_id, sequence)?;
        let rows = sqlx::query_as::<_, (String, String)>(
            "SELECT e.id::text, e.status \
             FROM oxide_batch.ob_job_execution e \
             JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
             WHERE i.job_name = $1 \
             ORDER BY e.attempt",
        )
        .bind(&plan.import_name)
        .fetch_all(&pool)
        .await
        .with_context(|| {
            format!(
                "read OxideBatch execution history for campaign import {:?}",
                plan.import_name
            )
        })?;

        if rows.is_empty() {
            result
                .missing_logical_identities
                .push(plan.import_name.clone());
        }
        result.execution_rows_observed +=
            u64::try_from(rows.len()).context("execution row count does not fit u64")?;

        for (execution_id, status) in rows {
            if is_recovery_required_status(&status) {
                result
                    .recovery_required_executions
                    .push(RecoveryRequiredExecution {
                        execution_id,
                        import_name: plan.import_name.clone(),
                        status,
                    });
            }
        }
    }

    pool.close().await;
    Ok(result)
}

fn is_recovery_required_status(status: &str) -> bool {
    matches!(
        status.to_ascii_uppercase().as_str(),
        "STARTING" | "STARTED" | "STOPPING" | "UNKNOWN"
    )
}

fn failure_name(failure: EnduranceFailure) -> &'static str {
    match failure {
        EnduranceFailure::Clean => "clean",
        EnduranceFailure::BeforeCommit => "before-commit",
        EnduranceFailure::AfterCommit => "after-commit",
    }
}

fn validate_common(
    value: &Value,
    expected_schema: &str,
    campaign_id: &str,
    source: &str,
) -> Result<()> {
    let schema = string_at(value, "/schema", source)?;
    if schema != expected_schema {
        bail!("{source} has schema {schema:?}, expected {expected_schema:?}");
    }
    let schema_version = u64_at(value, "/schema_version", source)?;
    if schema_version != u64::from(SCHEMA_VERSION) {
        bail!("{source} has schema version {schema_version}, expected {SCHEMA_VERSION}");
    }
    let record_campaign_id = string_at(value, "/campaign_id", source)?;
    if record_campaign_id != campaign_id {
        bail!("{source} has campaign id {record_campaign_id:?}, expected {campaign_id:?}");
    }
    Ok(())
}

fn positive_sequence(value: &Value, source: &str) -> Result<u64> {
    let sequence = u64_at(value, "/sequence", source)?;
    if sequence == 0 {
        bail!("{source} has invalid sequence zero");
    }
    Ok(sequence)
}

fn string_at<'a>(value: &'a Value, pointer: &str, source: &str) -> Result<&'a str> {
    value
        .pointer(pointer)
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow::anyhow!("{source} is missing string {pointer}"))
}

fn u64_at(value: &Value, pointer: &str, source: &str) -> Result<u64> {
    value
        .pointer(pointer)
        .and_then(Value::as_u64)
        .ok_or_else(|| anyhow::anyhow!("{source} is missing nonnegative integer {pointer}"))
}

fn i64_at(value: &Value, pointer: &str, source: &str) -> Result<i64> {
    value
        .pointer(pointer)
        .and_then(Value::as_i64)
        .ok_or_else(|| anyhow::anyhow!("{source} is missing integer {pointer}"))
}

fn bool_at(value: &Value, pointer: &str, source: &str) -> Result<bool> {
    value
        .pointer(pointer)
        .and_then(Value::as_bool)
        .ok_or_else(|| anyhow::anyhow!("{source} is missing boolean {pointer}"))
}

fn directory_files(dir: &Path) -> Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    for entry in fs::read_dir(dir).with_context(|| format!("read {}", dir.display()))? {
        let entry = entry.with_context(|| format!("read {} entry", dir.display()))?;
        let path = entry.path();
        if !path.is_file() {
            bail!("evidence entry is not a regular file: {}", path.display());
        }
        files.push(path);
    }
    Ok(files)
}

fn failure_file_sequence(path: &Path) -> Result<u64> {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| anyhow::anyhow!("failure filename is not UTF-8: {}", path.display()))?;
    let stem = name
        .strip_suffix(".json")
        .ok_or_else(|| anyhow::anyhow!("malformed failure filename: {}", path.display()))?;
    let (sequence_text, timestamp_text) = stem
        .split_once('-')
        .ok_or_else(|| anyhow::anyhow!("malformed failure filename: {}", path.display()))?;
    let sequence = sequence_text
        .parse::<u64>()
        .with_context(|| format!("parse failure sequence from {}", path.display()))?;
    let _ = timestamp_text
        .parse::<u64>()
        .with_context(|| format!("parse failure timestamp from {}", path.display()))?;
    if sequence == 0 || sequence_text != format!("{sequence:06}") {
        bail!("non-canonical failure filename: {}", path.display());
    }
    Ok(sequence)
}
