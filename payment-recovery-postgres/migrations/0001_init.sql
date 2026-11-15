CREATE SCHEMA IF NOT EXISTS payment_recovery_workload;

CREATE TABLE IF NOT EXISTS payment_recovery_workload.payment_recovery_run (
    run_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL,
    oxide_batch_version TEXT NOT NULL,
    business_date DATE NOT NULL,
    eligibility_cutoff TIMESTAMPTZ NOT NULL,
    scenario_set_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    run_state TEXT NOT NULL DEFAULT 'SNAPSHOTTED'
        CHECK (run_state IN ('SNAPSHOTTED', 'PROCESSING', 'PROCESSED', 'RECOVERING', 'VERIFIED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        contract_version,
        oxide_batch_version,
        business_date,
        eligibility_cutoff,
        scenario_set_id,
        policy_version
    )
);

CREATE TABLE IF NOT EXISTS payment_recovery_workload.payment_recovery_candidate (
    run_id TEXT NOT NULL
        REFERENCES payment_recovery_workload.payment_recovery_run(run_id)
        ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    merchant_payment_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL
        CHECK (operation_kind IN ('AUTHORIZE', 'AUTH_CAPTURE')),
    payment_state TEXT NOT NULL
        CHECK (payment_state IN ('PAYMENT_UNKNOWN', 'PAYMENT_PENDING')),
    operation_state TEXT NOT NULL
        CHECK (operation_state IN ('UNKNOWN', 'RETRY_PENDING', 'MANUAL_REVIEW')),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency TEXT NOT NULL CHECK (char_length(currency) = 3),
    idempotency_key TEXT NOT NULL,
    idempotency_key_digest TEXT NOT NULL,
    payment_method_token TEXT,
    provider_payment_id TEXT,
    gateway_operation_id TEXT,
    source_updated_at TIMESTAMPTZ NOT NULL,
    snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, candidate_id),
    UNIQUE (run_id, merchant_payment_id, operation_id)
);

COMMENT ON COLUMN payment_recovery_workload.payment_recovery_candidate.idempotency_key
    IS 'Sensitive replay input; never emit raw value into retained evidence or logs';
COMMENT ON COLUMN payment_recovery_workload.payment_recovery_candidate.payment_method_token
    IS 'Synthetic reference-system replay input; never emit raw value into retained evidence or logs';

CREATE TABLE IF NOT EXISTS payment_recovery_workload.payment_recovery_attempt (
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    attempt_state TEXT NOT NULL
        CHECK (attempt_state IN ('STARTED', 'CALL_UNKNOWN', 'OBSERVED', 'COMMITTED')),
    canonical_payment_state TEXT,
    canonical_operation_state TEXT,
    provider_status TEXT,
    provider_side_effect_count_before INTEGER
        CHECK (provider_side_effect_count_before IS NULL OR provider_side_effect_count_before >= 0),
    provider_side_effect_count_after INTEGER
        CHECK (provider_side_effect_count_after IS NULL OR provider_side_effect_count_after >= 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, candidate_id, attempt_no),
    FOREIGN KEY (run_id, candidate_id)
        REFERENCES payment_recovery_workload.payment_recovery_candidate(run_id, candidate_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS payment_recovery_workload.payment_recovery_result (
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    classification TEXT NOT NULL
        CHECK (
            classification IN (
                'RESOLVED_SUCCEEDED',
                'RESOLVED_FAILED',
                'RETRY_LATER',
                'HOLD_MANUAL',
                'NOT_ELIGIBLE'
            )
        ),
    duplicate_provider_mutation_count INTEGER NOT NULL DEFAULT 0
        CHECK (duplicate_provider_mutation_count >= 0),
    final_canonical_payment_state TEXT,
    final_canonical_operation_state TEXT,
    final_provider_status TEXT,
    result_digest TEXT NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, candidate_id),
    FOREIGN KEY (run_id, candidate_id)
        REFERENCES payment_recovery_workload.payment_recovery_candidate(run_id, candidate_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS payment_recovery_candidate_state_idx
    ON payment_recovery_workload.payment_recovery_candidate
        (run_id, operation_state, source_updated_at, candidate_id);

CREATE INDEX IF NOT EXISTS payment_recovery_attempt_candidate_idx
    ON payment_recovery_workload.payment_recovery_attempt
        (run_id, candidate_id, attempt_no);
