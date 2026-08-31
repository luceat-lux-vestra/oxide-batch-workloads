# csv-postgres

**OxideBatch 0.6.0 real-workload validation #1**: a streaming CSV → PostgreSQL
restartable batch import, built as an independent external consumer against
the **published `oxide-batch = "0.6.0"` crates.io artifact** — never a local
checkout of the framework. See the [top-level README](../README.md) for the
purpose of this repository.

This is not a toy example. It exists to answer, with evidence, whether
OxideBatch 0.6.0's public API is usable by a real application, and what its
transaction/checkpoint/restart/idempotency guarantees actually are.

## What it does

```
CSV file
  -> oxide_batch::item_components::delimited_file_reader   (streaming, restartable)
  -> CustomerRowProcessor (ItemProcessor)                    validates/parses fields
  -> CustomerRowWriter (ItemWriter)                           enlisted INSERT, PostgreSQL
  -> PostgresChunkTransactionManager                          commit / rollback / checkpoint
```

Launched through the real production path: `oxide_batch::JobLauncher::launch_chunk`
against a `ChunkJob`/`ChunkStep` — not the framework's own `oxide-batch-cli`
operator binary (which only inspects/recovers durable state; see its README),
and not `oxide-batch-test`'s `TestJob` (an in-process test harness, dev-only).

## Quickstart

```sh
# 1. PostgreSQL 18, locally
docker compose up -d   # or: docker-compose up -d

# 2. Build
cargo build --release
export DATABASE_URL=postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5433/csv_postgres_workload

# 3. Migrate: OxideBatch's own `oxide_batch` schema + this workload's `app_business` schema
cargo run -- migrate

# 4. Generate a deterministic dataset and import it
cargo run -- generate --output customers.csv --profile tiny --seed 42
cargo run -- run --input customers.csv --import-name customers_import --chunk-size 500

# 5. Verify against PostgreSQL directly (never log strings)
cargo run -- verify --input customers.csv
```

> **PostgreSQL 18 image note**: the official `postgres:18` image changed its
> expected data-directory layout — the compose volume mounts at
> `/var/lib/postgresql` (not `.../data`); mounting at the old path fails the
> container outright. See the comment in `docker-compose.yml`.

## CLI

