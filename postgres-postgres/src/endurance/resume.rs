use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::Path;

use anyhow::Result;
use serde::{Deserialize, Serialize};

use super::evidence::load_existing_campaign_readonly;
use super::{
    plan_iteration, IterationPlan, CANONICAL_CHUNK_SIZE, CANONICAL_CURSOR_FETCH_SIZE,
    CANONICAL_PAGING_PAGE_SIZE, CANONICAL_SEED, CANONICAL_SOURCE_ROWS,
};

const RESUME_SCHEMA: &str = "oxide-batch-workloads.endurance-resume-status";
const EVENT_SCHEMA: &str = "oxide-batch-workloads.endurance-event";
const ITERATION_SCHEMA: &str = "oxide-batch-workloads.endurance-iteration";
const FAILURE_SCHEMA: &str = "oxide-batch-workloads.endurance-failure";
const SCHEMA_VERSION: u32 = 1;
const WORKLOAD_NAME: &str = "postgres-postgres";
const OXIDE_BATCH_VERSION: &str = "0.6.0";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ResumeState {
    Ready,
    BlockedFailure,
    BlockedIncomplete,
    BlockedInconsistentEvidence,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ResumeStatus {
    pub schema: &'static str,
    pub schema_version: u32,
    pub campaign_id: String,
    pub state: ResumeState,
    pub frontier_sequence: u64,
    pub next_sequence: Option<u64>,
    pub next_plan: Option<IterationPlan>,
    pub blocked_sequence: Option<u64>,
    pub reason: Option<String>,
}

#[derive(Debug, Default)]
struct SequenceEvidence {
    lifecycle_events: u64,
    terminal_success_events: u64,
    terminal_failure_events: u64,
    iteration_success: bool,
    failure_files: u64,
}

#[derive(Debug, Deserialize)]
struct EventRecord {
    schema: String,
    schema_version: u32,
    campaign_id: String,
    sequence: u64,
    phase: String,
}

#[derive(Debug, Deserialize)]
struct IterationRecord {
    schema: String,
    schema_version: u32,
    campaign_id: String,
    sequence: u64,
    terminal: String,
}

#[derive(Debug, Deserialize)]
struct FailureRecord {
    schema: String,
    schema_version: u32,
    campaign_id: String,
    sequence: u64,
    terminal: String,
}

pub fn reconcile_resume_status(
    evidence_root: &Path,
    campaign_id: &str,
    workload_binary: &Path,
) -> Result<ResumeStatus> {
    let campaign = match load_existing_campaign_readonly(evidence_root, campaign_id, workload_binary)
    {
        Ok(campaign) => campaign,
        Err(error) => {
            return Ok(inconsistent(
                campaign_id,
                0,
                None,
                format!("campaign identity/provenance validation failed: {error:#}"),
            ));
        }
    };

    if campaign.workload != WORKLOAD_NAME
        || campaign.oxide_batch_version != OXIDE_BATCH_VERSION
        || campaign.source_rows != CANONICAL_SOURCE_ROWS
        || campaign.seed != CANONICAL_SEED
        || campaign.chunk_size != CANONICAL_CHUNK_SIZE
        || campaign.cursor_fetch_size != CANONICAL_CURSOR_FETCH_SIZE
        || campaign.paging_page_size != CANONICAL_PAGING_PAGE_SIZE
    {
        return Ok(inconsistent(
            campaign_id,
            0,
            None,
            "campaign metadata does not match the frozen endurance configuration".to_owned(),
        ));
    }

    let campaign_dir = evidence_root.join(campaign_id);
    let events_path = campaign_dir.join("events.jsonl");
    let iterations_dir = campaign_dir.join("iterations");
    let failures_dir = campaign_dir.join("failures");

    if !events_path.is_file() {
        return Ok(inconsistent(
            campaign_id,
            0,
            None,
            format!("required event log is missing: {}", events_path.display()),
        ));
    }
    if !iterations_dir.is_dir() {
        return Ok(inconsistent(
            campaign_id,
            0,
            None,
            format!(
                "required iteration evidence directory is missing: {}",
                iterations_dir.display()
            ),
        ));
    }
    if !failures_dir.is_dir() {
        return Ok(inconsistent(
            campaign_id,
            0,
            None,
            format!(
                "required failure evidence directory is missing: {}",
                failures_dir.display()
            ),
        ));
    }

    let mut evidence = BTreeMap::<u64, SequenceEvidence>::new();
    if let Err(reason) = scan_events(&events_path, campaign_id, &mut evidence) {
        return Ok(inconsistent(campaign_id, 0, None, reason));
    }
    if let Err(reason) = scan_iterations(&iterations_dir, campaign_id, &mut evidence) {
        return Ok(inconsistent(campaign_id, 0, None, reason));
    }
    if let Err(reason) = scan_failures(&failures_dir, campaign_id, &mut evidence) {
        return Ok(inconsistent(campaign_id, 0, None, reason));
    }

    reconcile_sequences(campaign_id, &evidence)
}

fn scan_events(
    path: &Path,
    campaign_id: &str,
    evidence: &mut BTreeMap<u64, SequenceEvidence>,
) -> std::result::Result<(), String> {
    let file = File::open(path).map_err(|error| format!("open {}: {error}", path.display()))?;
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line_number = index + 1;
        let line =
            line.map_err(|error| format!("read {} line {line_number}: {error}", path.display()))?;
        if line.trim().is_empty() {
            return Err(format!(
                "{} line {line_number} is unexpectedly blank",
                path.display()
            ));
        }
        let record: EventRecord = serde_json::from_str(&line).map_err(|error| {
            format!(
                "parse {} line {line_number} as endurance event: {error}",
                path.display()
            )
        })?;
        validate_common_record(
            &record.schema,
            EVENT_SCHEMA,
            record.schema_version,
            &record.campaign_id,
            campaign_id,
            record.sequence,
            &format!("{} line {line_number}", path.display()),
        )?;
        if record.phase.is_empty() {
            return Err(format!(
                "{} line {line_number} has an empty lifecycle phase",
                path.display()
            ));
        }

        let sequence = evidence.entry(record.sequence).or_default();
        sequence.lifecycle_events += 1;
        match record.phase.as_str() {
            "iteration-terminal-success" => sequence.terminal_success_events += 1,
            "iteration-terminal-failure" => sequence.terminal_failure_events += 1,
            _ => {}
        }
    }
    Ok(())
}

