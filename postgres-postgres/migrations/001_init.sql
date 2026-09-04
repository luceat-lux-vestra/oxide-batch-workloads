-- Workload-owned schemas for postgres-postgres. Deliberately separate from
-- the `oxide_batch` schema that PostgresMigrator::migrate owns: this
-- workload never creates objects in `oxide_batch`, and OxideBatch's own
-- migrator never touches either schema below. The boundary is enforced by
-- using distinct schemas, not by convention alone.
CREATE SCHEMA IF NOT EXISTS app_source;
CREATE SCHEMA IF NOT EXISTS app_business;

-- Deterministic source table. `customer_id` is the strict unique ordering
-- key both the cursor and paging readers key off of. Types are limited to
-- what `oxide_batch::item_components::PostgresRow` can decode (text/i64/i32/
-- bool/f64) so both readers' `map_row` can read every column directly, with
-- no base_query cast workaround required.
CREATE TABLE IF NOT EXISTS app_source.source_customer (
    customer_id   BIGINT NOT NULL PRIMARY KEY,
    full_name     TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL,
    balance_cents BIGINT NOT NULL
);

-- Deterministic transformed projection, not a byte-for-byte copy of
-- app_source.source_customer (see src/processor.rs). Destination rows are
-- scoped by (import_name, source_digest) so two different source
-- identities can never collide on the same customer_id: the primary key
-- includes both identity columns, not customer_id alone.
CREATE TABLE IF NOT EXISTS app_business.customer_projection (
    import_name    TEXT NOT NULL,
    source_digest  TEXT NOT NULL,
    customer_id    BIGINT NOT NULL,
    display_name   TEXT NOT NULL,
    loyalty_score  BIGINT NOT NULL,
    is_premium     BOOLEAN NOT NULL,
    row_fingerprint BYTEA NOT NULL,
    PRIMARY KEY (import_name, source_digest, customer_id)
);
