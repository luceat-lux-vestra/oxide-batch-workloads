mod evidence;
mod failpoint;
mod finalize;
mod finalize_failpoint;
mod job;
mod model;
mod oracle;
mod processor;
mod snapshot;
mod writer;

use anyhow::{bail, Result};
use clap::{CommandFactory, Parser, Subcommand, ValueEnum};

#[derive(Debug, Parser)]
#[command(
    name = "payment-reconciliation-postgres",
    about = "OxideBatch payment reconciliation workload for issue #148"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum DerivedScenario {
    MissingProvider,
    MissingInternal,
    DuplicateProvider,
    AmountMismatch,
}

impl DerivedScenario {
    fn as_contract_name(self) -> &'static str {
        match self {
            Self::MissingProvider => "MISSING_PROVIDER",
            Self::MissingInternal => "MISSING_INTERNAL",
            Self::DuplicateProvider => "DUPLICATE_PROVIDER",
            Self::AmountMismatch => "AMOUNT_MISMATCH",
        }
    }
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Create workload-owned schemas and tables.
    Migrate {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
    /// Freeze Merchant/provider source representations before batch execution.
    Snapshot {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, env = "MERCHANT_DATABASE_URL")]
        merchant_database_url: String,
        #[arg(long, env = "PROVIDER_BASE_URL")]
        provider_base_url: String,
        #[arg(long)]
        snapshot_id: String,
        #[arg(long)]
        business_date: String,
        /// v1 provider-account scope: Reference Merchant merchant_id.
        #[arg(long)]
        provider_scope: String,
        #[arg(long)]
        policy_version: Option<String>,
    },
    /// Produce a deterministic scenario from a frozen CLEAR base snapshot.
    DeriveScenario {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        base_snapshot_id: String,
        #[arg(long)]
        snapshot_id: String,
        #[arg(long, value_enum)]
        scenario: DerivedScenario,
        #[arg(long, default_value_t = 1)]
        delta_minor: i64,
    },
    /// Execute reconciliation processing through OxideBatch over one frozen snapshot.
    Run {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        snapshot_id: String,
        #[arg(long, default_value_t = job::DEFAULT_CHUNK_SIZE)]
        chunk_size: u32,
        #[arg(long, default_value_t = job::DEFAULT_PAGE_SIZE)]
        page_size: usize,
        #[arg(long, default_value_t = 0)]
        fail_at_chunk: u32,
        #[arg(long, value_enum, default_value = "during-write")]
        failure_mode: failpoint::FailureMode,
        #[arg(long)]
        pause_for_kill: Option<std::path::PathBuf>,
        #[arg(long)]
        pause_after_processing_commit: Option<std::path::PathBuf>,
    },
    /// Continue a hard-crashed execution through public OxideBatch recovery APIs.
    Recover {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        snapshot_id: String,
    },
    /// Derive CLEAR/HELD and cross the authoritative publication boundary.
    Finalize {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        snapshot_id: String,
        #[arg(long, value_enum)]
        finalize_crash_point: Option<finalize_failpoint::FinalizeCrashPoint>,
        #[arg(long)]
        pause_for_kill: Option<std::path::PathBuf>,
    },
    /// Independently verify the frozen source snapshot and expected business verdict.
    Verify {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        snapshot_id: String,
    },
    /// Clear only workload-owned state.
    Reset,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    Cli::command().debug_assert();
    match Cli::parse().command {
        Command::Migrate { database_url } => snapshot::migrate(&database_url).await,
        Command::Snapshot {
            database_url,
            merchant_database_url,
            provider_base_url,
            snapshot_id,
            business_date,
            provider_scope,
            policy_version,
        } => {
            let bundle = snapshot::acquire_base_snapshot(snapshot::AcquireRequest {
                database_url: &database_url,
                merchant_database_url: &merchant_database_url,
                provider_base_url: &provider_base_url,
                snapshot_id: &snapshot_id,
                business_date: &business_date,
                provider_scope: &provider_scope,
                policy_version: policy_version.as_deref(),
            })
            .await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "snapshot_id": bundle.metadata.snapshot_id,
                    "scenario_case": bundle.metadata.scenario_case,
                    "internal_rows": bundle.internal.len(),
                    "provider_rows": bundle.provider.len(),
                    "internal_snapshot_digest": bundle.metadata.internal_snapshot_digest,
                    "provider_snapshot_digest": bundle.metadata.provider_snapshot_digest
                }))?
            );
            Ok(())
        }
        Command::DeriveScenario {
            database_url,
            base_snapshot_id,
            snapshot_id,
            scenario,
            delta_minor,
        } => {
            let bundle = snapshot::derive_scenario(snapshot::DeriveRequest {
                database_url: &database_url,
                base_snapshot_id: &base_snapshot_id,
                snapshot_id: &snapshot_id,
                scenario_case: scenario.as_contract_name(),
                delta_minor,
            })
            .await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "snapshot_id": bundle.metadata.snapshot_id,
                    "base_snapshot_id": bundle.metadata.base_snapshot_id,
                    "scenario_case": bundle.metadata.scenario_case,
                    "internal_snapshot_digest": bundle.metadata.internal_snapshot_digest,
                    "provider_snapshot_digest": bundle.metadata.provider_snapshot_digest
                }))?
            );
            Ok(())
        }
        Command::Verify {
            database_url,
            snapshot_id,
        } => {
            let report = evidence::verify(&database_url, &snapshot_id).await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
            if report.violations.is_empty() {
                Ok(())
            } else {
                bail!(
                    "independent verification reported {} violation(s)",
                    report.violations.len()
                )
            }
        }
        Command::Run {
            database_url,
            snapshot_id,
            chunk_size,
            page_size,
            fail_at_chunk,
            failure_mode,
            pause_for_kill,
            pause_after_processing_commit,
        } => {
            let run_id = job::run(job::RunRequest {
                database_url: &database_url,
                snapshot_id: &snapshot_id,
                chunk_size,
                page_size,
                fail_at_chunk,
                failure_mode,
                pause_for_kill,
                pause_after_processing_commit,
            })
            .await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "run_id": run_id,
                    "snapshot_id": snapshot_id,
                    "processing_state": "PROCESSED"
                }))?
            );
            Ok(())
        }
        Command::Recover {
            database_url,
            snapshot_id,
        } => {
            let run_id = job::recover(&database_url, &snapshot_id).await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "run_id": run_id,
                    "snapshot_id": snapshot_id,
                    "recovery_state": "PENDING"
                }))?
            );
            Ok(())
        }
        Command::Finalize {
            database_url,
            snapshot_id,
            finalize_crash_point,
            pause_for_kill,
        } => {
            let report = finalize::finalize(
                &database_url,
                &snapshot_id,
                finalize_crash_point,
                pause_for_kill,
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
            Ok(())
        }
        Command::Reset => checkpoint_six("reset"),
    }
}

fn checkpoint_six(command: &str) -> Result<()> {
    bail!("{command} remains intentionally unimplemented before issue #148 checkpoint 6")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_contract_is_well_formed() {
        Cli::command().debug_assert();
    }

    #[test]
    fn cli_exposes_frozen_commands() {
        let command = Cli::command();
        let names: Vec<_> = command
            .get_subcommands()
            .map(|subcommand| subcommand.get_name())
            .collect();

        assert_eq!(
            names,
            [
                "migrate",
                "snapshot",
                "derive-scenario",
                "run",
                "recover",
                "finalize",
                "verify",
                "reset",
            ]
        );
    }
}