fn scan_iterations(
    dir: &Path,
    campaign_id: &str,
    evidence: &mut BTreeMap<u64, SequenceEvidence>,
) -> std::result::Result<(), String> {
    for entry in fs::read_dir(dir).map_err(|error| format!("read {}: {error}", dir.display()))? {
        let entry = entry.map_err(|error| format!("read {} entry: {error}", dir.display()))?;
        let path = entry.path();
        if !path.is_file() {
            return Err(format!(
                "iteration evidence entry is not a regular file: {}",
                path.display()
            ));
        }
        let sequence = parse_iteration_file_name(&path)?;
        let record: IterationRecord = serde_json::from_reader(
            File::open(&path).map_err(|error| format!("open {}: {error}", path.display()))?,
        )
        .map_err(|error| format!("parse {} as iteration evidence: {error}", path.display()))?;
        validate_common_record(
            &record.schema,
            ITERATION_SCHEMA,
            record.schema_version,
            &record.campaign_id,
            campaign_id,
            record.sequence,
            &path.display().to_string(),
        )?;
        if record.sequence != sequence {
            return Err(format!(
                "iteration evidence sequence {} disagrees with filename sequence {sequence}: {}",
                record.sequence,
                path.display()
            ));
        }
        if record.terminal != "success" {
            return Err(format!(
                "iteration singleton must be terminal success, found {:?}: {}",
                record.terminal,
                path.display()
            ));
        }

        let sequence_evidence = evidence.entry(sequence).or_default();
        if sequence_evidence.iteration_success {
            return Err(format!(
                "duplicate terminal iteration singleton for sequence {sequence}"
            ));
        }
        sequence_evidence.iteration_success = true;
    }
    Ok(())
}

fn scan_failures(
    dir: &Path,
    campaign_id: &str,
    evidence: &mut BTreeMap<u64, SequenceEvidence>,
) -> std::result::Result<(), String> {
    for entry in fs::read_dir(dir).map_err(|error| format!("read {}: {error}", dir.display()))? {
        let entry = entry.map_err(|error| format!("read {} entry: {error}", dir.display()))?;
        let path = entry.path();
        if !path.is_file() {
            return Err(format!(
                "failure evidence entry is not a regular file: {}",
                path.display()
            ));
        }
        let sequence = parse_failure_file_name(&path)?;
        let record: FailureRecord = serde_json::from_reader(
            File::open(&path).map_err(|error| format!("open {}: {error}", path.display()))?,
        )
        .map_err(|error| format!("parse {} as failure evidence: {error}", path.display()))?;
        validate_common_record(
            &record.schema,
            FAILURE_SCHEMA,
            record.schema_version,
            &record.campaign_id,
            campaign_id,
            record.sequence,
            &path.display().to_string(),
        )?;
        if record.sequence != sequence {
            return Err(format!(
                "failure evidence sequence {} disagrees with filename sequence {sequence}: {}",
                record.sequence,
                path.display()
            ));
        }
        if record.terminal != "failure" {
            return Err(format!(
                "failure artifact must be terminal failure, found {:?}: {}",
                record.terminal,
                path.display()
            ));
        }

        evidence.entry(sequence).or_default().failure_files += 1;
    }
    Ok(())
}

