# payment-reconciliation-postgres

Issue owner: #148.

This top-level Cargo project is the first real OxideBatch business workload built
against the payment-reference system. It is classified as milestone-null
supplemental validation of the exact published OxideBatch 0.6.0 artifact.

## Validation subject

```text
oxide-batch = { version = "=0.6.0", features = ["postgres"] }
rust-version = "1.95"
```

The committed lockfile is derived from the repository's already validated
`postgres-local-partition` dependency graph. Checkpoint 3 changes only feature
activation on already-resolved dependencies; it does not introduce a new direct
package.

## Checkpoint 3 boundary

Implemented:

- workload migration execution;
- real Merchant PostgreSQL snapshot acquisition under a repeatable-read,
  read-only transaction;
- v1 scope filtering by Reference Merchant `merchant_id` and Merchant
  `created_at` projected to a UTC business date;
- stable v1 Merchant states only: `AUTHORIZED`, `PAID`, `REFUNDED`;
- internal financial totals derived only from `SUCCEEDED` Merchant operations;
- provider acquisition only through payment-sandbox's supported
  `GET /payments/{provider_payment_id}` response;
- fail-closed base acquisition: the independently derived oracle must report
  `CLEAR` before a base snapshot is persisted;
- canonical source-row digests;
- deterministic derived scenarios:
  - `missing-provider`
  - `missing-internal`
  - `duplicate-provider`
  - `amount-mismatch`
- source-only independent oracle covering all seven frozen classifications;
- stored-digest recomputation and scenario expectation checking in `verify`.

Still intentionally unimplemented:

- OxideBatch reconciliation processing;
- CLEAR/HELD finalization/publication;
- crash/recovery;
- reset behavior;
- retained Evidence Contract v2 artifacts.

## Source boundary

The batch workload never reads Kill Bill internals or payment-sandbox storage.

```text
Reference Merchant PostgreSQL
        |
        | repeatable-read source projection
        v
internal_payment_snapshot

external_payment_mapping.provider_payment_id
        |
        | supported HTTP GET
        v
payment-sandbox /payments/{id}
        |
        v
provider_payment_snapshot
```

The two representations are frozen before any later OxideBatch job begins.
Recovery must reuse the recorded snapshot digests rather than silently refresh
external state.

Provider HTTP acquisition currently shells out directly to `curl` without a
shell, with a bounded connect/overall timeout. The intended source is the
loopback-only direct payment-sandbox port (`PAYMENT_SANDBOX_DIRECT_PORT`, default
`18084`), never the fault-proxy endpoint. This matches the existing
payment-reference runtime dependency and avoids introducing a second HTTP client
dependency into the validation subject.

## Commands implemented in checkpoint 3

```text
payment-reconciliation-postgres migrate --database-url <url>

payment-reconciliation-postgres snapshot \
  --database-url <workload-db> \
  --merchant-database-url <merchant-db> \
  --provider-base-url <payment-sandbox-or-fault-proxy-base> \
  --snapshot-id <id> \
  --business-date YYYY-MM-DD \
  --provider-scope <merchant-id>

payment-reconciliation-postgres derive-scenario \
  --database-url <workload-db> \
  --base-snapshot-id <clear-id> \
  --snapshot-id <derived-id> \
  --scenario missing-provider|missing-internal|duplicate-provider|amount-mismatch \
  [--delta-minor 1]

payment-reconciliation-postgres verify \
  --database-url <workload-db> \
  --snapshot-id <id>
```

`verify` is deliberately source-only at this checkpoint. It independently
recomputes snapshot digests and the expected `CLEAR | HELD` verdict; it does
not yet validate production reconciliation/finalization rows.

The migration owns only the `payment_reconciliation` schema. It does not
modify payment-reference source schemas or OxideBatch framework metadata.


## Checkpoint 4 — OxideBatch processing and publication boundary

Implemented in checkpoint 4:

- released `postgres_paging_reader` over the frozen snapshot tables, ordered by
  `provider_payment_id`;
- SQL aggregation only for per-side row counts and representative fields;
  business classification remains processor-owned;
- identifying OxideBatch job parameters:
  `contract_version`, `business_date`, `provider_scope`, `scenario_case`,
  both snapshot digests, and `policy_version`;
- deterministic run identity derived from exactly those identifying values;
- processor-owned seven-way classification;
- released `postgres_batch_writer` under
  `ChunkDeliveryMode::AtomicSameResource`;
- idempotent detail upsert keyed by `(run_id, provider_payment_id)`;
- separate finalization transaction that independently derives mismatch count,
  aggregate financial delta, `CLEAR | HELD`, and a canonical result digest;
- publication invariant:
  - `CLEAR -> exactly one published_reconciliation row`;
  - `HELD -> zero published_reconciliation rows`;
- finalization replay checks the already-stored verdict/digest and remains
  idempotent.

The OxideBatch job never publishes directly. A completed framework execution
moves the workload-owned run to `PROCESSED`; only `finalize` may make a
publication authoritative.

### Processing commands

```text
payment-reconciliation-postgres run \
  --database-url <workload-db> \
  --snapshot-id <frozen-snapshot> \
  [--chunk-size 100] \
  [--page-size 250]

payment-reconciliation-postgres finalize \
  --database-url <workload-db> \
  --snapshot-id <frozen-snapshot>
```

Crash/recovery injection and replay evidence remain checkpoint 5 work.
