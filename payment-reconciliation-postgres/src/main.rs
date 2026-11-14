use anyhow::{bail, Result};
use clap::{CommandFactory, Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(
    name = "payment-reconciliation-postgres",
    about = "OxideBatch payment reconciliation workload scaffold for issue #148"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Create workload-owned schemas and tables.
    Migrate,
    /// Freeze Merchant/provider source representations before batch execution.
    Snapshot,
    /// Produce a deterministic scenario from a frozen base snapshot.
    DeriveScenario,
    /// Execute reconciliation processing through OxideBatch.
    Run,
    /// Continue a crashed execution through public OxideBatch recovery APIs.
    Recover,
    /// Derive CLEAR/HELD and cross the authoritative publication boundary.
    Finalize,
    /// Independently verify source, reconciliation, and publication state.
    Verify,
    /// Clear only workload-owned state.
    Reset,
}

fn main() -> Result<()> {
    Cli::command().debug_assert();
    let cli = Cli::parse();
    let command = match cli.command {
        Command::Migrate => "migrate",
        Command::Snapshot => "snapshot",
        Command::DeriveScenario => "derive-scenario",
        Command::Run => "run",
        Command::Recover => "recover",
        Command::Finalize => "finalize",
        Command::Verify => "verify",
        Command::Reset => "reset",
    };

    bail!(
        "{command} is declared by issue #148 checkpoint 2 but intentionally not implemented in the scaffold"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_contract_is_well_formed() {
        Cli::command().debug_assert();
    }

    #[test]
    fn cli_exposes_frozen_checkpoint_two_commands() {
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
