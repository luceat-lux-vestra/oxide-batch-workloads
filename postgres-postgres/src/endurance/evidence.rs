use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::crash::{execute_crash_iteration_observed, CrashIterationResult};
use super::{
    execute_clean_iteration_observed, plan_iteration, CleanIterationResult, EnduranceFailure,
    IterationObserver, CANONICAL_CHUNK_SIZE, CANONICAL_CURSOR_FETCH_SIZE,
    CANONICAL_PAGING_PAGE_SIZE, CANONICAL_SEED, CANONICAL_SOURCE_ROWS,
};

const CAMPAIGN_SCHEMA: &str = "oxide-batch-workloads.endurance-campaign";
const EVENT_SCHEMA: &str = "oxide-batch-workloads.endurance-event";
const ITERATION_SCHEMA: &str = "oxide-batch-workloads.endurance-iteration";
const FAILURE_SCHEMA: &str = "oxide-batch-workloads.endurance-failure";
const SCHEMA_VERSION: u32 = 1;
const WORKLOAD_NAME: &str = "postgres-postgres";
const OXIDE_BATCH_VERSION: &str = "0.6.0";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct BinaryIdentity {
    pub path: String,
    pub size_bytes: u64,
    pub sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CampaignRecord {
    pub schema: String,
    pub schema_version: u32,
    pub created_at_unix_nanos_utc: u64,
    pub campaign_id: String,
    pub workload: String,
    pub oxide_batch_version: String,
    pub source_rows: u64,
    pub seed: u64,
    pub chunk_size: u32,
    pub cursor_fetch_size: usize,
    pub paging_page_size: usize,
    pub workload_binary: BinaryIdentity,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum FailureClass {
    HarnessEnvironment,
    WorkloadChild,
    Verification,
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", content = "result", rename_all = "kebab-case")]
pub enum RecordedExecutionResult {
    Clean(CleanIterationResult),
    Crash(CrashIterationResult),
}

#[derive(Clone, Debug, Serialize)]
pub struct RecordedIterationOutput {
    pub campaign_id: String,
    pub sequence: u64,
    pub campaign_dir: String,
    pub events_path: String,
    pub iteration_path: String,
    pub execution: RecordedExecutionResult,
}

#[derive(Debug)]
pub struct EvidenceStore {
    campaign: CampaignRecord,
    campaign_dir: PathBuf,
    events_path: PathBuf,
    iterations_dir: PathBuf,
    failures_dir: PathBuf,
}

impl EvidenceStore {
    pub fn open(evidence_root: &Path, campaign_id: &str, workload_binary: &Path) -> Result<Self> {
        // Reuse Slice A validation rather than maintaining a second campaign-id grammar.
        let _ = plan_iteration(campaign_id, 1)?;

        let campaign_dir = evidence_root.join(campaign_id);
        let iterations_dir = campaign_dir.join("iterations");
        let failures_dir = campaign_dir.join("failures");
        fs::create_dir_all(&iterations_dir)
            .with_context(|| format!("create {}", iterations_dir.display()))?;
        fs::create_dir_all(&failures_dir)
            .with_context(|| format!("create {}", failures_dir.display()))?;

        let expected = CampaignRecord {
            schema: CAMPAIGN_SCHEMA.to_owned(),
            schema_version: SCHEMA_VERSION,
            created_at_unix_nanos_utc: now_unix_nanos()?,
            campaign_id: campaign_id.to_owned(),
            workload: WORKLOAD_NAME.to_owned(),
            oxide_batch_version: OXIDE_BATCH_VERSION.to_owned(),
            source_rows: CANONICAL_SOURCE_ROWS,
            seed: CANONICAL_SEED,
            chunk_size: CANONICAL_CHUNK_SIZE,
            cursor_fetch_size: CANONICAL_CURSOR_FETCH_SIZE,
            paging_page_size: CANONICAL_PAGING_PAGE_SIZE,
            workload_binary: binary_identity(workload_binary)?,
        };

        let campaign_path = campaign_dir.join("campaign.json");
        let campaign = if campaign_path.exists() {
            let existing: CampaignRecord = serde_json::from_reader(
                File::open(&campaign_path)
                    .with_context(|| format!("open {}", campaign_path.display()))?,
            )
            .with_context(|| format!("parse {}", campaign_path.display()))?;
            if !same_campaign_identity(&existing, &expected) {
                bail!(
                    "existing campaign metadata conflicts with requested campaign identity: {}",
                    campaign_path.display()
                );
            }
            existing
        } else {
            atomic_write_json(&campaign_path, &expected)?;
            expected
        };

        let events_path = campaign_dir.join("events.jsonl");
        let events = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&events_path)
            .with_context(|| format!("open {}", events_path.display()))?;
        events
            .sync_all()
            .with_context(|| format!("sync {}", events_path.display()))?;

        Ok(Self {
            campaign,
            campaign_dir,
            events_path,
            iterations_dir,
            failures_dir,
        })
    }

    pub fn campaign(&self) -> &CampaignRecord {
        &self.campaign
    }

    pub fn campaign_dir(&self) -> &Path {
        &self.campaign_dir
    }

    pub fn events_path(&self) -> &Path {
        &self.events_path
    }

    pub fn iteration_path(&self, sequence: u64) -> PathBuf {
        self.iterations_dir.join(format!("{sequence:06}.json"))
    }

    pub fn record_event(&self, sequence: u64, phase: &str, detail: Value) -> Result<()> {
        let record = json!({
            "schema": EVENT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "timestamp_unix_nanos_utc": now_unix_nanos()?,
            "campaign_id": self.campaign.campaign_id,
            "sequence": sequence,
            "phase": phase,
            "detail": detail,
        });
        let mut bytes = serde_json::to_vec(&record)?;
        bytes.push(b'\n');

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.events_path)
            .with_context(|| format!("append {}", self.events_path.display()))?;
        file.write_all(&bytes)
            .with_context(|| format!("write {}", self.events_path.display()))?;
        file.flush()
            .with_context(|| format!("flush {}", self.events_path.display()))?;
        file.sync_data()
            .with_context(|| format!("sync {}", self.events_path.display()))?;
        Ok(())
    }

    fn write_success(&self, sequence: u64, execution: &RecordedExecutionResult) -> Result<PathBuf> {
        let path = self.iteration_path(sequence);
        if path.exists() {
            bail!(
                "terminal iteration evidence already exists for sequence {sequence}: {}",
                path.display()
            );
        }

        let record = json!({
            "schema": ITERATION_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "timestamp_unix_nanos_utc": now_unix_nanos()?,
            "campaign_id": self.campaign.campaign_id,
            "sequence": sequence,
            "terminal": "success",
            "execution": execution,
        });
        atomic_write_json(&path, &record)?;
        Ok(path)
    }

    fn write_failure(
        &self,
        sequence: u64,
        class: &FailureClass,
        redacted_error: &str,
    ) -> Result<PathBuf> {
        let timestamp = now_unix_nanos()?;
        let path = self
            .failures_dir
            .join(format!("{sequence:06}-{timestamp}.json"));
        let record = json!({
            "schema": FAILURE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "timestamp_unix_nanos_utc": timestamp,
            "campaign_id": self.campaign.campaign_id,
            "sequence": sequence,
            "terminal": "failure",
            "failure_class": class,
            "error": redacted_error,
        });
        atomic_write_json(&path, &record)?;
        Ok(path)
    }
}

struct EvidenceObserver<'a> {
    store: &'a EvidenceStore,
    sequence: u64,
}

