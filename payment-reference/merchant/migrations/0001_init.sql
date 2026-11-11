CREATE SCHEMA IF NOT EXISTS payment_reference_merchant;

CREATE TABLE IF NOT EXISTS payment_reference_merchant.merchant (
    merchant_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reference_merchant.customer (
    customer_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reference_merchant.orders (
    merchant_order_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES payment_reference_merchant.merchant(merchant_id),
    customer_id TEXT NOT NULL REFERENCES payment_reference_merchant.customer(customer_id),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    order_state TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (order_state IN ('CREATED', 'PAYMENT_PENDING', 'PAID', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reference_merchant.merchant_payment (
    merchant_payment_id TEXT PRIMARY KEY,
    merchant_order_id TEXT NOT NULL REFERENCES payment_reference_merchant.orders(merchant_order_id),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    payment_method_token TEXT NOT NULL,
    payment_state TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (
            payment_state IN (
                'CREATED',
                'PAYMENT_PENDING',
                'AUTHORIZED',
                'PARTIALLY_CAPTURED',
                'PAID',
                'CANCELLED',
                'PARTIALLY_REFUNDED',
                'REFUNDED',
                'PAYMENT_FAILED',
                'PAYMENT_UNKNOWN'
            )
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reference_merchant.merchant_payment_operation (
    operation_id TEXT PRIMARY KEY,
    merchant_payment_id TEXT NOT NULL
        REFERENCES payment_reference_merchant.merchant_payment(merchant_payment_id),
    operation_kind TEXT NOT NULL,
    operation_state TEXT NOT NULL
        CHECK (
            operation_state IN (
                'PENDING',
                'SUCCEEDED',
                'FAILED',
                'UNKNOWN',
                'RETRY_PENDING',
                'MANUAL_REVIEW'
            )
        ),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reference_merchant.refund_request (
    refund_request_id TEXT PRIMARY KEY,
    merchant_payment_id TEXT NOT NULL
        REFERENCES payment_reference_merchant.merchant_payment(merchant_payment_id),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    request_state TEXT NOT NULL DEFAULT 'CREATED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reference_merchant.external_payment_mapping (
    merchant_payment_id TEXT PRIMARY KEY
        REFERENCES payment_reference_merchant.merchant_payment(merchant_payment_id),
    gateway_operation_id TEXT,
    killbill_payment_id TEXT,
    killbill_transaction_id TEXT,
    hyperswitch_payment_id TEXT,
    hyperswitch_attempt_id TEXT,
    provider_payment_id TEXT,
    provider_operation_id TEXT,
    provider_idempotency_key TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reference_merchant.business_event (
    event_id BIGSERIAL PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS merchant_payment_order_idx
    ON payment_reference_merchant.merchant_payment (merchant_order_id);

CREATE INDEX IF NOT EXISTS merchant_payment_operation_payment_idx
    ON payment_reference_merchant.merchant_payment_operation (merchant_payment_id, created_at);