| Command | Purpose |
|---|---|
| `generate` | Deterministic synthetic CSV (see below). Writes a `.manifest.json` sidecar (rows/seed/size/SHA-256). |
| `migrate` | `PostgresMigrator::migrate` (framework's `oxide_batch` schema) + this workload's `app_business` schema. |
| `run` | Launches or resumes the import job through `JobLauncher::launch_chunk`. |
| `recover` | Marks a `Starting/Started/Stopping/Unknown` execution left by a hard crash as recoverable (`RecoveryRequest::mark_failed`), required before a subsequent `run` can resume it — see *Restart semantics* below. |
| `verify` | Independently re-parses `--input` (a real CSV parser) and compares it against the database directly: row count *and* a full-content digest (`customer_id`/`name`/`email`/`amount`/`created_at`, both computed by streaming). Prints a JSON report to stdout and **exits nonzero on any mismatch** — this is not a DB-only summary. Requires `--input` to be in strictly ascending `customer_id` order (validated, fails closed if not — every `generate`d dataset satisfies this by construction; see `src/verify.rs`'s doc comment). |
| `reset` | Truncates only `app_business.imported_customer`. Never touches `oxide_batch`. |

`run` flags for deterministic fault injection (built into the shipped binary
itself, not just the test suite):

```
--fail-at chunk:N | row:N
--failure-mode before-write | during-write | after-business-commit
--hard-crash                  # std::process::abort() instead of a typed error
--idempotent-writes           # ON CONFLICT (customer_id) DO NOTHING instead of a strict insert
```

Example — crash immediately after chunk 50 commits, then recover and restart:

```sh
cargo run -- run --input customers.csv --import-name demo --chunk-size 500 \
  --fail-at chunk:50 --failure-mode after-business-commit --hard-crash
# process aborts (SIGABRT); the DB has exactly 50 chunks' worth of committed rows

cargo run -- recover --import-name demo --input customers.csv
cargo run -- run --input customers.csv --import-name demo --chunk-size 500
```

## Dataset generator

Deterministic and seeded (`rand` + `ChaCha8Rng` — never OS entropy): the same
`(rows, seed, id-offset)` always produces a byte-identical CSV.

```sh
cargo run -- generate --output out.csv --profile tiny|normal|stress --seed 42
```

| Profile | Rows | Use |
|---|---|---|
| `tiny` | 1,000 | CI |
| `normal` | 100,000 | Local validation |
| `stress` | 1,000,000 | Manual resource/perf observation |

Edge cases (deterministic positions, always present unless the row count is
too small): a quoted field containing a comma, a field with an escaped
(doubled) quote. Explicit, position-targeted edge cases:

```
--inject-duplicate-at N     # re-emits row N-1's customer_id at row N (real PK duplicate)
--inject-malformed-at N     # row N is missing its trailing field
--inject-bad-amount-at N    # row N's amount is non-numeric
--id-offset N                # shifts every customer_id by N (test isolation on a shared table)
```

Encoding: UTF-8, LF. CRLF is not exercised by the generator or covered by a
test; BOM is out of scope (spec ss10). Undocumented input in either regard is
unsupported.

## Database schema

Two schemas, deliberately never overlapping:

- `oxide_batch` — OxideBatch's own durable job/step/instance metadata
  (`PostgresMigrator::migrate`). No command this workload ships (`migrate`,
  `run`, `recover`, `verify`, `reset`) ever creates, alters, or drops
  anything here.

  The **exception** is `validation/generate-evidence.sh`, a test/evidence
  script, not a shipped command: it `TRUNCATE`s
  `oxide_batch.ob_job_execution`/`ob_job_instance` **rows** (never the
  schema or table structure) between scenarios, so the same deterministic
  job identity can be relaunched on every re-run without colliding with a
  prior run's already-`COMPLETED` instance. This is disposable test-only
  teardown and assumes an isolated/throwaway database — run it against
  `docker-compose.yml`'s database, never a shared or production one.
- `app_business` — this workload's own business table
  (`migrations/001_init.sql`):

  ```sql
  CREATE TABLE app_business.imported_customer (
      customer_id BIGINT PRIMARY KEY,
      name        TEXT NOT NULL,
      email       TEXT NOT NULL,
      amount      BIGINT NOT NULL,
      created_at  TIMESTAMPTZ NOT NULL
  );
  ```

  The `PRIMARY KEY` is a real database constraint: duplicate-key correctness
  is judged by PostgreSQL, never by application code alone.

## Restart semantics (what was actually observed, not assumed)

- Job identity includes the input file's **SHA-256** as an identifying
  `JobParameter`, alongside `import_name`. Restarting against the *same*
  file resumes the same `JobInstance`. Restarting against the *same path*
  whose *content changed* resolves to a **different** `JobInstanceKey` — a
  fresh, independent instance — so the framework itself never resumes a
  stale checkpoint against mutated bytes (proven in `tests/input_identity.rs`).
- A **graceful** component failure (a typed error, no crash) reaches a
  terminal `FAILED` status by itself; a plain `run` afterward resumes it —
  no `recover` call needed (`tests/restart.rs`).
- A **hard process crash** (`std::process::abort()`) leaves the execution
  recorded as `STARTED`/`UNKNOWN` — never a terminal status, since nothing
  survived to persist one. A plain `run` against it is **rejected** by the
  framework (`"job instance N already has execution N in STARTED"`); an
  explicit `recover` (`RecoveryRequest::mark_failed`) is required first.
- Under `ChunkDeliveryMode::AtomicSameResource` (what this workload uses:
  business writes and the framework's own checkpoint/progress share one
  PostgreSQL transaction), a chunk killed immediately after its commit
  returns success is durable — its rows are never lost — but this workload
  does **not** claim the framework guarantees the *reader* never revisits
  that chunk's byte range on a subsequent attempt. What is verified,
  end to end: **final business state has no duplicates**, verified against
  the exact same input file used for both the clean run and the
  crash+restart run — same bytes, same `customer_id` range, full row
  content compared (not an offset-derived subset) — in
  `tests/restart.rs::clean_run_and_recovered_run_converge_to_the_same_content`
  and `validation/restart-run.json`'s `full_content_digests_match`. That is
  a final-state guarantee (Claim B, spec ss24), not a stronger "never
  reprocessed" claim (Claim A) — this workload does not have evidence for
  Claim A independent of Claim B and does not assert it.

## Application-level idempotency boundary

`postgres_batch_writer`-style strict `INSERT` (no `ON CONFLICT`) is the
default: a duplicate business key is a real `PRIMARY KEY` violation that
rolls back the whole containing chunk and fails the job
(`tests/rollback.rs`). `--idempotent-writes` switches to
`ON CONFLICT (customer_id) DO NOTHING`, which silently absorbs a duplicate
and completes with the correct final count. **Both are demonstrated
separately, deliberately not blended** (spec ss26): defaulting to
`ON CONFLICT` everywhere would hide a framework reprocessing defect behind
apparent success.

## Error provenance and diagnosability

Two separate claims, kept separate on purpose:

- **Transaction correctness: PASS.** A real `PRIMARY KEY` violation (or any
  other business-transaction rejection) rolls back its whole containing
  chunk correctly and fails the job — independently verified in
  `tests/rollback.rs` by checking actual database row counts, not by
  trusting a status code.
- **Root-cause provenance through the public API: limited, by framework
  design.** `BusinessTransactionError` (`Infrastructure` / `Rejected` /
  `Cancelled`) and `WriterError`'s `FailureCategory` are both
  value-redacted by OxideBatch itself — the framework's own PostgreSQL
  adapter discards the `SQLSTATE`, constraint name, and driver error
  *before* any consumer code (including `src/writer.rs`'s
  `CustomerRowWriter`) ever sees the failure. A real `PRIMARY KEY`
  violation and a transient connection failure are both indistinguishable
  stable categories at this workload's boundary — there is no lower-level
  public extension point in 0.6.0 that would let a consumer recover the
  discarded detail, so no consumer-side workaround is possible or
  attempted here. Filed as
  [luceat-lux-vestra/oxide-batch#220](https://github.com/luceat-lux-vestra/oxide-batch/issues/220).

## Findings against OxideBatch 0.6.0

- **Missing capability (diagnostics)** — `BusinessTransactionError`/
  `WriterError` are value-redacted down to a stable category with no
  driver/`SQLSTATE`/constraint detail recoverable at the public consumer
  boundary; see *Error provenance and diagnosability* above and
  [luceat-lux-vestra/oxide-batch#220](https://github.com/luceat-lux-vestra/oxide-batch/issues/220).
- **API gap** — `item_components::postgres_batch_writer` generates each
  row's placeholder group as a fixed `($1, $2, ...)`; there is no way to
  add a per-column SQL cast, and `BusinessValue` has no temporal variant
  (only `Text`/`Bytes`/`I64`/`Bool`/`Null`). Binding a timestamp as
  `BusinessValue::text(...)` against a `TIMESTAMPTZ` column fails —
  confirmed directly against PostgreSQL 18 (`PREPARE`/`EXECUTE`): `ERROR:
  column "created_at" is of type timestamp with time zone but expression is
  of type text`. **`postgres_batch_writer` cannot write into any
  non-text/i64/bool/bytes column.** Worked around with a hand-written
  `ItemWriter` on the lower `BusinessTransaction`/`BusinessStatement`
  primitive (`src/writer.rs`) — legitimate use of a real, lower-level public
  extension point, not a framework reimplementation. Filed as
  [luceat-lux-vestra/oxide-batch#218](https://github.com/luceat-lux-vestra/oxide-batch/issues/218).
- **Doc/release inconsistency** — the `v0.6.0` tag's `README.md` states
  "`oxide-batch` `0.6.0` ... is not yet published", while crates.io already
  lists it as the published `default_version`. Cosmetic, but a reader
  landing on the tagged README would be misled about publication status.
- **Ergonomics, positive** — `ChunkDeliveryMode` naming its two delivery
  modes directly in the type system (`AtomicSameResource` /
  `AtLeastOnce`) is unusually good, concrete, load-bearing documentation:
  it let this workload state its own delivery-semantics claim precisely
  rather than guessing.
- **Ergonomics, friction** — building a durable `ChunkJob` by hand (outside
  `oxide-batch-test`'s convenience constructors, which are dev-only)
  requires assembling `ChunkComponentRevisions` +
  `ChunkRestartContract` + `StateSchemaId`/`StateSchemaVersion` +
  `ComponentStreamIdentity` registration on both the step and the
  component-revisions side, with the two required to describe the same set
  (enforced only at `ChunkJob::new` time, not by types). Workable, but
  entirely undocumented outside the test-kit examples; a first-party
  "building a durable ChunkJob without the test kit" guide would have saved
  real trial and error here.
- A production consumer wrapping `ChunkTransactionManager` for
  fault-injection/observability purposes must override **both** `begin`
  *and* `begin_for` (the durable, repository-backed path used by the real
  launcher) and **both** `commit` *and* `commit_with_component_state` (the
  one actually invoked once any `ItemStream` is registered) — the trait's
  standalone-friendly default for the unused pair silently no-ops
  otherwise. Not a defect (the docs explain it once you look), but easy to
  miss on a first read; see `src/failpoint.rs`'s module doc.

  These three documentation/ergonomics findings are filed together as
  [luceat-lux-vestra/oxide-batch#219](https://github.com/luceat-lux-vestra/oxide-batch/issues/219).

## Resource observations (observational, not a benchmark)

CPU: Apple M1 Max. OS: macOS 26.6.2. PostgreSQL: 18.6 (`postgres:18` image,
via `docker-compose.yml`). Rust: 1.98.0. `cargo build --release`.

| Profile | Rows | CSV size | Runtime | Peak RSS |
|---|---|---|---|---|
| normal | 100,000 | 7.7 MB | 1.58s (~63k rows/s) | ~13.4 MB |
| stress | 1,000,000 | 79.5 MB | 15.17s (~66k rows/s) | ~13.4 MB |

A 10x increase in row count (and file size) produced **no measurable
increase** in peak RSS. This is a corrected number: an earlier version of
this workload computed the input's SHA-256 (job identity, ss15) by reading
the whole file into memory (`std::fs::read`) *before* the streaming reader
ever opened it, which — while the reader/writer/processor loop was itself
already properly chunked — meant actual peak memory still scaled with file
size (the previous, wrong measurement: ~21 MB → ~93 MB, a ~4.4x increase
for the same 10x row-count step, misattributed in an earlier revision of
this README to "the streaming architecture" when in fact a real
non-streaming step was the dominant contributor). `sha256_of_file`
(`src/generator.rs`) now streams through a fixed 64 KB buffer; `verify`
(`src/verify.rs`) streams both sides it compares (a real `csv` crate
parser reading the source file record-by-record, and `sqlx`'s row `fetch`
stream against the database, never `fetch_all`) rather than buffering
either whole. The flat 13.4 MB now genuinely reflects
`memory ≈ runtime baseline + O(chunk_size)` (spec ss9), not file size.

## Test suite

```sh
docker compose up -d
export CSV_POSTGRES_TEST_DATABASE_URL=postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5433/csv_postgres_workload
export DATABASE_URL="$CSV_POSTGRES_TEST_DATABASE_URL"
cargo run -- migrate
cargo test -- --test-threads=1
```

`--test-threads=1` is this suite's default, not just a suggestion: several
tests either inspect global `pg_stat_activity` state or `reset()` the whole
shared business table (each `tests/support::generate` call gets its own
`customer_id` range for isolation, but a handful of tests need the *whole*
table to themselves for a moment regardless — see each such test's own doc
comment for why). Rather than track and re-document exactly which tests
that is and let the count go stale as tests are added (as happened once
already), the whole suite -- and CI -- just runs serialized.

Every test spawns the **compiled binary** as a real child process (never an
in-process function call) and asserts on **PostgreSQL state** — row counts,
`PRIMARY KEY`-enforced uniqueness, `oxide_batch.ob_job_execution` status,
canonical content digests — never on log strings. `tests/restart.rs`'s
hard-crash scenarios use a genuine `std::process::abort()` (SIGABRT) inside
the child; restart always launches a brand-new process.

| File | Covers |
|---|---|
| `tests/clean_import.rs` | T1: real source-vs-database correctness via `verify` (not a spot-check), a negative control that corrupts one DB value and asserts `verify` rejects it, a negative control that proves `verify`'s ascending-`customer_id` ordering contract is actually enforced, no leaked idle-in-transaction session |
| `tests/malformed_input.rs` | T2: wrong field count / non-numeric amount fails the job, zero partial rows |
| `tests/rollback.rs` | T3, T7: real PK violation, strict vs. idempotent |
| `tests/restart.rs` | T4, T5, T6, T8: graceful vs. hard-crash failure windows, recovery, clean-vs-recovered equivalence |
| `tests/input_identity.rs` | T9: content-mutated input creates a new instance, never a stale resume |

## Reproducing the evidence files

```sh
docker compose up -d
DATABASE_URL=postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5433/csv_postgres_workload \
  ./validation/generate-evidence.sh
```

Writes `validation/{clean-run,crash-run,restart-run}.json`.

## What this workload does not cover

Out of scope for this first workload (spec ss50): JSON/DB-to-DB ingest,
distributed/partitioned/parallel execution, a scheduler, a web
dashboard/metrics/tracing backend, retry/skip policy extension, graceful
`SIGTERM` shutdown as an acceptance criterion, CRLF/BOM input, and any
`oxide-batch` framework source change (findings above are reported, not
patched here).
