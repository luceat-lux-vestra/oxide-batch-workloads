//! Campaign #203 support primitives for the S10 endurance harness.
//!
//! This module contains deterministic planning, one clean child-process
//! iteration, and workload-owned business cleanup. Long-running loops, fault
//! delivery, recovery, sampling, evidence persistence, and resume state belong
//! to later campaign slices.

pub mod crash;

use std::path::Path;
use std::process::{Output, Stdio};

use anyhow::{bail, Context, Result};
use serde::Serialize;
use sqlx::postgres::PgPoolOptions;
use tokio::process::Command;

const MAX_CAMPAIGN_ID_LEN: usize = 48;

pub const CANONICAL_SOURCE_ROWS: u64 = 200_000;
pub const CANONICAL_SEED: u64 = 20_260_904;
pub const CANONICAL_CHUNK_SIZE: u32 = 1_000;
pub const CANONICAL_CURSOR_FETCH_SIZE: usize = 500;
pub const CANONICAL_PAGING_PAGE_SIZE: usize = 750;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum EnduranceReader {
    Cursor,
    Paging,
}

impl EnduranceReader {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Cursor => "cursor",
            Self::Paging => "paging",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum EnduranceFailure {
    Clean,
    BeforeCommit,
    AfterCommit,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct IterationPlan {
    pub campaign_id: String,
    pub sequence: u64,
    pub reader: EnduranceReader,
    pub failure: EnduranceFailure,
    pub import_name: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ChildProcessResult {
    pub pid: u32,
    pub success: bool,
    pub code: Option<i32>,
    pub status: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct CleanIterationResult {
    pub campaign_id: String,
    pub sequence: u64,
    pub reader: EnduranceReader,
    pub import_name: String,
    pub chunk_size: u32,
    pub fetch_size: Option<usize>,
    pub page_size: Option<usize>,
    pub run: ChildProcessResult,
    pub verification: ChildProcessResult,
    pub cleanup_deleted_rows: u64,
}

pub fn plan_iteration(campaign_id: &str, sequence: u64) -> Result<IterationPlan> {
    validate_campaign_id(campaign_id)?;
    if sequence == 0 {
        bail!("sequence must be greater than zero");
    }

    let reader = if sequence % 2 == 1 {
        EnduranceReader::Cursor
    } else {
        EnduranceReader::Paging
    };
    let failure = match sequence % 50 {
        24 => EnduranceFailure::BeforeCommit,
        49 => EnduranceFailure::AfterCommit,
        _ => EnduranceFailure::Clean,
    };
    let import_name = format!("endurance_{campaign_id}_{sequence:06}_{}", reader.as_str());

    Ok(IterationPlan {
        campaign_id: campaign_id.to_owned(),
        sequence,
        reader,
        failure,
        import_name,
    })
}

pub async fn execute_clean_iteration(
    workload_binary: &Path,
    database_url: &str,
    campaign_id: &str,
    sequence: u64,
) -> Result<CleanIterationResult> {
    let plan = plan_iteration(campaign_id, sequence)?;
    if plan.failure != EnduranceFailure::Clean {
        let failure = match plan.failure {
            EnduranceFailure::BeforeCommit => "before-commit",
            EnduranceFailure::AfterCommit => "after-commit",
            EnduranceFailure::Clean => "clean",
        };
        bail!(
            "sequence {sequence} is reserved for {failure} crash/recovery orchestration and cannot run through the clean Slice B path"
        );
    }

    let (fetch_size, page_size) = match plan.reader {
        EnduranceReader::Cursor => (Some(CANONICAL_CURSOR_FETCH_SIZE), None),
        EnduranceReader::Paging => (None, Some(CANONICAL_PAGING_PAGE_SIZE)),
    };

    let mut run_command = Command::new(workload_binary);
    run_command
        .arg("run")
        .arg("--database-url")
        .arg(database_url)
        .arg("--import-name")
        .arg(&plan.import_name)
        .arg("--chunk-size")
        .arg(CANONICAL_CHUNK_SIZE.to_string())
        .arg("--reader")
        .arg(plan.reader.as_str());
    match plan.reader {
        EnduranceReader::Cursor => {
            run_command
                .arg("--fetch-size")
                .arg(CANONICAL_CURSOR_FETCH_SIZE.to_string());
        }
        EnduranceReader::Paging => {
            run_command
                .arg("--page-size")
                .arg(CANONICAL_PAGING_PAGE_SIZE.to_string());
        }
    }

    let (run, run_output) = run_child(run_command, "run").await?;
    require_success("run", &run, &run_output)?;

    let mut verify_command = Command::new(workload_binary);
    verify_command
        .arg("verify")
        .arg("--database-url")
        .arg(database_url)
        .arg("--import-name")
        .arg(&plan.import_name);
    let (verification, verify_output) = run_child(verify_command, "verify").await?;
    require_success("verify", &verification, &verify_output)?;

    let cleanup_deleted_rows = cleanup_business(database_url, &plan.import_name).await?;

    Ok(CleanIterationResult {
        campaign_id: plan.campaign_id,
        sequence: plan.sequence,
        reader: plan.reader,
        import_name: plan.import_name,
        chunk_size: CANONICAL_CHUNK_SIZE,
        fetch_size,
        page_size,
        run,
        verification,
        cleanup_deleted_rows,
    })
}

async fn run_child(mut command: Command, phase: &str) -> Result<(ChildProcessResult, Output)> {
    command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    let child = command
        .spawn()
        .with_context(|| format!("spawn postgres-postgres {phase} child"))?;
    let pid = child
        .id()
        .ok_or_else(|| anyhow::anyhow!("postgres-postgres {phase} child has no process id"))?;
    let output = child
        .wait_with_output()
        .await
        .with_context(|| format!("wait for postgres-postgres {phase} child"))?;
    let process = ChildProcessResult {
        pid,
        success: output.status.success(),
        code: output.status.code(),
        status: output.status.to_string(),
    };
    Ok((process, output))
}

fn require_success(phase: &str, process: &ChildProcessResult, output: &Output) -> Result<()> {
    if process.success {
        return Ok(());
    }

    bail!(
        "{phase} child failed: pid={} status={} code={:?}\nstdout={}\nstderr={}",
        process.pid,
        process.status,
        process.code,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}

/// Delete only one verified iteration's workload-owned business projection.
///
/// Cleanup fails closed unless the import identity is exactly one that
/// plan_iteration can generate. This prevents the campaign-only control
/// surface from deleting unrelated workload output even when an operator
/// supplies the wrong import name.
///
/// This operation never touches the immutable source table or OxideBatch's
/// oxide_batch.* metadata. It is intentionally separate from job::reset,
/// whose development/test contract truncates both workload-owned tables.
pub async fn cleanup_business(database_url: &str, import_name: &str) -> Result<u64> {
    validate_endurance_import_name(import_name)?;

    let pool = PgPoolOptions::new().connect(database_url).await?;
    let delete_result =
        sqlx::query("DELETE FROM app_business.customer_projection WHERE import_name = $1")
            .bind(import_name)
            .execute(&pool)
            .await;
    pool.close().await;

    Ok(delete_result?.rows_affected())
}

fn validate_campaign_id(campaign_id: &str) -> Result<()> {
    if campaign_id.is_empty() {
        bail!("campaign id must not be empty");
    }
    if campaign_id.len() > MAX_CAMPAIGN_ID_LEN {
        bail!("campaign id must be at most {MAX_CAMPAIGN_ID_LEN} bytes");
    }
    if !campaign_id
        .bytes()
        .next()
        .is_some_and(|byte| byte.is_ascii_alphanumeric())
    {
        bail!("campaign id must start with an ASCII alphanumeric character");
    }
    if !campaign_id
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        bail!("campaign id may contain only ASCII alphanumeric characters, '-' and '_'");
    }
    Ok(())
}

fn validate_endurance_import_name(import_name: &str) -> Result<()> {
    let rest = import_name
        .strip_prefix("endurance_")
        .ok_or_else(|| anyhow::anyhow!("import name is not an endurance campaign identity"))?;
    let (identity, reader) = rest
        .rsplit_once('_')
        .ok_or_else(|| anyhow::anyhow!("endurance import name is missing reader mode"))?;
    if reader != "cursor" && reader != "paging" {
        bail!("endurance import name has unsupported reader mode");
    }

    let (campaign_id, sequence_text) = identity
        .rsplit_once('_')
        .ok_or_else(|| anyhow::anyhow!("endurance import name is missing sequence"))?;
    validate_campaign_id(campaign_id)?;
    if sequence_text.len() < 6 || !sequence_text.bytes().all(|byte| byte.is_ascii_digit()) {
        bail!("endurance import sequence must be a zero-padded decimal number");
    }
    let sequence: u64 = sequence_text.parse()?;
    if sequence == 0 {
        bail!("endurance import sequence must be greater than zero");
    }

    let expected = plan_iteration(campaign_id, sequence)?.import_name;
    if import_name != expected {
        bail!("import name does not match the canonical endurance identity");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        plan_iteration, validate_endurance_import_name, EnduranceFailure, EnduranceReader,
    };

    #[test]
    fn deterministic_schedule_covers_frozen_boundaries() -> anyhow::Result<()> {
        let cases = [
            (23, EnduranceReader::Cursor, EnduranceFailure::Clean),
            (24, EnduranceReader::Paging, EnduranceFailure::BeforeCommit),
            (25, EnduranceReader::Cursor, EnduranceFailure::Clean),
            (48, EnduranceReader::Paging, EnduranceFailure::Clean),
            (49, EnduranceReader::Cursor, EnduranceFailure::AfterCommit),
            (50, EnduranceReader::Paging, EnduranceFailure::Clean),
            (74, EnduranceReader::Paging, EnduranceFailure::BeforeCommit),
            (99, EnduranceReader::Cursor, EnduranceFailure::AfterCommit),
            (100, EnduranceReader::Paging, EnduranceFailure::Clean),
        ];

        for (sequence, reader, failure) in cases {
            let plan = plan_iteration("s10-baseline", sequence)?;
            assert_eq!(plan.sequence, sequence);
            assert_eq!(plan.reader, reader);
            assert_eq!(plan.failure, failure);
        }

        Ok(())
    }

    #[test]
    fn import_identity_is_stable_and_sequence_scoped() -> anyhow::Result<()> {
        let first = plan_iteration("s10-baseline", 1)?;
        let second = plan_iteration("s10-baseline", 2)?;

        assert_eq!(first.import_name, "endurance_s10-baseline_000001_cursor");
        assert_eq!(second.import_name, "endurance_s10-baseline_000002_paging");
        assert_ne!(first.import_name, second.import_name);
        assert_eq!(first, plan_iteration("s10-baseline", 1)?);

        Ok(())
    }

    #[test]
    fn invalid_campaign_identity_and_zero_sequence_fail_closed() {
        assert!(plan_iteration("", 1).is_err());
        assert!(plan_iteration("-starts-with-punctuation", 1).is_err());
        assert!(plan_iteration("contains/slash", 1).is_err());
        assert!(plan_iteration("s10-baseline", 0).is_err());
        assert!(plan_iteration(&"a".repeat(49), 1).is_err());
    }

    #[test]
    fn cleanup_identity_accepts_only_canonical_endurance_names() -> anyhow::Result<()> {
        let canonical = plan_iteration("s10-baseline", 24)?.import_name;
        assert!(validate_endurance_import_name(&canonical).is_ok());

        assert!(validate_endurance_import_name("customers_transform").is_err());
        assert!(validate_endurance_import_name("endurance_s10-baseline_24_paging").is_err());
        assert!(validate_endurance_import_name("endurance_s10-baseline_000024_cursor").is_err());
        assert!(validate_endurance_import_name("endurance_s10-baseline_000000_paging").is_err());

        Ok(())
    }
}
