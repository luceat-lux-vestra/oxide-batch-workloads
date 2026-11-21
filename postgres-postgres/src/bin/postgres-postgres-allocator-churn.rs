use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use clap::Parser;
use postgres_postgres::endurance::sampling::{
    LinuxResourceProbe, ProcessObservation, ResourceProbe,
};
use postgres_postgres::endurance::{
    plan_iteration, EnduranceFailure, EnduranceReader, CANONICAL_CHUNK_SIZE,
    CANONICAL_CURSOR_FETCH_SIZE, CANONICAL_PAGING_PAGE_SIZE,
};
use postgres_postgres::failpoint::FailureMode;
use postgres_postgres::job::{self, ReaderMode};
use serde::Serialize;
use sqlx::postgres::PgPoolOptions;
use tokio::time::sleep;

const REPORT_SCHEMA: &str = "oxide-batch-allocator-same-process-churn/v1";
const MIN_CYCLES: u64 = 2;
const MAX_CYCLES: u64 = 22;
const MAX_IDLE_MILLIS: u64 = 600_000;

#[derive(Debug, Parser)]
#[command(
    name = "postgres-postgres-allocator-churn",
    about = "Run repeated real postgres-postgres workloads in one process for allocator retention evidence"
)]
struct Cli {
    #[arg(long, env = "DATABASE_URL", hide_env_values = true)]
    database_url: String,
    #[arg(long)]
    campaign_id: String,
    #[arg(long, default_value_t = 20)]
    cycles: u64,
    #[arg(long, default_value_t = 1_000)]
    idle_ms: u64,
    #[arg(long)]
    native_telemetry_root: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
struct PhaseSample {
    phase: &'static str,
    process: ProcessObservation,
}

#[derive(Debug, Serialize)]
struct NativeSnapshots {
    post_run: Option<String>,
    post_cleanup: Option<String>,
    post_idle: Option<String>,
}

#[derive(Debug, Serialize)]
struct CycleReport {
    sequence: u64,
    reader: &'static str,
    import_name: String,
    projection_rows: u64,
    cleanup_deleted_rows: u64,
    pre_run: PhaseSample,
    post_run: PhaseSample,
    post_cleanup: PhaseSample,
    post_idle: PhaseSample,
    native: NativeSnapshots,
}

#[derive(Debug, Serialize)]
struct ChurnReport {
    schema: &'static str,
    allocator: &'static str,
    process_id: u32,
    campaign_id: String,
    source_rows: u64,
    cycles: u64,
    cursor_cycles: u64,
    paging_cycles: u64,
    idle_ms: u64,
    chunk_size: u32,
    cursor_fetch_size: usize,
    paging_page_size: usize,
    native_telemetry_enabled: bool,
    cycle_reports: Vec<CycleReport>,
    limitations: Vec<&'static str>,
}

fn validate_cli(cli: &Cli) -> Result<()> {
    if !(MIN_CYCLES..=MAX_CYCLES).contains(&cli.cycles) {
        bail!("cycles must be between {MIN_CYCLES} and {MAX_CYCLES}");
    }
    if !cli.cycles.is_multiple_of(2) {
        bail!("cycles must be even so cursor/paging counts remain balanced");
    }
    if cli.idle_ms == 0 || cli.idle_ms > MAX_IDLE_MILLIS {
        bail!("idle-ms must be between 1 and {MAX_IDLE_MILLIS}");
    }
    let terminal_plan = plan_iteration(&cli.campaign_id, cli.cycles)?;
    if terminal_plan.failure != EnduranceFailure::Clean {
        bail!(
            "cycle {} is reserved for crash/recovery orchestration; same-process churn is clean-only",
            cli.cycles
        );
    }
    Ok(())
}

fn sample_process(probe: &LinuxResourceProbe, phase: &'static str) -> Result<PhaseSample> {
    Ok(PhaseSample {
        phase,
        process: probe.process(std::process::id(), "same-process-churn")?,
    })
}

fn capture_native(
    root: Option<&Path>,
    sequence: u64,
    phase: &'static str,
) -> Result<Option<String>> {
    let Some(root) = root else {
        return Ok(None);
    };
    let output_dir = root.join(format!("{sequence:06}")).join(phase);
    postgres_postgres::allocator_telemetry::capture_to(&output_dir)?;
    Ok(Some(output_dir.join("snapshot.json").display().to_string()))
}

async fn source_row_count(pool: &sqlx::PgPool) -> Result<u64> {
    let count = sqlx::query_scalar::<_, i64>("SELECT count(*) FROM app_source.source_customer")
        .fetch_one(pool)
        .await?;
    u64::try_from(count).context("source row count must be non-negative")
}

async fn projection_row_count(pool: &sqlx::PgPool, import_name: &str) -> Result<u64> {
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT count(*) FROM app_business.customer_projection WHERE import_name = $1",
    )
    .bind(import_name)
    .fetch_one(pool)
    .await?;
    u64::try_from(count).context("projection row count must be non-negative")
}

