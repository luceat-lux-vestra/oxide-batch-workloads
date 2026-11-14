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
`postgres-local-partition` dependency graph with only the root package name
changed. The direct dependency set is intentionally identical at this scaffold
checkpoint so `cargo --locked` remains deterministic without introducing an
unreviewed dependency update.

## Checkpoint 2 boundary

Implemented in this checkpoint:

- real-workload registry entry;
- exact published OxideBatch dependency and committed lockfile;
- workload-owned initial PostgreSQL schema;
- frozen CLI command surface;
- workload-owned `ci/validate <ci|msrv>`.

Intentionally not implemented yet:

- Merchant/provider snapshot acquisition;
- deterministic mismatch scenario derivation;
- OxideBatch reconciliation processing;
- crash/recovery;
- CLEAR/HELD finalization and publication;
- independent business oracle;
- retained Evidence Contract v2 artifacts.

Every CLI command currently fails closed with an explicit scaffold-only message.
That is deliberate: later checkpoints implement each responsibility without
silently claiming behavior at this boundary.

## Frozen commands

```text
payment-reconciliation-postgres migrate
payment-reconciliation-postgres snapshot
payment-reconciliation-postgres derive-scenario
payment-reconciliation-postgres run
payment-reconciliation-postgres recover
payment-reconciliation-postgres finalize
payment-reconciliation-postgres verify
payment-reconciliation-postgres reset
```

The migration owns only the `payment_reconciliation` schema. It does not modify
payment-reference source schemas or OxideBatch framework metadata.
