CREATE SCHEMA IF NOT EXISTS payment_reference_gateway;

CREATE TABLE IF NOT EXISTS payment_reference_gateway.gateway_operation (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    merchant_id TEXT NOT NULL,
    merchant_order_id TEXT NOT NULL,
    merchant_payment_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    payment_method_token TEXT,
    requested_at TEXT,
    payment_state TEXT NOT NULL DEFAULT 'PAYMENT_PENDING',
    operation_state TEXT NOT NULL DEFAULT 'PENDING',
    killbill_payment_id TEXT,
    killbill_transaction_id TEXT,
    hyperswitch_payment_id TEXT,
    hyperswitch_attempt_id TEXT,
    provider_payment_id TEXT,
    provider_operation_id TEXT,
    provider_idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS gateway_operation_payment_idx
    ON payment_reference_gateway.gateway_operation (merchant_payment_id, created_at DESC);