async fn cleanup_projection(pool: &sqlx::PgPool, import_name: &str) -> Result<u64> {
    Ok(
        sqlx::query("DELETE FROM app_business.customer_projection WHERE import_name = $1")
            .bind(import_name)
            .execute(pool)
            .await?
            .rows_affected(),
    )
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let _allocator_background_actions =
        postgres_postgres::allocator::initialize_selected_allocator();

    let cli = Cli::parse();
    validate_cli(&cli)?;

    let control_pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&cli.database_url)
        .await?;
    let source_rows = source_row_count(&control_pool).await?;
    if source_rows == 0 {
        bail!("source table must be seeded before same-process churn execution");
    }

    let probe = LinuxResourceProbe;
    let process_id = std::process::id();
    let mut cycle_reports =
        Vec::with_capacity(usize::try_from(cli.cycles).context("cycle count does not fit usize")?);

    for sequence in 1..=cli.cycles {
        let plan = plan_iteration(&cli.campaign_id, sequence)?;
        if plan.failure != EnduranceFailure::Clean {
            bail!(
                "sequence {sequence} is reserved for crash/recovery orchestration; same-process churn is clean-only"
            );
        }
        let (reader, reader_label, fetch_size, page_size) = match plan.reader {
            EnduranceReader::Cursor => (
                ReaderMode::Cursor,
                "cursor",
                Some(CANONICAL_CURSOR_FETCH_SIZE),
                None,
            ),
            EnduranceReader::Paging => (
                ReaderMode::Paging,
                "paging",
                None,
                Some(CANONICAL_PAGING_PAGE_SIZE),
            ),
        };

        let pre_run = sample_process(&probe, "pre-run")?;
        job::run(
            &cli.database_url,
            &plan.import_name,
            CANONICAL_CHUNK_SIZE,
            reader,
            fetch_size,
            page_size,
            0,
            FailureMode::DuringWrite,
            None,
        )
        .await
        .with_context(|| format!("same-process churn sequence {sequence} workload failed"))?;
        let post_run = sample_process(&probe, "post-run")?;
        let native_post_run =
            capture_native(cli.native_telemetry_root.as_deref(), sequence, "post-run")?;

        let projection_rows = projection_row_count(&control_pool, &plan.import_name).await?;
        if projection_rows != source_rows {
            bail!("sequence {sequence} projected {projection_rows} rows, expected {source_rows}");
        }
        let cleanup_deleted_rows = cleanup_projection(&control_pool, &plan.import_name).await?;
        if cleanup_deleted_rows != source_rows {
            bail!(
                "sequence {sequence} cleanup deleted {cleanup_deleted_rows} rows, expected {source_rows}"
            );
        }
        let post_cleanup = sample_process(&probe, "post-cleanup")?;
        let native_post_cleanup = capture_native(
            cli.native_telemetry_root.as_deref(),
            sequence,
            "post-cleanup",
        )?;

        sleep(Duration::from_millis(cli.idle_ms)).await;
        let post_idle = sample_process(&probe, "post-idle")?;
        let native_post_idle =
            capture_native(cli.native_telemetry_root.as_deref(), sequence, "post-idle")?;

        cycle_reports.push(CycleReport {
            sequence,
            reader: reader_label,
            import_name: plan.import_name,
            projection_rows,
            cleanup_deleted_rows,
            pre_run,
            post_run,
            post_cleanup,
            post_idle,
            native: NativeSnapshots {
                post_run: native_post_run,
                post_cleanup: native_post_cleanup,
                post_idle: native_post_idle,
            },
        });
    }

    control_pool.close().await;

    let report = ChurnReport {
        schema: REPORT_SCHEMA,
        allocator: postgres_postgres::allocator::selected_allocator(),
        process_id,
        campaign_id: cli.campaign_id,
        source_rows,
        cycles: cli.cycles,
        cursor_cycles: cli.cycles / 2,
        paging_cycles: cli.cycles / 2,
        idle_ms: cli.idle_ms,
        chunk_size: CANONICAL_CHUNK_SIZE,
        cursor_fetch_size: CANONICAL_CURSOR_FETCH_SIZE,
        paging_page_size: CANONICAL_PAGING_PAGE_SIZE,
        native_telemetry_enabled: cli.native_telemetry_root.is_some(),
        cycle_reports,
        limitations: vec![
            "same-process churn is allocator retention evidence, not benchmark timing",
            "the process intentionally remains alive across every cycle for one allocator",
            "business rows are deleted after each successful cycle while OxideBatch metadata accumulates",
            "the source digest pass performed by job::run is part of the real workload and contributes allocation churn",
            "phase-boundary RSS/PSS observations do not replace Phase 4 peak-memory sampling",
            "this lane checks completed execution and projection cardinality; Phase 5A retains the independent semantic verifier",
            "allocator-native payloads retain allocator-specific semantics and are not normalized into one fragmentation score",
            "rising RSS or PSS alone is not a memory-leak verdict",
        ],
    };
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
