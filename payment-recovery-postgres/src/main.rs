use anyhow::{bail, Result};
use clap::{CommandFactory, Parser, Subcommand};
use payment_recovery_postgres::candidate::{self, SnapshotCandidatesRequest};
use payment_recovery_postgres::job;
use payment_recovery_postgres::{CONTRACT_VERSION, OXIDE_BATCH_VERSION};

#[derive(Debug, Parser)]
#[command(
    name = "payment-recovery-postgres",
    about = "OxideBatch payment recovery workload for issue #271"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Create workload-owned recovery schemas and tables.
    Migrate {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
    /// Freeze eligible Merchant UNKNOWN/RETRY_PENDING candidates for one logical run.
    SnapshotCandidates {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, env = "MERCHANT_DATABASE_URL")]
        merchant_database_url: String,
        #[arg(long)]
        run_id: String,
        #[arg(long)]
        business_date: String,
        #[arg(long)]
        eligibility_cutoff: String,
        #[arg(long)]
        scenario_set_id: String,
        #[arg(long, default_value = "payment-recovery-policy-v1")]
        policy_version: String,
    },
    /// Execute payment recovery processing for one frozen run.
    Run {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, env = "MERCHANT_BASE_URL")]
        merchant_base_url: String,
        #[arg(long, env = "PROVIDER_BASE_URL")]
        provider_base_url: String,
        #[arg(long)]
        run_id: String,
        #[arg(long, default_value_t = job::DEFAULT_CHUNK_SIZE)]
        chunk_size: u32,
        #[arg(long, default_value_t = job::DEFAULT_PAGE_SIZE)]
        page_size: usize,
    },
    /// Continue an interrupted OxideBatch execution using public recovery APIs.
    Recover {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        run_id: String,
    },
    /// Independently verify recovery classification and side-effect cardinality.
    Verify {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, env = "PROVIDER_BASE_URL")]
        provider_base_url: String,
        #[arg(long)]
        run_id: String,
    },
    /// Clear only workload-owned recovery state.
    Reset {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    Cli::command().debug_assert();

    match Cli::parse().command {
        Command::Migrate { database_url } => job::migrate(&database_url).await,
        Command::SnapshotCandidates {
            database_url,
            merchant_database_url,
            run_id,
            business_date,
            eligibility_cutoff,
            scenario_set_id,
            policy_version,
        } => {
            let summary = candidate::snapshot_candidates(SnapshotCandidatesRequest {
                database_url: &database_url,
                merchant_database_url: &merchant_database_url,
                run_id: &run_id,
                business_date: &business_date,
                eligibility_cutoff: &eligibility_cutoff,
                scenario_set_id: &scenario_set_id,
                policy_version: &policy_version,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
            Ok(())
        }
        Command::Run {
            database_url,
            merchant_base_url,
            provider_base_url,
            run_id,
            chunk_size,
            page_size,
        } => {
            let report = job::run(job::RunRequest {
                database_url: &database_url,
                merchant_base_url: &merchant_base_url,
                provider_base_url: &provider_base_url,
                run_id: &run_id,
                chunk_size,
                page_size,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
            Ok(())
        }
        Command::Recover { .. } => checkpoint_six("recover"),
        Command::Verify { .. } => checkpoint_seven("verify"),
        Command::Reset { .. } => checkpoint_seven("reset"),
    }
}

fn checkpoint_six(command: &str) -> Result<()> {
    bail!("{command} is intentionally deferred until issue #271 checkpoint 6")
}

fn checkpoint_seven(command: &str) -> Result<()> {
    bail!("{command} is intentionally deferred until issue #271 checkpoint 7")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_contract_is_well_formed() {
        Cli::command().debug_assert();
    }

    #[test]
    fn cli_exposes_frozen_checkpoint_commands() {
        let names: Vec<_> = Cli::command()
            .get_subcommands()
            .map(|command| command.get_name())
            .collect();

        assert_eq!(
            names,
            [
                "migrate",
                "snapshot-candidates",
                "run",
                "recover",
                "verify",
                "reset",
            ]
        );
    }

    #[test]
    fn release_and_contract_identity_are_frozen() {
        assert_eq!(CONTRACT_VERSION, "payment-recovery-v1");
        assert_eq!(OXIDE_BATCH_VERSION, "0.7.0");
    }
}
