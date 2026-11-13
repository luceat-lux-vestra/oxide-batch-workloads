//! Pure deterministic fault-plan model for payment-reference scenarios.
//!
//! This crate owns only the versioned plan contract and logical scheduler. It
//! deliberately performs no I/O and has no provider, Gateway, Merchant, status,
//! or webhook integration.

use std::collections::HashSet;
use std::fmt;

use serde::{Deserialize, Serialize};

pub const FAULT_PLAN_VERSION: u32 = 1;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FaultPlan {
    pub fault_plan_version: u32,
    pub scenario_version: u32,
    pub seed: u64,
    pub logical_business_time: String,
    pub scenario_case: String,
    pub expected_provider_truth: String,
    pub expected_canonical_outcome: String,
    pub steps: Vec<FaultStep>,
}

impl FaultPlan {
    pub fn validate(&self) -> Result<(), FaultPlanError> {
        if self.fault_plan_version != FAULT_PLAN_VERSION {
            return Err(FaultPlanError::UnsupportedVersion(
                self.fault_plan_version,
            ));
        }
        if self.scenario_version == 0 {
            return Err(FaultPlanError::ZeroScenarioVersion);
        }
        require_non_blank("logical_business_time", &self.logical_business_time)?;
        require_non_blank("scenario_case", &self.scenario_case)?;
        require_non_blank("expected_provider_truth", &self.expected_provider_truth)?;
        require_non_blank(
            "expected_canonical_outcome",
            &self.expected_canonical_outcome,
        )?;
        if self.steps.is_empty() {
            return Err(FaultPlanError::EmptySteps);
        }

        let mut step_ids = HashSet::new();
        let mut sequences = HashSet::new();
        for step in &self.steps {
            step.validate()?;
            if !step_ids.insert(step.step_id.as_str()) {
                return Err(FaultPlanError::DuplicateStepId(step.step_id.clone()));
            }
            if !sequences.insert(step.sequence) {
                return Err(FaultPlanError::DuplicateSequence(step.sequence));
            }
        }

        Ok(())
    }

    pub fn scheduler(&self) -> Result<DeterministicScheduler, FaultPlanError> {
        DeterministicScheduler::from_plan(self)
    }

