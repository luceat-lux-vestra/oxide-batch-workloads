-- Business schema for this workload. Deliberately separate from the
-- `oxide_batch` schema that PostgresMigrator::migrate owns: this workload
-- never creates objects in `oxide_batch`, and OxideBatch's migrator never
-- touches this schema. Ownership boundary is enforced by using distinct
-- schemas, not by convention alone.
CREATE SCHEMA IF NOT EXISTS app_business;

-- The business key (customer_id) carries a real PostgreSQL PRIMARY KEY
-- constraint: duplicate-key correctness is judged by the database, never by
-- application code alone (see validation spec ss12).
CREATE TABLE IF NOT EXISTS app_business.imported_customer (
    customer_id BIGINT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    amount      BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);