fn validate_common_record(
    schema: &str,
    expected_schema: &str,
    schema_version: u32,
    record_campaign_id: &str,
    expected_campaign_id: &str,
    sequence: u64,
    source: &str,
) -> std::result::Result<(), String> {
    if schema != expected_schema {
        return Err(format!(
            "unexpected schema {schema:?}, expected {expected_schema:?}: {source}"
        ));
    }
    if schema_version != SCHEMA_VERSION {
        return Err(format!(
            "unsupported schema version {schema_version}, expected {SCHEMA_VERSION}: {source}"
        ));
    }
    if record_campaign_id != expected_campaign_id {
        return Err(format!(
            "campaign id {record_campaign_id:?} does not match {expected_campaign_id:?}: {source}"
        ));
    }
    if sequence == 0 {
        return Err(format!("sequence zero is invalid: {source}"));
    }
    Ok(())
}

fn parse_iteration_file_name(path: &Path) -> std::result::Result<u64, String> {
    let name = utf8_file_name(path)?;
    let stem = name
        .strip_suffix(".json")
        .ok_or_else(|| format!("malformed iteration evidence filename: {}", path.display()))?;
    let sequence = parse_sequence_text(stem, path)?;
    if name != format!("{sequence:06}.json") {
        return Err(format!(
            "non-canonical iteration evidence filename: {}",
            path.display()
        ));
    }
    Ok(sequence)
}

fn parse_failure_file_name(path: &Path) -> std::result::Result<u64, String> {
    let name = utf8_file_name(path)?;
    let stem = name
        .strip_suffix(".json")
        .ok_or_else(|| format!("malformed failure evidence filename: {}", path.display()))?;
    let (sequence_text, timestamp_text) = stem
        .split_once('-')
        .ok_or_else(|| format!("malformed failure evidence filename: {}", path.display()))?;
    let sequence = parse_sequence_text(sequence_text, path)?;
    if sequence_text != format!("{sequence:06}") {
        return Err(format!(
            "non-canonical failure sequence in filename: {}",
            path.display()
        ));
    }
    if timestamp_text.is_empty()
        || !timestamp_text.bytes().all(|byte| byte.is_ascii_digit())
        || timestamp_text.parse::<u64>().is_err()
    {
        return Err(format!(
            "malformed failure timestamp in filename: {}",
            path.display()
        ));
    }
    Ok(sequence)
}

fn parse_sequence_text(text: &str, path: &Path) -> std::result::Result<u64, String> {
    if text.is_empty() || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(format!(
            "malformed sequence in evidence filename: {}",
            path.display()
        ));
    }
    let sequence = text
        .parse::<u64>()
        .map_err(|error| format!("parse sequence from {}: {error}", path.display()))?;
    if sequence == 0 {
        return Err(format!(
            "sequence zero is invalid in evidence filename: {}",
            path.display()
        ));
    }
    Ok(sequence)
}

fn utf8_file_name(path: &Path) -> std::result::Result<&str, String> {
    path.file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| format!("evidence filename is not UTF-8: {}", path.display()))
}

