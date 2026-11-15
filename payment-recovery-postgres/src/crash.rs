use std::path::PathBuf;

use anyhow::{bail, Result};

#[derive(Clone, Copy, Debug, Eq, PartialEq, clap::ValueEnum)]
pub enum CrashPoint {
    #[value(name = "after-candidate-read-before-replay")]
    AfterCandidateReadBeforeReplay,
    #[value(name = "while-replay-outcome-unknown")]
    WhileReplayOutcomeUnknown,
    #[value(name = "after-external-convergence-before-local-commit")]
    AfterExternalConvergenceBeforeLocalCommit,
    #[value(name = "after-local-write-before-checkpoint-commit")]
    AfterLocalWriteBeforeCheckpointCommit,
}

impl CrashPoint {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::AfterCandidateReadBeforeReplay => "after-candidate-read-before-replay",
            Self::WhileReplayOutcomeUnknown => "while-replay-outcome-unknown",
            Self::AfterExternalConvergenceBeforeLocalCommit => {
                "after-external-convergence-before-local-commit"
            }
            Self::AfterLocalWriteBeforeCheckpointCommit => {
                "after-local-write-before-checkpoint-commit"
            }
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct CrashControl {
    point: Option<CrashPoint>,
    candidate_id: Option<String>,
    marker: Option<PathBuf>,
}

impl CrashControl {
    pub fn new(
        point: Option<CrashPoint>,
        candidate_id: Option<String>,
        marker: Option<PathBuf>,
    ) -> Result<Self> {
        match (&point, &candidate_id, &marker) {
            (None, None, None) => Ok(Self::default()),
            (Some(_), Some(candidate_id), Some(_)) if candidate_id.trim().is_empty() => {
                bail!("crash candidate id must not be blank")
            }
            (Some(_), Some(_), Some(_)) => Ok(Self {
                point,
                candidate_id,
                marker,
            }),
            _ => bail!(
                "--crash-point, --crash-candidate-id, and --pause-for-kill must be supplied together"
            ),
        }
    }

    pub const fn disabled() -> Self {
        Self {
            point: None,
            candidate_id: None,
            marker: None,
        }
    }

    pub fn is_armed_for(&self, point: CrashPoint, candidate_id: &str) -> bool {
        self.point == Some(point) && self.candidate_id.as_deref() == Some(candidate_id)
    }

    pub async fn pause_if(&self, point: CrashPoint, candidate_id: &str) {
        if !self.is_armed_for(point, candidate_id) {
            return;
        }

        let Some(marker) = &self.marker else {
            std::process::abort();
        };
        let pid = std::process::id();
        let payload = format!("{pid}\n{}\n{candidate_id}\n", point.as_str());
        if let Err(error) = std::fs::write(marker, payload) {
            eprintln!(
                "payment-recovery crash marker write failed {}: {error}",
                marker.display()
            );
            std::process::abort();
        }

        std::future::pending::<()>().await;
        unreachable!("armed crash point must be externally killed");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crash_control_requires_complete_target() {
        assert!(CrashControl::new(None, None, None).is_ok());
        assert!(CrashControl::new(
            Some(CrashPoint::AfterCandidateReadBeforeReplay),
            None,
            Some(PathBuf::from("/tmp/marker"))
        )
        .is_err());
        assert!(CrashControl::new(
            Some(CrashPoint::AfterCandidateReadBeforeReplay),
            Some("candidate-1".into()),
            Some(PathBuf::from("/tmp/marker"))
        )
        .is_ok());
    }
}
