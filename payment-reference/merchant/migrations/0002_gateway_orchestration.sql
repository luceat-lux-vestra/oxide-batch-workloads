ALTER TABLE payment_reference_merchant.merchant_payment_operation
    ADD COLUMN IF NOT EXISTS amount_minor BIGINT
        CHECK (amount_minor IS NULL OR amount_minor > 0);