fn reconcile_sequences(
    campaign_id: &str,
    evidence: &BTreeMap<u64, SequenceEvidence>,
) -> Result<ResumeStatus> {
    let mut frontier = 0_u64;
    let mut sequence = 1_u64;

    loop {
        let Some(current) = evidence.get(&sequence) else {
            if let Some((&higher, _)) = evidence.range((sequence + 1)..).next() {
                return Ok(inconsistent(
                    campaign_id,
                    frontier,
                    Some(sequence),
                    format!(
                        "evidence gap at sequence {sequence}; higher sequence {higher} already exists"
                    ),
                ));
            }
            return ready(campaign_id, frontier, sequence);
        };

        if current.terminal_success_events > 1 || current.terminal_failure_events > 1 {
            return Ok(inconsistent(
                campaign_id,
                frontier,
                Some(sequence),
                format!("duplicate terminal lifecycle outcomes for sequence {sequence}"),
            ));
        }

        let has_success =
            current.terminal_success_events == 1 || current.iteration_success;
        let has_failure = current.terminal_failure_events == 1 || current.failure_files > 0;

        if has_success && has_failure {
            return Ok(inconsistent(
                campaign_id,
                frontier,
                Some(sequence),
                format!("conflicting success and failure evidence for sequence {sequence}"),
            ));
        }

        if current.terminal_success_events == 1 && !current.iteration_success {
            return Ok(inconsistent(
                campaign_id,
                frontier,
                Some(sequence),
                format!(
                    "terminal-success lifecycle exists without iteration singleton for sequence {sequence}"
                ),
            ));
        }
        if current.iteration_success && current.terminal_success_events != 1 {
            return Ok(inconsistent(
                campaign_id,
                frontier,
                Some(sequence),
                format!(
                    "iteration singleton exists without exactly one terminal-success lifecycle for sequence {sequence}"
                ),
            ));
        }

        if has_failure {
            if let Some((&higher, _)) = evidence.range((sequence + 1)..).next() {
                return Ok(inconsistent(
                    campaign_id,
                    frontier,
                    Some(sequence),
                    format!(
                        "sequence {sequence} is failed but higher sequence {higher} evidence exists"
                    ),
                ));
            }
            return Ok(blocked(
                campaign_id,
                ResumeState::BlockedFailure,
                frontier,
                sequence,
                format!("terminal failure evidence blocks sequence {sequence}"),
            ));
        }

        if current.iteration_success && current.terminal_success_events == 1 {
            frontier = sequence;
            sequence = match sequence.checked_add(1) {
                Some(next) => next,
                None => {
                    return Ok(inconsistent(
                        campaign_id,
                        frontier,
                        Some(sequence),
                        "sequence frontier overflowed u64".to_owned(),
                    ));
                }
            };
            continue;
        }

        if current.lifecycle_events > 0 {
            if let Some((&higher, _)) = evidence.range((sequence + 1)..).next() {
                return Ok(inconsistent(
                    campaign_id,
                    frontier,
                    Some(sequence),
                    format!(
                        "sequence {sequence} is incomplete but higher sequence {higher} evidence exists"
                    ),
                ));
            }
            return Ok(blocked(
                campaign_id,
                ResumeState::BlockedIncomplete,
                frontier,
                sequence,
                format!("nonterminal lifecycle evidence blocks sequence {sequence}"),
            ));
        }

        return Ok(inconsistent(
            campaign_id,
            frontier,
            Some(sequence),
            format!("unclassified durable evidence exists for sequence {sequence}"),
        ));
    }
}

fn ready(campaign_id: &str, frontier: u64, next_sequence: u64) -> Result<ResumeStatus> {
    let next_plan = plan_iteration(campaign_id, next_sequence)?;
    Ok(ResumeStatus {
        schema: RESUME_SCHEMA,
        schema_version: SCHEMA_VERSION,
        campaign_id: campaign_id.to_owned(),
        state: ResumeState::Ready,
        frontier_sequence: frontier,
        next_sequence: Some(next_sequence),
        next_plan: Some(next_plan),
        blocked_sequence: None,
        reason: None,
    })
}

fn blocked(
    campaign_id: &str,
    state: ResumeState,
    frontier: u64,
    sequence: u64,
    reason: String,
) -> ResumeStatus {
    ResumeStatus {
        schema: RESUME_SCHEMA,
        schema_version: SCHEMA_VERSION,
        campaign_id: campaign_id.to_owned(),
        state,
        frontier_sequence: frontier,
        next_sequence: None,
        next_plan: None,
        blocked_sequence: Some(sequence),
        reason: Some(reason),
    }
}

fn inconsistent(
    campaign_id: &str,
    frontier: u64,
    sequence: Option<u64>,
    reason: String,
) -> ResumeStatus {
    ResumeStatus {
        schema: RESUME_SCHEMA,
        schema_version: SCHEMA_VERSION,
        campaign_id: campaign_id.to_owned(),
        state: ResumeState::BlockedInconsistentEvidence,
        frontier_sequence: frontier,
        next_sequence: None,
        next_plan: None,
        blocked_sequence: sequence,
        reason: Some(reason),
    }
}
