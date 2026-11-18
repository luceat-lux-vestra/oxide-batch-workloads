CREATE TABLE IF NOT EXISTS payment_reference_merchant.settlement_close (
    statement_id TEXT PRIMARY KEY
        CHECK (statement_id ~ '^statement-[0-9a-f]{64}$'),
    statement_digest TEXT NOT NULL
        CHECK (statement_digest ~ '^sha256:[0-9a-f]{64}$'),
    provider_account_id TEXT NOT NULL
        CHECK (btrim(provider_account_id) <> ''),
    settlement_date DATE NOT NULL,
    currency TEXT NOT NULL
        CHECK (currency ~ '^[A-Z]{3}$'),
    line_count BIGINT NOT NULL
        CHECK (line_count > 0),
    captured_total_minor BIGINT NOT NULL
        CHECK (captured_total_minor >= 0),
    refunded_total_minor BIGINT NOT NULL
        CHECK (
            refunded_total_minor >= 0
            AND refunded_total_minor <= captured_total_minor
        ),
    net_settlement_total_minor BIGINT NOT NULL
        CHECK (
            net_settlement_total_minor >= 0
            AND net_settlement_total_minor
                = captured_total_minor - refunded_total_minor
        ),
    settlement_state TEXT NOT NULL
        CHECK (settlement_state IN ('HELD', 'CLOSED')),
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT settlement_close_identity_unique
        UNIQUE (provider_account_id, settlement_date, currency),
    CONSTRAINT settlement_close_state_timestamp_check
        CHECK (
            (settlement_state = 'HELD' AND closed_at IS NULL)
            OR (settlement_state = 'CLOSED' AND closed_at IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS settlement_close_date_idx
    ON payment_reference_merchant.settlement_close
        (settlement_date, provider_account_id, currency);
