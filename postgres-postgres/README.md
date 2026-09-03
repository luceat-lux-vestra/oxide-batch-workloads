# postgres-postgres

Independent, external-consumer validation of published
[OxideBatch](https://github.com/luceat-lux-vestra/oxide-batch) **`0.6.0`**
(exact `crates.io` artifact, `postgres` feature): a PostgreSQL ->
PostgreSQL restartable batch transform, driven end to end through the real
production `oxide_batch::JobLauncher` / `ChunkJob` launch path.

This is PR 1 of campaign
[luceat-lux-vestra/oxide-batch-workloads#63](../../issues/63): the clean
cursor-mode vertical slice only. See [Scope of this PR](#scope-of-this-pr)
below for what later PRs add.

## Purpose

Validate, with real database evidence, that a released `oxide-batch`
consumer can:

- stream a PostgreSQL source table through the released server-side
  `postgres_cursor_reader` with bounded memory;
- transform each row through a small deterministic `ItemProcessor`;
- write the result through the released `postgres_batch_writer`, enlisted
  in the same business transaction OxideBatch's own checkpoint/component
  state commits through (`ChunkDeliveryMode::AtomicSameResource`);
- give every run a deterministic, mutation-sensitive source identity; and
- be checked by a verifier that never trusts the production code path it
  is checking.

## Architecture / data flow

```
app_source.source_customer
        |
        |  postgres_cursor_reader (DECLARE CURSOR / bounded FETCH,
        |  ORDER BY customer_id, own dedicated connection)
        v
   SourceRow { customer_id, full_name, is_active, balance_cents }
        |
        |  CustomerProjector (src/processor.rs; deterministic, pure)
        v
   ProjectedRow { customer_id, display_name, loyalty_score,
                  is_premium, row_fingerprint }
        |
        |  postgres_batch_writer, PostgresBatchMode::MultiRowValues,
        |  enlisted in the same business transaction OxideBatch's own
        |  checkpoint/component state commits through
        v
app_business.customer_projection
   (scoped by import_name + source_digest)
```

The whole path above runs through one real `ChunkJob` launched by
`JobLauncher::launch_chunk` (`src/job.rs`) -- never through
`oxide-batch-test`, never through a hand-rolled loop that reimplements
chunking, checkpointing, or transaction enlistment.

## Schemas

Three PostgreSQL schemas, kept structurally separate (never by convention
alone):

| Schema | Owner | Contents |
|---|---|---|
| `oxide_batch` | OxideBatch (`PostgresMigrator::migrate`, a public API) | Framework durable metadata. This workload's production commands (`run`, `verify`, `reset`) never write to it directly. |
| `app_source` | this workload | `source_customer`: the deterministic source table. |
| `app_business` | this workload | `customer_projection`: the deterministic transformed destination. |

```sql
CREATE TABLE app_source.source_customer (
    customer_id   BIGINT NOT NULL PRIMARY KEY,
    full_name     TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL,
    balance_cents BIGINT NOT NULL
);

CREATE TABLE app_business.customer_projection (
    import_name     TEXT NOT NULL,
    source_digest   TEXT NOT NULL,
    customer_id     BIGINT NOT NULL,
    display_name    TEXT NOT NULL,
    loyalty_score   BIGINT NOT NULL,
    is_premium      BOOLEAN NOT NULL,
    row_fingerprint BYTEA NOT NULL,
    PRIMARY KEY (import_name, source_digest, customer_id)
);
```

`customer_id` is the strict unique `BIGINT NOT NULL` ordering key both the
cursor reader (this PR) and the paging reader (PR 2) key off of.

The destination is a **deterministic transformed projection**, not a
byte-for-byte copy (`src/processor.rs::transform`):

- `display_name` = `full_name` upper-cased;
- `loyalty_score` = `balance_cents / 100` (integer division);
- `is_premium` = `balance_cents >= 50_000` ($500.00, a fixed threshold);
- `row_fingerprint` = first 16 bytes of `SHA-256(customer_id, full_name,
  is_active, balance_cents)`, demonstrating `BusinessValue::bytes` support.

No `TIMESTAMPTZ` column exists anywhere in this schema. The released
`PostgresBatchWriter` only supports `BusinessValue`'s `Text`/`Bytes`/
`I64`/`Bool`/`Null` representation (no temporal variant --
`csv-postgres`'s `src/writer.rs` documents this as a known v0.6.0
limitation, tracked upstream as
[luceat-lux-vestra/oxide-batch#218](https://github.com/luceat-lux-vestra/oxide-batch/issues/218)).
This workload deliberately designed around that limitation instead of
reimplementing a workaround, so it can exercise the released writer
directly and unmodified.

## Source identity

Before every `run`, `job::run` streams `app_source.source_customer`
(`ORDER BY customer_id`, `sqlx`'s row `fetch`, never `fetch_all`) through
`src/source_digest.rs::compute`, which hashes every business-significant
column into one SHA-256 digest. That digest becomes an *identifying*
`JobParameter` (`ParameterRole::Identifying`) alongside the user-facing
`import_name`.

Consequences, all covered by `tests/source_identity.rs`:

- the same source content under the same `import_name` resolves to the
  same `JobInstanceKey` (deterministic, reproducible digest for a fixed
  `(rows, seed)`);
- a changed source (different seed/content) resolves to a **different**
  `JobInstanceKey` -- it can never silently resume a stale checkpoint
  against different content;
- `app_business.customer_projection` rows are scoped by `(import_name,
  source_digest, customer_id)` in the primary key itself, so even two
  different import names that happen to observe an identical source digest
  cannot collide or overwrite each other's rows.

PR 1 proves this mechanism on the clean path only (determinism, a changed
seed producing a changed identity, collision-free destination scoping).
Full restart-against-mutated-source evidence is PR 3's scope (see below).

## Cursor semantics

`job::run` calls `oxide_batch::item_components::postgres_cursor_reader`
directly:

- `key_columns = [KeysetColumn::i64("customer_id")]` -- a real
  `BIGINT NOT NULL PRIMARY KEY` column is a valid strict total order;
- `PostgresCursorFormat::with_fetch_size(--fetch-size)` bounds one `FETCH`
  round trip's row count (default `200`; `ci/validate` also exercises a
  fetch size smaller than the CI dataset -- see
  `tests/reader_bounds.rs`);
- `map_row` uses only the public `PostgresRow` accessor methods
  (`i64`/`text`/`bool`), no private driver row type;
- the reader's own `ItemStream`/checkpoint state is registered on the
  `ChunkStep` via `with_item_stream`, and its component revision is
  declared in `ChunkComponentRevisions`, exactly as the released
  component's own contract requires.

The cursor reader's restart model (a fresh `DECLARE ... WHERE (customer_id)
> (restored)` on every process attempt) is entirely the framework's; this
workload does not reimplement or second-guess it. See
`oxide_batch::item_components::postgres_cursor`'s own module documentation
upstream for the full model. **Restart/crash evidence for this workload is
PR 3's scope, not this PR's.**

## Same-resource destination transaction boundary

`ChunkRestartContract::new(..., ChunkDeliveryMode::AtomicSameResource)`
(`src/job.rs::component_revisions`) is the only delivery mode this
workload's writer is compatible with: `postgres_batch_writer` (via
`src/writer.rs`) receives the enlisted `BusinessTransaction` from
`WriteContext::transaction()` and has no field, method, or code path that
could open a private connection or commit independently -- a structural
guarantee from the released component's own design, not a runtime check
this workload adds. `PostgresChunkTransactionManager` is used directly and
unwrapped (no decorator), so destination business rows and OxideBatch's own
checkpoint/component state commit through exactly one transaction the
framework manages.

## Verifier design

`src/verify.rs` is independent of the OxideBatch execution path: it never
imports `JobLauncher`/`ChunkJob`/`ChunkStep`, and it never calls
`processor::transform`/`processor::fingerprint` as its expected-value
oracle. It carries its own separate, hand-written copy of the same
transformation arithmetic (`expected_projection`), so a defect in
`src/processor.rs` is not automatically invisible to verification --
`tests/verifier_negative_control.rs` exercises this directly.

`verify --import-name <name>`:

1. recomputes the current source digest (the same streaming mechanism
   `job::run` uses);
2. streams `app_source.source_customer` `ORDER BY customer_id`, deriving
   each row's *expected* projection independently;
3. streams `app_business.customer_projection` scoped to `(import_name,
   source_digest)`, also `ORDER BY customer_id`;
4. merge-compares both streams in one bounded-memory pass: a `customer_id`
   present on only one side is reported as a missing or unexpected
   destination row the moment the merge's cursors diverge, and a
   `customer_id` present on both sides is compared field by field
   (`display_name`, `loyalty_score`, `is_premium`, `row_fingerprint`);
5. accumulates two independent running digests (expected-from-source,
   actual-from-destination) and reports both plus the exact row counts;
6. prints a JSON report to stdout and exits nonzero on **any** row-count,
   field, or digest mismatch.

`tests/verifier_negative_control.rs` proves three independent failure
modes are actually caught: a corrupted destination value, a missing
destination row, and an unexpected/extra destination row -- not merely a
row-count check.

## Commands

```
postgres-postgres migrate --database-url <url>
postgres-postgres seed --database-url <url> --rows <n> --seed <n> [--id-offset <n>]
postgres-postgres run --database-url <url> --import-name <name> [--chunk-size <n>] [--fetch-size <n>]
postgres-postgres verify --database-url <url> --import-name <name>
postgres-postgres reset --database-url <url>
```

`--database-url` also reads `DATABASE_URL` from the environment. `run` in
this PR supports **cursor mode only** -- there is no `--reader` flag yet
(PR 2 adds paging mode and the mode selector). `reset` truncates only
`app_source`/`app_business`; it never touches `oxide_batch`.

## Local development

```
docker compose up -d --wait
cargo run -- migrate
cargo run -- seed --rows 2000 --seed 42
cargo run -- run --import-name demo --chunk-size 200 --fetch-size 128
cargo run -- verify --import-name demo
```

`ci/validate ci` runs the full contract (format, clippy `-D warnings`,
build, an `.unwrap()`/`.expect()` production-path guard, real-PostgreSQL
integration tests, and a golden-path smoke sequence). `ci/validate msrv`
matches the declared `rust-version` in `Cargo.toml` with a locked,
database-free build.

## Current evidence scope (PR 1)

Proven, with real PostgreSQL 18, in this PR:

- exact published `oxide-batch = "=0.6.0"` provenance (registry-resolved
  lockfile, no path/git/patch substitution -- enforced by the repository's
  own `validate-oxidebatch-provenance.py`);
- deterministic seed/source generation and its digest determinism/
  sensitivity;
- cursor-mode clean execution through the real `JobLauncher`/`ChunkJob`
  path, exactly once per row, with a bounded `--fetch-size` exercised
  against a dataset spanning multiple `FETCH` batches;
- the released `PostgresBatchWriter` used directly, enlisted in the
  same-resource atomic business transaction;
- independent verification, including three corruption negative controls;
- collision-free destination scoping across distinct import names/source
  identities;
- workload-owned `reset` never touches `oxide_batch` metadata.

## Explicit limitations (not this PR)

- **No paging mode.** `postgres_paging_reader` is PR 2.
- **No crash/recovery evidence.** No hard-kill, no `recover` command, no
  pre-commit/post-commit crash boundary testing. That is PR 3.
- **No restart-against-mutated-source evidence.** Source identity's
  clean-path mechanism is proven here; resuming (or correctly refusing to
  resume) a checkpoint after a source mutation is PR 3.
- **No benchmark/throughput claims.** Not attempted before the correctness/
  recovery campaign closes (see `ROADMAP.md`).
- **No Spring Batch comparison, no cross-workload shared framework, no
  central-CI workload-specific branching.** Out of scope for this campaign
  entirely (see the parent issue's non-goals).
- **No production readiness or exactly-once claim.** This PR demonstrates
  clean-path correctness only; do not read anything here as a durability or
  concurrency guarantee beyond what is explicitly stated above.