impl IterationObserver for EvidenceObserver<'_> {
    fn record(&mut self, phase: &'static str, detail: Value) -> Result<()> {
        self.store.record_event(self.sequence, phase, detail)
    }
}

pub async fn execute_recorded_iteration(
    workload_binary: &Path,
    database_url: &str,
    evidence_root: &Path,
    campaign_id: &str,
    sequence: u64,
) -> Result<RecordedIterationOutput> {
    let plan = plan_iteration(campaign_id, sequence)?;
    let store = EvidenceStore::open(evidence_root, campaign_id, workload_binary)?;
    let mut observer = EvidenceObserver {
        store: &store,
        sequence,
    };

    let execution_result: Result<RecordedExecutionResult> = match plan.failure {
        EnduranceFailure::Clean => execute_clean_iteration_observed(
            workload_binary,
            database_url,
            campaign_id,
            sequence,
            &mut observer,
        )
        .await
        .map(RecordedExecutionResult::Clean),
        EnduranceFailure::BeforeCommit | EnduranceFailure::AfterCommit => {
            execute_crash_iteration_observed(
                workload_binary,
                database_url,
                campaign_id,
                sequence,
                &mut observer,
            )
            .await
            .map(RecordedExecutionResult::Crash)
        }
    };

    match execution_result {
        Ok(execution) => {
            let iteration_path = store.write_success(sequence, &execution)?;
            store.record_event(
                sequence,
                "iteration-terminal-success",
                json!({
                    "iteration_path": iteration_path.display().to_string(),
                }),
            )?;

            Ok(RecordedIterationOutput {
                campaign_id: campaign_id.to_owned(),
                sequence,
                campaign_dir: store.campaign_dir().display().to_string(),
                events_path: store.events_path().display().to_string(),
                iteration_path: iteration_path.display().to_string(),
                execution,
            })
        }
        Err(error) => {
            let class = classify_failure(&error);
            let redacted = redact_database_url(&format!("{error:#}"), database_url);
            let failure_path = store.write_failure(sequence, &class, &redacted)?;
            store.record_event(
                sequence,
                "iteration-terminal-failure",
                json!({
                    "failure_class": class,
                    "failure_path": failure_path.display().to_string(),
                }),
            )?;
            Err(error)
        }
    }
}

