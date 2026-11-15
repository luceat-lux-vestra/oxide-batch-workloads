use std::path::PathBuf;

#[derive(Clone, Copy, Debug, Eq, PartialEq, clap::ValueEnum)]
pub enum FinalizeCrashPoint {
    #[value(name = "before-commit")]
    BeforeCommit,
    #[value(name = "after-commit")]
    AfterCommit,
}

pub async fn pause(marker: &PathBuf, point: FinalizeCrashPoint) {
    let pid = std::process::id();
    let label = match point {
        FinalizeCrashPoint::BeforeCommit => "finalize-before-commit",
        FinalizeCrashPoint::AfterCommit => "finalize-after-commit",
    };
    if let Err(error) = std::fs::write(marker, format!("{pid}\n{label}\n")) {
        eprintln!(
            "finalize failpoint could not write marker {}: {error}",
            marker.display()
        );
        std::process::abort();
    }
    std::future::pending::<()>().await;
    unreachable!("armed finalization failpoint must be externally killed");
}
