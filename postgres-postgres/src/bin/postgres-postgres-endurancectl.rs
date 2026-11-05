use std::path::PathBuf;
use std::time::Duration;

use clap::{Parser, Subcommand};
use postgres_postgres::endurance::batch::execute_bounded_batch;
use postgres_postgres::endurance::campaign::execute_acceptance_campaign;
use postgres_postgres::endurance::crash::execute_crash_iteration;
use postgres_postgres::endurance::evidence::execute_recorded_iteration;
use postgres_postgres::endurance::guarded_batch::execute_guarded_bounded_batch;
use postgres_postgres::endurance::progress::execute_next_sampled_recorded_iteration;
use postgres_postgres::endurance::resume::reconcile_resume_status;
use postgres_postgres::endurance::sampling::execute_sampled_recorded_iteration;
use postgres_postgres::endurance::{cleanup_business, execute_clean_iteration, plan_iteration};

/// Campaign #203 bounded control surface for deterministic endurance setup,
/// one clean logical iteration, one scheduled crash/recovery iteration, and
/// one evidence-persisted bounded iteration, one resource-sampled recorded
/// iteration, one resume-selected single-step iteration, one explicit finite
/// batch of resume-selected iterations, one externally guarded finite batch,
/// one acceptance-bounded 24h/100-iteration campaign controller, and read-only
/// durable frontier inspection.
///
/// Unbounded daemon/service lifecycle remains outside this control surface.
#[derive(Parser)]
#[command(name = "postgres-postgres-endurancectl")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Emit one deterministic logical-iteration plan as JSON.
    Plan {
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        sequence: u64,
    },
    /// Delete only one iteration's workload-owned destination rows.
    CleanupBusiness {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        import_name: String,
    },
    /// Execute one clean logical iteration through the real workload binary,
    /// independently verify it, then clean only that iteration's business rows.
    RunClean {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        sequence: u64,
    },
    /// Execute one scheduled crash/recovery logical iteration through real
    /// workload, recovery, continuation, and verification child processes.
    RunCrash {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        sequence: u64,
    },
    /// Execute one planned clean or crash/recovery iteration while persisting
    /// durable campaign, event, iteration, and failure evidence.
    RunRecorded {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        evidence_root: PathBuf,
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        sequence: u64,
    },
    /// Execute one recorded iteration while appending bounded resource samples.
    RunSampled {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        evidence_root: PathBuf,
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        sequence: u64,
        #[arg(long, default_value_t = 1000)]
        sample_interval_ms: u64,
    },
    /// Reconcile durable evidence and execute exactly one ready next sequence
    /// through the existing sampled recorded-iteration path.
    RunNext {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        evidence_root: PathBuf,
        #[arg(long)]
        campaign_id: String,
        #[arg(long, default_value_t = 1000)]
        sample_interval_ms: u64,
    },
    /// Execute an explicit finite number of durable-resume-selected iterations.
    RunBatch {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        evidence_root: PathBuf,
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        iterations: u64,
        #[arg(long, default_value_t = 1000)]
        sample_interval_ms: u64,
    },
    /// Execute a finite durable-resume-selected batch with a fail-closed
    /// external guard before every logical iteration.
    RunGuardedBatch {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        evidence_root: PathBuf,
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        iterations: u64,
        #[arg(long, default_value_t = 1000)]
        sample_interval_ms: u64,
        #[arg(long)]
        pre_iteration_guard: PathBuf,
    },
    /// Run until both canonical campaign acceptance floors are satisfied:
    /// elapsed >= 24 hours and completed logical iterations >= 100.
    RunCampaign {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        evidence_root: PathBuf,
        #[arg(long)]
        campaign_id: String,
        #[arg(long, default_value_t = 1000)]
        sample_interval_ms: u64,
        #[arg(long)]
        pre_iteration_guard: PathBuf,
    },
    /// Inspect existing durable evidence and report the next safe sequence
    /// without executing a workload process or mutating campaign evidence.
    ResumeStatus {
        #[arg(long)]
        evidence_root: PathBuf,
        #[arg(long)]
        campaign_id: String,
    },
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Plan {
            campaign_id,
            sequence,
        } => {
            let plan = plan_iteration(&campaign_id, sequence)?;
            println!("{}", serde_json::to_string_pretty(&plan)?);
            Ok(())
        }
        Command::CleanupBusiness {
            database_url,
            import_name,
        } => {
            let deleted_rows = cleanup_business(&database_url, &import_name).await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "import_name": import_name,
                    "deleted_rows": deleted_rows,
                }))?
            );
            Ok(())
        }
        Command::RunClean {
            database_url,
            campaign_id,
            sequence,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result =
                execute_clean_iteration(&workload_binary, &database_url, &campaign_id, sequence)
                    .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::RunCrash {
            database_url,
            campaign_id,
            sequence,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result =
                execute_crash_iteration(&workload_binary, &database_url, &campaign_id, sequence)
                    .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::RunRecorded {
            database_url,
            evidence_root,
            campaign_id,
            sequence,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result = execute_recorded_iteration(
                &workload_binary,
                &database_url,
                &evidence_root,
                &campaign_id,
                sequence,
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::RunSampled {
            database_url,
            evidence_root,
            campaign_id,
            sequence,
            sample_interval_ms,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result = execute_sampled_recorded_iteration(
                &workload_binary,
                &database_url,
                &evidence_root,
                &campaign_id,
                sequence,
                Duration::from_millis(sample_interval_ms),
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::RunNext {
            database_url,
            evidence_root,
            campaign_id,
            sample_interval_ms,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result = execute_next_sampled_recorded_iteration(
                &workload_binary,
                &database_url,
                &evidence_root,
                &campaign_id,
                Duration::from_millis(sample_interval_ms),
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::RunBatch {
            database_url,
            evidence_root,
            campaign_id,
            iterations,
            sample_interval_ms,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result = execute_bounded_batch(
                &workload_binary,
                &database_url,
                &evidence_root,
                &campaign_id,
                iterations,
                Duration::from_millis(sample_interval_ms),
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::RunGuardedBatch {
            database_url,
            evidence_root,
            campaign_id,
            iterations,
            sample_interval_ms,
            pre_iteration_guard,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result = execute_guarded_bounded_batch(
                &workload_binary,
                &database_url,
                &evidence_root,
                &campaign_id,
                iterations,
                Duration::from_millis(sample_interval_ms),
                &pre_iteration_guard,
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::RunCampaign {
            database_url,
            evidence_root,
            campaign_id,
            sample_interval_ms,
            pre_iteration_guard,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result = execute_acceptance_campaign(
                &workload_binary,
                &database_url,
                &evidence_root,
                &campaign_id,
                Duration::from_millis(sample_interval_ms),
                &pre_iteration_guard,
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::ResumeStatus {
            evidence_root,
            campaign_id,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let status = reconcile_resume_status(&evidence_root, &campaign_id, &workload_binary)?;
            println!("{}", serde_json::to_string_pretty(&status)?);
            Ok(())
        }
    }
}

fn sibling_workload_binary() -> anyhow::Result<PathBuf> {
    let current_exe = std::env::current_exe()?;
    let parent = current_exe.parent().ok_or_else(|| {
        anyhow::anyhow!(
            "cannot resolve postgres-postgres sibling directory from {}",
            current_exe.display()
        )
    })?;
    let binary_name = if cfg!(windows) {
        "postgres-postgres.exe"
    } else {
        "postgres-postgres"
    };
    Ok(parent.join(binary_name))
}