fn classify_failure(error: &anyhow::Error) -> FailureClass {
    let message = format!("{error:#}");
    if message.contains("verify child failed") {
        FailureClass::Verification
    } else if message.contains("run child failed")
        || message.contains("recover child failed")
        || message.contains("continuation child failed")
        || message.contains("plain relaunch unexpectedly succeeded")
    {
        FailureClass::WorkloadChild
    } else {
        FailureClass::HarnessEnvironment
    }
}

fn redact_database_url(message: &str, database_url: &str) -> String {
    if database_url.is_empty() {
        return message.to_owned();
    }
    message.replace(database_url, "[REDACTED_DATABASE_URL]")
}

fn same_campaign_identity(existing: &CampaignRecord, expected: &CampaignRecord) -> bool {
    existing.schema == expected.schema
        && existing.schema_version == expected.schema_version
        && existing.campaign_id == expected.campaign_id
        && existing.workload == expected.workload
        && existing.oxide_batch_version == expected.oxide_batch_version
        && existing.source_rows == expected.source_rows
        && existing.seed == expected.seed
        && existing.chunk_size == expected.chunk_size
        && existing.cursor_fetch_size == expected.cursor_fetch_size
        && existing.paging_page_size == expected.paging_page_size
        && existing.workload_binary == expected.workload_binary
}

fn binary_identity(path: &Path) -> Result<BinaryIdentity> {
    let canonical = fs::canonicalize(path)
        .with_context(|| format!("canonicalize workload binary {}", path.display()))?;
    let metadata = fs::metadata(&canonical)
        .with_context(|| format!("stat workload binary {}", canonical.display()))?;

    let mut reader = BufReader::new(
        File::open(&canonical)
            .with_context(|| format!("open workload binary {}", canonical.display()))?,
    );
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .with_context(|| format!("hash workload binary {}", canonical.display()))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    let digest = hasher.finalize();
    let sha256 = digest
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<Vec<_>>()
        .join("");

    Ok(BinaryIdentity {
        path: canonical.display().to_string(),
        size_bytes: metadata.len(),
        sha256,
    })
}

fn atomic_write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("evidence path has no parent: {}", path.display()))?;
    fs::create_dir_all(parent)
        .with_context(|| format!("create evidence parent {}", parent.display()))?;

    let timestamp = now_unix_nanos()?;
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| {
            anyhow::anyhow!("evidence path has non-UTF-8 file name: {}", path.display())
        })?;
    let temp = parent.join(format!(
        ".{file_name}.tmp-{}-{timestamp}",
        std::process::id()
    ));

    let write_result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)
            .with_context(|| format!("create temporary evidence {}", temp.display()))?;
        serde_json::to_writer_pretty(&mut file, value)
            .with_context(|| format!("serialize temporary evidence {}", temp.display()))?;
        file.write_all(b"\n")
            .with_context(|| format!("finish temporary evidence {}", temp.display()))?;
        file.flush()
            .with_context(|| format!("flush temporary evidence {}", temp.display()))?;
        file.sync_all()
            .with_context(|| format!("sync temporary evidence {}", temp.display()))?;
        fs::rename(&temp, path)
            .with_context(|| format!("rename {} -> {}", temp.display(), path.display()))?;

        if let Ok(directory) = File::open(parent) {
            directory
                .sync_all()
                .with_context(|| format!("sync evidence directory {}", parent.display()))?;
        }
        Ok(())
    })();

    if write_result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    write_result
}

fn now_unix_nanos() -> Result<u64> {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before UNIX epoch")?
        .as_nanos();
    u64::try_from(nanos).context("current UNIX nanosecond timestamp does not fit u64")
}