    pub fn identity(&self) -> FaultPlanIdentity {
        FaultPlanIdentity {
            fault_plan_version: self.fault_plan_version,
            scenario_version: self.scenario_version,
            seed: self.seed,
            logical_business_time: self.logical_business_time.clone(),
            scenario_case: self.scenario_case.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FaultPlanIdentity {
    pub fault_plan_version: u32,
    pub scenario_version: u32,
    pub seed: u64,
    pub logical_business_time: String,
    pub scenario_case: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FaultStep {
    pub step_id: String,
    pub sequence: u32,
    pub offset_ms: u64,
    pub point: FaultPoint,
    pub request_match: Option<RequestMatch>,
    pub action: FaultAction,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RequestMatch {
    pub method: String,
    pub path_suffix: String,
}

impl RequestMatch {
    fn validate(&self) -> Result<(), FaultPlanError> {
        require_non_blank("request_match.method", &self.method)?;
        require_non_blank("request_match.path_suffix", &self.path_suffix)?;
        if !self.method.bytes().all(|byte| byte.is_ascii_uppercase()) {
            return Err(FaultPlanError::InvalidRequestMethod(self.method.clone()));
        }
        if !self.path_suffix.starts_with('/') {
            return Err(FaultPlanError::InvalidRequestPath(
                self.path_suffix.clone(),
            ));
        }
        Ok(())
    }
}

impl FaultStep {
    fn validate(&self) -> Result<(), FaultPlanError> {
        require_non_blank("step_id", &self.step_id)?;

        let request_point = matches!(
            self.point,
            FaultPoint::RequestBeforeProviderSideEffect | FaultPoint::RequestAfterProviderCommit
        );
        match (request_point, self.request_match.as_ref()) {
            (true, Some(request_match)) => request_match.validate()?,
            (true, None) => {
                return Err(FaultPlanError::MissingRequestMatch(self.step_id.clone()));
            }
            (false, Some(_)) => {
                return Err(FaultPlanError::UnexpectedRequestMatch(self.step_id.clone()));
            }
            (false, None) => {}
        }

        let compatible = matches!(
            (&self.point, &self.action),
            (
                FaultPoint::RequestBeforeProviderSideEffect,
                FaultAction::TransportFailure { .. }
                    | FaultAction::HttpFailure { .. }
                    | FaultAction::PermanentBusinessFailure { .. }
            ) | (
                FaultPoint::RequestAfterProviderCommit,
                FaultAction::CommitThenDropResponse
            ) | (
                FaultPoint::StatusInquiry,
                FaultAction::StatusVisibility { .. }
            ) | (
                FaultPoint::ProviderTransition,
                FaultAction::ProviderTransition { .. }
            ) | (
                FaultPoint::WebhookDelivery,
                FaultAction::Webhook { .. }
            )
        );

        if !compatible {
            return Err(FaultPlanError::IncompatibleAction {
                step_id: self.step_id.clone(),
                point: self.point,
                action_kind: self.action.kind(),
            });
        }

        match &self.action {
            FaultAction::HttpFailure { status } => {
                if !matches!(status, 429 | 502 | 503 | 504) {
                    return Err(FaultPlanError::UnsupportedHttpStatus(*status));
                }
            }
            FaultAction::PermanentBusinessFailure { status, code } => {
                if !(400..=499).contains(status) || *status == 429 {
                    return Err(FaultPlanError::UnsupportedBusinessFailureStatus(*status));
                }
                require_non_blank("business_failure.code", code)?;
            }
            FaultAction::Webhook {
                event_id, attempt, ..
            } => {
                require_non_blank("webhook.event_id", event_id)?;
                if *attempt == 0 {
                    return Err(FaultPlanError::ZeroWebhookAttempt);
                }
            }
            FaultAction::TransportFailure { .. }
            | FaultAction::CommitThenDropResponse
            | FaultAction::StatusVisibility { .. }
            | FaultAction::ProviderTransition { .. } => {}
        }

        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum FaultPoint {
    RequestBeforeProviderSideEffect,
    RequestAfterProviderCommit,
    StatusInquiry,
    ProviderTransition,
    WebhookDelivery,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum FaultAction {
    TransportFailure {
        class: TransportFailureClass,
    },
    HttpFailure {
        status: u16,
    },
    PermanentBusinessFailure {
        status: u16,
        code: String,
    },
    CommitThenDropResponse,
    StatusVisibility {
        visibility: StatusVisibility,
    },
    ProviderTransition {
        outcome: AsyncOutcome,
    },
    Webhook {
        delivery: WebhookDelivery,
        event_id: String,
        attempt: u32,
    },
}

impl FaultAction {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::TransportFailure { .. } => "TRANSPORT_FAILURE",
            Self::HttpFailure { .. } => "HTTP_FAILURE",
            Self::PermanentBusinessFailure { .. } => "PERMANENT_BUSINESS_FAILURE",
            Self::CommitThenDropResponse => "COMMIT_THEN_DROP_RESPONSE",
            Self::StatusVisibility { .. } => "STATUS_VISIBILITY",
            Self::ProviderTransition { .. } => "PROVIDER_TRANSITION",
            Self::Webhook { .. } => "WEBHOOK",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TransportFailureClass {
    Connect,
    Request,
    Timeout,
    ConnectionDrop,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum StatusVisibility {
    Immediate,
    NotFound,
    Stale,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AsyncOutcome {
    Succeeded,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum WebhookDelivery {
    Deliver,
    Drop,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeterministicScheduler {
    identity: FaultPlanIdentity,
    steps: Vec<FaultStep>,
    cursor: usize,
    now_ms: u64,
}

impl DeterministicScheduler {
    pub fn from_plan(plan: &FaultPlan) -> Result<Self, FaultPlanError> {
        plan.validate()?;

        let mut steps = plan.steps.clone();
        steps.sort_by_key(|step| (step.offset_ms, step.sequence));

        Ok(Self {
            identity: plan.identity(),
            steps,
            cursor: 0,
            now_ms: 0,
        })
    }

    pub fn identity(&self) -> &FaultPlanIdentity {
        &self.identity
    }

    pub fn now_ms(&self) -> u64 {
        self.now_ms
    }

    pub fn remaining(&self) -> usize {
        self.steps.len().saturating_sub(self.cursor)
    }

    pub fn next_due_at(&self) -> Option<u64> {
        self.steps.get(self.cursor).map(|step| step.offset_ms)
    }

    pub fn advance_to(&mut self, logical_offset_ms: u64) -> Result<Vec<FaultStep>, FaultPlanError> {
        if logical_offset_ms < self.now_ms {
            return Err(FaultPlanError::BackwardTime {
                current_ms: self.now_ms,
                requested_ms: logical_offset_ms,
            });
        }

        let start = self.cursor;
        while self
            .steps
            .get(self.cursor)
            .is_some_and(|step| step.offset_ms <= logical_offset_ms)
        {
            self.cursor += 1;
        }
        self.now_ms = logical_offset_ms;

        Ok(self.steps[start..self.cursor].to_vec())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FaultPlanError {
    UnsupportedVersion(u32),
    ZeroScenarioVersion,
    BlankField(&'static str),
    EmptySteps,
    DuplicateStepId(String),
    DuplicateSequence(u32),
    MissingRequestMatch(String),
    UnexpectedRequestMatch(String),
    InvalidRequestMethod(String),
    InvalidRequestPath(String),
    IncompatibleAction {
        step_id: String,
        point: FaultPoint,
        action_kind: &'static str,
    },
    UnsupportedHttpStatus(u16),
    UnsupportedBusinessFailureStatus(u16),
    ZeroWebhookAttempt,
    BackwardTime {
        current_ms: u64,
        requested_ms: u64,
    },
}

impl fmt::Display for FaultPlanError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedVersion(version) => {
                write!(f, "unsupported fault_plan_version: {version}")
            }
            Self::ZeroScenarioVersion => write!(f, "scenario_version must be greater than zero"),
            Self::BlankField(field) => write!(f, "{field} must not be blank"),
            Self::EmptySteps => write!(f, "fault plan must contain at least one step"),
            Self::DuplicateStepId(step_id) => write!(f, "duplicate step_id: {step_id}"),
            Self::DuplicateSequence(sequence) => {
                write!(f, "duplicate step sequence: {sequence}")
            }
            Self::MissingRequestMatch(step_id) => {
                write!(f, "request fault step {step_id} requires request_match")
            }
            Self::UnexpectedRequestMatch(step_id) => {
                write!(f, "non-request fault step {step_id} must not define request_match")
            }
            Self::InvalidRequestMethod(method) => {
                write!(f, "request_match.method must be uppercase ASCII: {method:?}")
            }
            Self::InvalidRequestPath(path) => {
                write!(f, "request_match.path_suffix must begin with '/': {path:?}")
            }
            Self::IncompatibleAction {
                step_id,
                point,
                action_kind,
            } => write!(
                f,
                "fault action {action_kind} is incompatible with point {point:?} for step {step_id}"
            ),
            Self::UnsupportedHttpStatus(status) => {
                write!(f, "unsupported injected HTTP status: {status}")
            }
            Self::UnsupportedBusinessFailureStatus(status) => {
                write!(f, "unsupported permanent business failure HTTP status: {status}")
            }
            Self::ZeroWebhookAttempt => write!(f, "webhook attempt must be greater than zero"),
            Self::BackwardTime {
                current_ms,
                requested_ms,
            } => write!(
                f,
                "logical time cannot move backward: current={current_ms} requested={requested_ms}"
            ),
        }
    }
}

impl std::error::Error for FaultPlanError {}

fn require_non_blank(field: &'static str, value: &str) -> Result<(), FaultPlanError> {
    if value.trim().is_empty() {
        return Err(FaultPlanError::BlankField(field));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan(steps: Vec<FaultStep>) -> FaultPlan {
        FaultPlan {
            fault_plan_version: FAULT_PLAN_VERSION,
            scenario_version: 1,
            seed: 42,
            logical_business_time: "2026-11-14T00:00:00Z".into(),
            scenario_case: "deterministic-scheduler".into(),
            expected_provider_truth: "AUTHORIZED".into(),
            expected_canonical_outcome: "PAYMENT_UNKNOWN/UNKNOWN".into(),
            steps,
        }
    }

    fn step(
        step_id: &str,
        sequence: u32,
        offset_ms: u64,
        point: FaultPoint,
        action: FaultAction,
    ) -> FaultStep {
        let request_match = matches!(
            point,
            FaultPoint::RequestBeforeProviderSideEffect | FaultPoint::RequestAfterProviderCommit
        )
        .then(|| RequestMatch {
            method: "POST".into(),
            path_suffix: "/payments".into(),
        });

        FaultStep {
            step_id: step_id.into(),
            sequence,
            offset_ms,
            point,
            request_match,
            action,
        }
    }

    #[test]
    fn scheduler_orders_by_logical_offset_then_explicit_sequence() {
        let plan = plan(vec![
            step(
                "third",
                30,
                200,
                FaultPoint::StatusInquiry,
                FaultAction::StatusVisibility {
                    visibility: StatusVisibility::Immediate,
                },
            ),
            step(
                "second",
                20,
                100,
                FaultPoint::RequestBeforeProviderSideEffect,
                FaultAction::HttpFailure { status: 503 },
            ),
            step(
                "first",
                10,
                100,
                FaultPoint::RequestBeforeProviderSideEffect,
                FaultAction::TransportFailure {
                    class: TransportFailureClass::Connect,
                },
            ),
        ]);

        let mut scheduler = plan.scheduler().unwrap();
        let due = scheduler.advance_to(100).unwrap();

        assert_eq!(
            due.iter().map(|step| step.step_id.as_str()).collect::<Vec<_>>(),
            vec!["first", "second"]
        );
        assert_eq!(scheduler.next_due_at(), Some(200));
        assert_eq!(scheduler.remaining(), 1);
    }

    #[test]
    fn replay_of_same_plan_has_identical_identity_and_schedule() {
        let plan = plan(vec![
            step(
                "commit-drop",
                1,
                0,
                FaultPoint::RequestAfterProviderCommit,
                FaultAction::CommitThenDropResponse,
            ),
            step(
                "event",
                2,
                500,
                FaultPoint::WebhookDelivery,
                FaultAction::Webhook {
                    delivery: WebhookDelivery::Deliver,
                    event_id: "event-1".into(),
                    attempt: 1,
                },
            ),
        ]);

        let mut first = plan.scheduler().unwrap();
        let mut replay = plan.scheduler().unwrap();

        assert_eq!(first.identity(), replay.identity());
        assert_eq!(
            first.advance_to(u64::MAX).unwrap(),
            replay.advance_to(u64::MAX).unwrap()
        );
    }

    #[test]
    fn scheduler_rejects_backward_logical_time() {
        let plan = plan(vec![step(
            "status",
            1,
            100,
            FaultPoint::StatusInquiry,
            FaultAction::StatusVisibility {
                visibility: StatusVisibility::Stale,
            },
        )]);

        let mut scheduler = plan.scheduler().unwrap();
        scheduler.advance_to(100).unwrap();

        assert_eq!(
            scheduler.advance_to(99),
            Err(FaultPlanError::BackwardTime {
                current_ms: 100,
                requested_ms: 99,
            })
        );
    }

    #[test]
    fn validation_rejects_point_action_mismatch() {
        let plan = plan(vec![step(
            "bad",
            1,
            0,
            FaultPoint::StatusInquiry,
            FaultAction::HttpFailure { status: 503 },
        )]);

        assert!(matches!(
            plan.validate(),
            Err(FaultPlanError::IncompatibleAction { .. })
        ));
    }

    #[test]
    fn validation_rejects_duplicate_step_identity_and_sequence() {
        let duplicate_id = plan(vec![
            step(
                "same",
                1,
                0,
                FaultPoint::ProviderTransition,
                FaultAction::ProviderTransition {
                    outcome: AsyncOutcome::Succeeded,
                },
            ),
            step(
                "same",
                2,
                1,
                FaultPoint::ProviderTransition,
                FaultAction::ProviderTransition {
                    outcome: AsyncOutcome::Failed,
                },
            ),
        ]);
        assert_eq!(
            duplicate_id.validate(),
            Err(FaultPlanError::DuplicateStepId("same".into()))
        );

        let duplicate_sequence = plan(vec![
            step(
                "one",
                1,
                0,
                FaultPoint::ProviderTransition,
                FaultAction::ProviderTransition {
                    outcome: AsyncOutcome::Succeeded,
                },
            ),
            step(
                "two",
                1,
                1,
                FaultPoint::ProviderTransition,
                FaultAction::ProviderTransition {
                    outcome: AsyncOutcome::Failed,
                },
            ),
        ]);
        assert_eq!(
            duplicate_sequence.validate(),
            Err(FaultPlanError::DuplicateSequence(1))
        );
    }

    #[test]
    fn validation_rejects_unsupported_http_status_and_zero_webhook_attempt() {
        let bad_status = plan(vec![step(
            "bad-status",
            1,
            0,
            FaultPoint::RequestBeforeProviderSideEffect,
            FaultAction::HttpFailure { status: 500 },
        )]);
        assert_eq!(
            bad_status.validate(),
            Err(FaultPlanError::UnsupportedHttpStatus(500))
        );

        let bad_attempt = plan(vec![step(
            "bad-attempt",
            1,
            0,
            FaultPoint::WebhookDelivery,
            FaultAction::Webhook {
                delivery: WebhookDelivery::Deliver,
                event_id: "event-1".into(),
                attempt: 0,
            },
        )]);
        assert_eq!(
            bad_attempt.validate(),
            Err(FaultPlanError::ZeroWebhookAttempt)
        );
    }
}
