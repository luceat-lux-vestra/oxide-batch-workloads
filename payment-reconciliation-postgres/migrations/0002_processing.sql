ALTER TABLE payment_reconciliation.reconciliation_detail
    ALTER COLUMN internal_row_count TYPE BIGINT,
    ALTER COLUMN provider_row_count TYPE BIGINT;

CREATE INDEX IF NOT EXISTS reconciliation_run_snapshot_idx
    ON payment_reconciliation.reconciliation_run (snapshot_id);

CREATE INDEX IF NOT EXISTS reconciliation_detail_run_classification_idx
    ON payment_reconciliation.reconciliation_detail (run_id, classification);
