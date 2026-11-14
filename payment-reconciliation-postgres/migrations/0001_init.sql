CREATE SCHEMA IF NOT EXISTS payment_reconciliation;

CREATE TABLE IF NOT EXISTS payment_reconciliation.source_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL
        CHECK (contract_version = 'payment-reconciliation-v1'),
    business_date DATE NOT NULL,
    provider_scope TEXT NOT NULL,
    scenario_case TEXT NOT NULL,
    base_snapshot_id TEXT,
    internal_snapshot_digest CHAR(64) NOT NULL,
    provider_snapshot_digest CHAR(64) NOT NULL,
    policy_version TEXT NOT NULL,
    mutation_recipe JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reconciliation.internal_payment_snapshot (
    snapshot_id TEXT NOT NULL
        REFERENCES payment_reconciliation.source_snapshot(snapshot_id) ON DELETE CASCADE,
    source_row_id TEXT NOT NULL,
    merchant_payment_id TEXT NOT NULL,
    provider_payment_id TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    normalized_status TEXT NOT NULL,
    authorized_amount_minor BIGINT NOT NULL CHECK (authorized_amount_minor >= 0),
    captured_amount_minor BIGINT NOT NULL CHECK (captured_amount_minor >= 0),
    refunded_amount_minor BIGINT NOT NULL CHECK (refunded_amount_minor >= 0),
    PRIMARY KEY (snapshot_id, source_row_id)
);

CREATE INDEX IF NOT EXISTS internal_payment_snapshot_match_idx
    ON payment_reconciliation.internal_payment_snapshot
        (snapshot_id, provider_payment_id, source_row_id);

CREATE TABLE IF NOT EXISTS payment_reconciliation.provider_payment_snapshot (
    snapshot_id TEXT NOT NULL
        REFERENCES payment_reconciliation.source_snapshot(snapshot_id) ON DELETE CASCADE,
    source_row_id TEXT NOT NULL,
    provider_payment_id TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    normalized_status TEXT NOT NULL,
    authorized_amount_minor BIGINT NOT NULL CHECK (authorized_amount_minor >= 0),
    captured_amount_minor BIGINT NOT NULL CHECK (captured_amount_minor >= 0),
    refunded_amount_minor BIGINT NOT NULL CHECK (refunded_amount_minor >= 0),
    provider_version BIGINT NOT NULL CHECK (provider_version >= 0),
    PRIMARY KEY (snapshot_id, source_row_id)
);

CREATE INDEX IF NOT EXISTS provider_payment_snapshot_match_idx
    ON payment_reconciliation.provider_payment_snapshot
        (snapshot_id, provider_payment_id, source_row_id);

CREATE TABLE IF NOT EXISTS payment_reconciliation.reconciliation_run (
    run_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL
        REFERENCES payment_reconciliation.source_snapshot(snapshot_id),
    processing_state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (processing_state IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED')),
    business_state TEXT
        CHECK (business_state IS NULL OR business_state IN ('CLEAR', 'HELD')),
    mismatch_count BIGINT,
    aggregate_delta_minor BIGINT,
    result_digest CHAR(64),
    finalized_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reconciliation.reconciliation_detail (
    run_id TEXT NOT NULL
        REFERENCES payment_reconciliation.reconciliation_run(run_id) ON DELETE CASCADE,
    provider_payment_id TEXT NOT NULL,
    classification TEXT NOT NULL
        CHECK (
            classification IN (
                'MATCH',
                'MISSING_PROVIDER',
                'MISSING_INTERNAL',
                'DUPLICATE_INTERNAL',
                'DUPLICATE_PROVIDER',
                'AMOUNT_MISMATCH',
                'ATTRIBUTE_MISMATCH'
            )
        ),
    internal_row_count INTEGER NOT NULL CHECK (internal_row_count >= 0),
    provider_row_count INTEGER NOT NULL CHECK (provider_row_count >= 0),
    internal_currency TEXT,
    provider_currency TEXT,
    internal_status TEXT,
    provider_status TEXT,
    authorized_delta_minor BIGINT,
    captured_delta_minor BIGINT,
    refunded_delta_minor BIGINT,
    PRIMARY KEY (run_id, provider_payment_id)
);

CREATE TABLE IF NOT EXISTS payment_reconciliation.published_reconciliation (
    run_id TEXT PRIMARY KEY
        REFERENCES payment_reconciliation.reconciliation_run(run_id),
    result_digest CHAR(64) NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
