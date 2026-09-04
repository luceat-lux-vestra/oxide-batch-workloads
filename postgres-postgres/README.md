# postgres-postgres

Independent, external-consumer validation of published
[OxideBatch](https://github.com/luceat-lux-vestra/oxide-batch) **`0.6.0`**
(exact `crates.io` artifact, `postgres` feature): a PostgreSQL ->
PostgreSQL restartable batch transform, driven end to end through the real
production `oxide_batch::JobLauncher` / `ChunkJob` launch path, in either of
two reader modes.

This is PR 3 of campaign
[luceat-lux-vestra/oxide-batch-workloads#63](../../issues/63): rollback,
hard-crash, and genuine new-process recovery evidence added on top of PR 1's
clean cursor-mode vertical slice and PR 2's paging-mode parity. See
[Explicit limitations](#explicit-limitations-not-this-pr) below for what
PR 4 still adds (benchmarking, retained evidence, campaign closure).

## Purpose

Validate, with real database evidence, that a released `oxide-batch`
consumer can:

- stream a PostgreSQL source table through either the released server-side
  `postgres_cursor_reader` (a real streamed cursor) or the released
  `postgres_paging_reader` (independent, bounded keyset pages, no
  server-side resource held between pages), both with bounded memory;
- transform each row through a small deterministic `ItemProcessor`,
  identical regardless of which reader produced it;
- write the result through the released `postgres_batch_writer`, enlisted
  in the same business transaction OxideBatch's own checkpoint/component
  state commits through (`ChunkDeliveryMode::AtomicSameResource`);
- give every run a deterministic, mutation-sensitive source content
  identity (`source_digest`, independent of which reader mode produced
  it), with `reader_mode` itself participating in job identity as its own
  separate identifying parameter;
- prove cursor and paging produce equivalent business results over
  identical source content;
- be checked by a verifier that never trusts the production code path it
  is checking; and
- survive a pre-commit typed failure (whole-chunk rollback), a real
  `SIGKILL` before a chunk commits, and a real `SIGKILL` immediately after
  one commits -- recovering, in a genuinely new OS process, through the
  public OxideBatch recovery/operator API only, to a final business state
  representation-identical to a clean run, for both reader modes (see
  [Crash / rollback / recovery evidence](#crash--rollback--recovery-evidence-pr-3)
  below).

## Architecture / data flow

```
app_source.source_customer
        |
        |  --reader cursor: postgres_cursor_reader (DECLARE CURSOR /
        |  bounded FETCH, one dedicated connection held for the run)
        |    -- or --
        |  --reader paging: postgres_paging_reader (independent, bounded
        |  WHERE (customer_id) > (last) ORDER BY customer_id LIMIT
        |  page_size pages; no OFFSET, no resource held between pages)
        |
        |  both: ORDER BY customer_id, same base query
        v
   SourceRow { customer_id, full_name, is_active, balance_cents }
        |
        |  CustomerProjector (src/processor.rs; deterministic, pure;
        |  identical for both reader modes)
        v
   ProjectedRow { customer_id, display_name, loyalty_score,
                  is_premium, row_fingerprint }
        |
        |  postgres_batch_writer, PostgresBatchMode::MultiRowValues,
        |  enlisted in the same business transaction OxideBatch's own
        |  checkpoint/component state commits through
        v
app_business.customer_projection
   (scoped by import_name + source_digest -- see "Reader mode and job
   identity" below for why reader_mode is not part of this scoping key)
```

The whole path above runs through one real `ChunkJob` launched by
`JobLauncher::launch_chunk` (`src/job.rs`) -- never through
`oxide-batch-test`, never through a hand-rolled loop that reimplements
chunking, checkpointing, or transaction enlistment. `src/job.rs` keeps
everything above common to both modes (source-stability guard, source
digest, processor, writer, transaction manager, `ChunkStep`/`ChunkJob`
construction, `JobLauncher`, terminal status handling) in one generic
`launch_and_finish` helper; only reader construction, stream namespace, and
component/definition revisions branch per mode.

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
cursor reader and the paging reader key off of -- the sole strict
total-order key either reader ever uses; paging never issues `OFFSET`.

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

### Closing the digest/read TOCTOU window

Computing a digest and later reading the source again (on a separate
connection, potentially after other work) is a time-of-check-to-time-of-use
hazard on its own: a digest computed against one snapshot says nothing
about what a later, independent read will actually observe if the source
was mutated in between. `job::run` and `verify` both close this for real,
at the database level, via
`src/source_digest.rs::lock_source_for_stable_read`: a dedicated
transaction holding `LOCK TABLE app_source.source_customer IN SHARE MODE`
from *before* the digest is computed until *after* the source has actually
been read (whichever reader `run` selected -- cursor or paging, both open
their own separate connection/pool after the digest is computed, so both
need the guard exactly as much; the independent comparison read for
`verify`). `SHARE MODE` blocks every other session's write to that table for
as long as the guard is held, while never blocking the plain reads both
readers and the verifier need to keep working -- this is a real
PostgreSQL-enforced guarantee, not a cooperative convention.
`tests/source_stability.rs` attacks this window directly, for **both**
reader modes: it confirms (via `pg_locks`) the guard is actually held while
a run is in flight, then proves a concurrent write against
`app_source.source_customer` is genuinely blocked (and that it succeeds
immediately once the run releases the guard).

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
Restart-against-mutated-source evidence, across a real process crash, is
this PR's own scope -- see
[Crash / rollback / recovery evidence](#crash--rollback--recovery-evidence-pr-3)
below.

## Reader modes

`run` requires an explicit `--reader cursor|paging` -- there is no default,
so a run can never silently pick a reader mode. Both modes read the exact
same base query and key off the exact same `key_columns =
[KeysetColumn::i64("customer_id")]` (a real `BIGINT NOT NULL PRIMARY KEY`
column is a valid strict total order); `map_row` for both uses only the
public `PostgresRow` accessor methods (`i64`/`text`/`bool`), no private
driver row type. Both readers' own `ItemStream`/checkpoint state is
registered on the `ChunkStep` via `with_item_stream`, and their component
revisions are declared in `ChunkComponentRevisions`, exactly as each
released component's own contract requires (`src/job.rs::component_revisions`).

### `--reader cursor`

`job::run` calls `oxide_batch::item_components::postgres_cursor_reader`
directly:

- `PostgresCursorFormat::with_fetch_size(--fetch-size)` bounds one `FETCH`
  round trip's row count (default `job::DEFAULT_FETCH_SIZE` = `200`;
  `ci/validate` also exercises a fetch size smaller than the CI dataset --
  see `tests/reader_bounds.rs`);
- a real streamed server-side `DECLARE CURSOR` session held on one
  dedicated connection for the run's duration.

The cursor reader's restart model (a fresh `DECLARE ... WHERE (customer_id)
> (restored)` on every process attempt) is entirely the framework's; this
workload does not reimplement or second-guess it. See
`oxide_batch::item_components::postgres_cursor`'s own module documentation
upstream for the full model.

### `--reader paging`

`job::run` calls `oxide_batch::item_components::postgres_paging_reader`
directly, over the same logical base query and row mapper as cursor mode:

- `PostgresPagingFormat::with_page_size(--page-size)` bounds one page
  query's row count (default `job::DEFAULT_PAGE_SIZE` = `250`);
- each page is an independent, bounded `WHERE (customer_id) > (last)
  ORDER BY customer_id LIMIT page_size` query issued over the reader's own
  pool -- the pinned `v0.6.0` implementation never issues `OFFSET`, and no
  server-side resource (transaction, cursor) is held between pages. This
  workload does not construct that predicate itself, and does not add its
  own `ORDER BY`/`LIMIT`/`OFFSET` to the workload SQL -- those are entirely
  the released reader's own responsibility, exercised through its public
  configuration surface only.

`--fetch-size` and `--page-size` are mode-specific and mutually exclusive:
supplying the wrong one for the selected `--reader` is a configuration
error (nonzero exit, no job execution ever recorded), never a silently
ignored no-op -- see `tests/reader_config.rs`. A page size of `0` is
likewise rejected, by the released reader's own construction-time
validation (`PostgresComponentConfigError::InvalidFetchSize`) -- this
workload does not reimplement that check.

**Restart/crash evidence for both reader modes is this PR's own scope** --
see [Crash / rollback / recovery evidence](#crash--rollback--recovery-evidence-pr-3)
below.

## Reader mode and job identity

`source_digest` (`src/source_digest.rs`) remains exactly what it was in
PR 1: a mode-independent identity over the source table's own content, with
no knowledge of which reader will, or did, read it. `reader_mode`
(`"cursor"` or `"paging"`) is a separate, third *identifying*
`JobParameter`, alongside `import_name` and `source_digest`
(`src/job.rs::parameters`) -- it participates in *job* identity, not in
source identity. Job identity is therefore effectively `import_name +
source_digest + reader_mode`: the same import name against the exact same
source content, run once under each reader mode, resolves to two distinct
`JobInstance`s -- proven directly against `oxide_batch.ob_job_instance`
(framework-owned durable metadata) in `tests/reader_mode_identity.rs`,
including that each instance's own `identifying_parameters` records its own
`reader_mode` value with role `identifying`.

This is not incidental: cursor and paging both persist the same
`KeysetPosition` payload type through their own `ItemStream`, but under
different schema/codec identity (`CursorKeysetSchema`/
`oxide-batch.postgres-cursor-reader-position-codec` vs.
`PagingKeysetSchema`/`oxide-batch.postgres-paging-reader-position-codec`),
different stream namespaces
(`oxide-batch-workload.postgres-postgres.cursor-reader` vs.
`...paging-reader`), and different reader/stream component revisions. A
shared payload shape is not license to cross-interpret one mode's state
contract as the other's: resuming one mode's persisted stream state through
the other reader would not be a resume -- it would be a silent
reinterpretation of one component's bytes by an unrelated component. This
workload never attempts that.

Because cursor and paging declare structurally different `ChunkJob`
manifests (different component revisions) under the same `job_name`,
OxideBatch's own repository requires them to use different
`DefinitionRevision` strings too (`job {name} definition revision {rev} has
drifted` is a hard error otherwise, since `(job_name, definition_revision)`
is pinned to exactly one manifest forever) -- so the whole-job definition
bump from PR 1's single `postgres-postgres-transform-v1` is, per mode,
`postgres-postgres-transform-cursor-v2` and
`postgres-postgres-transform-paging-v2` (`src/job.rs::definition_revision`).
Processor, writer, checkpoint, and execution-context revisions/schemas are
unchanged from PR 1 for both modes -- reader mode has no bearing on any of
those.

`app_business.customer_projection`'s primary key remains `(import_name,
source_digest, customer_id)` -- unchanged, and deliberately **not**
including `reader_mode`: business output scoping and job-instance identity
are different concerns. Cursor and paging against the same import name and
source content write into the *same* destination scope (proven equivalent
field-by-field in `tests/reader_parity.rs`), while still being tracked as
two separate, independently resumable `JobInstance`s at the framework
level.

### Historical PR 1 compatibility

PR 1's cursor `JobInstance`s were created before `reader_mode` existed as a
parameter at all. Under this PR's identity scheme, an import name/source
content that previously resolved to a PR 1 instance now resolves to a
*different* `JobInstanceKey` (the addition of an identifying parameter
necessarily changes the key), so a PR 1 instance is not, and cannot be,
resumed by a PR 2 `--reader cursor` run against the same import
name/source. This is an intentional, campaign-local, one-time compatibility
transition, not a defect: no framework metadata is mutated to migrate PR 1
instances forward, and no such migration is planned. A deployment that
still needs to resume a specific pre-PR-2 instance must do so on the PR 1
binary before adopting PR 2.

## Same-resource destination transaction boundary

`ChunkRestartContract::new(..., ChunkDeliveryMode::AtomicSameResource)`
(`src/job.rs::component_revisions`, shared unchanged by both reader modes)
is the only delivery mode this workload's writer is compatible with:
`postgres_batch_writer` (via `src/writer.rs`) receives the enlisted
`BusinessTransaction` from `WriteContext::transaction()` and has no field,
method, or code path that could open a private connection or commit
independently -- a structural guarantee from the released component's own
design, not a runtime check this workload adds. `PostgresChunkTransactionManager`
is used directly and unwrapped (no decorator), so destination business rows
and OxideBatch's own checkpoint/component state commit through exactly one
transaction the framework manages -- identically regardless of which reader
produced the item being written.

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

## Crash / rollback / recovery evidence (PR 3)

Proven, with real PostgreSQL 18 and the published `oxide-batch = "=0.6.0"`
public API, for **both** reader modes, driving the real compiled
`postgres-postgres` binary as a genuine child (and, for the two crash
scenarios, a genuinely *new* child afterward) process -- never an in-process
function call standing in for a crash:

1. **Pre-commit typed rollback** (`tests/rollback.rs`): a typed error
   returned after a target chunk's business `INSERT`s execute but before its
   transaction commits rolls the *whole* chunk back. The previously
   committed chunks stay durable; the target chunk contributes zero rows and
   the durable checkpoint stays at the previous chunk's boundary. A plain
   restart (no `recover` needed -- the launcher's own future completed and
   persisted a terminal `FAILED` status) then completes the exact remainder.
2. **Hard crash before commit** (`tests/crash_before_commit.rs`): a real
   `SIGKILL` delivered to the child process after a target chunk's business
   writes have executed inside its still-open enlisted transaction, but
   before that transaction commits. PostgreSQL rolls back the abandoned
   transaction on connection loss, so neither the chunk's business rows nor
   its checkpoint become durable; the previous chunk's boundary is
   unaffected. A plain restart is refused by the framework itself (an
   execution still recorded in progress); `recover` (through the public
   operator/recovery API) is required first, and continuation runs in a
   brand-new process.
3. **Hard crash immediately after commit** (`tests/crash_after_commit.rs`):
   a real `SIGKILL` delivered immediately after a target chunk's atomic
   commit call returns success. The committed chunk's business rows and
   checkpoint are both durable together (never a split state where one
   advanced without the other); after `recover` and a restart in a new
   process, the final business state has no missing, extra, or duplicate
   rows, and is byte-for-byte content-digest-identical to a clean run over
   the same source content (`tests/crash_after_commit.rs`'s
   `destination_content_digest` comparison) -- not merely the same row
   count.
4. **Source-mutation / stale-checkpoint isolation across a crash**
   (`tests/source_mutation_recovery.rs`): once a crashed, non-terminal
   execution's source content is mutated (after the run/lock window has
   closed -- see [Source identity](#source-identity)), the resulting new
   `source_digest` resolves `recover` and a fresh `run` to a **different**
   `JobInstance`, never a silent resume of the stale, differently-keyed
   checkpoint. The crashed instance's own destination scope is left exactly
   as it was, untouched by the new instance.
5. **Recovery selection is explicit and fails closed**
   (`tests/recover_negative.rs`): `recover` rejects an import name/reader
   that was never run, an execution that already reached a terminal status,
   and a reader mode that does not match the crashed instance's own
   identifying parameter -- it never guesses which execution to recover.

### Deterministic failure injection (`src/failpoint.rs`)

A workload-local `FailingWriter`/`FailingTransactionManager` pair, built
only on the same public traits a real consumer implements against
(`ItemWriter`, `ChunkTransactionManager`, `ChunkTransaction` --
`postgres-postgres` does not depend on `csv-postgres`, and does not use
`oxide-batch-test`'s injection types, which are a dev-only dependency and
cannot ship in a production binary; this mirrors `csv-postgres/src/failpoint.rs`'s
established, independent shape in this same repository, not a shared
dependency). `run --fail-at-chunk N --failure-mode during-write|after-business-commit`
targets a 1-based chunk-transaction-attempt ordinal deterministically (never
randomly), at exactly one of two semantic points: after the writer's real
`INSERT` executes but before that chunk's transaction commits
(`during-write`), or after the transaction's commit call has already
returned success (`after-business-commit`). What happens when the failpoint
fires is a second, independent choice:

- by default, a typed graceful error (the chunk rolls back; the process
  then exits non-zero on its own) -- the pre-commit rollback scenario;
- with `--pause-for-kill <path>`, instead of erroring or self-terminating,
  the process writes its own PID to `<path>` and then blocks forever. The
  *test harness* -- a separate parent process -- polls for that marker file
  (never a fixed sleep guess) and, once observed, delivers a real `SIGKILL`
  to that exact child (`std::process::Child::kill`). This is a genuine,
  externally delivered OS-level process termination, synchronized to the
  precise semantic boundary under test.

### `recover`

```
postgres-postgres recover --database-url <url> --import-name <name> --reader cursor|paging
```

Marks a `Starting`/`Started`/`Stopping`/`Unknown` execution left behind by a
hard crash as recoverable, through the public OxideBatch recovery/operator
API only (`JobRepository::recover_job_execution` with a
`RecoveryRequest::mark_failed`) -- never by mutating `oxide_batch` metadata
directly, in production code or in tests. Which execution is recovered is
fully deterministic and never ambiguous: the exact same
`(import_name, source_digest, reader_mode)` identity `run` itself would
resolve right now, with `source_digest` recomputed live from
`app_source.source_customer`'s *current* content under the same
`lock_source_for_stable_read` guard `run`/`verify` use -- never a
caller-supplied digest, and never a "most recent execution for this
import_name" fallback across identities. A source mutated since the crash
therefore makes `recover` fail closed rather than resume stale state (see
scenario 4 above); an already-terminal execution, or a reader mode that
does not match the crashed instance's own identity, likewise fail closed
with a nonzero exit.

### What this does, and does not, prove

This PR demonstrates same-resource atomic chunk durability, checkpoint/
business-row consistency across a real process crash, and deterministic
operator-driven restart behavior, for the specific failure boundaries listed
above -- observed through real database state and real OS-level process
termination, using only released public OxideBatch surface. It is not a
general exactly-once, high-availability, or production-readiness claim, and
it does not attempt every conceivable crash point (e.g. mid-read, or a crash
during the reader's own `ItemStream` checkpoint write outside a chunk
transaction) -- see [Explicit limitations](#explicit-limitations-not-this-pr).

## Commands

```
postgres-postgres migrate --database-url <url>
postgres-postgres seed --database-url <url> --rows <n> --seed <n> [--id-offset <n>]
postgres-postgres run --database-url <url> --import-name <name> --reader cursor|paging [--chunk-size <n>] [--fetch-size <n> | --page-size <n>] [--fail-at-chunk <n> --failure-mode during-write|after-business-commit [--pause-for-kill <path>]]
postgres-postgres recover --database-url <url> --import-name <name> --reader cursor|paging
postgres-postgres verify --database-url <url> --import-name <name>
postgres-postgres reset --database-url <url>
```

`--database-url` also reads `DATABASE_URL` from the environment. `run`'s
`--reader` is required -- there is no default reader mode. `--fetch-size`
is cursor-only; `--page-size` is paging-only; supplying the
mode-incompatible option fails the run rather than silently ignoring it
(see [Reader modes](#reader-modes)). `run`'s `--fail-at-chunk`/
`--failure-mode`/`--pause-for-kill` are deterministic fault-injection flags
(see [Crash / rollback / recovery evidence](#crash--rollback--recovery-evidence-pr-3));
omitted (or `--fail-at-chunk 0`), a run's behavior is unchanged from PR 1/
PR 2. `reset` truncates only `app_source`/`app_business`; it never touches
`oxide_batch`.

## Local development

```
docker compose up -d --wait
cargo run -- migrate
cargo run -- seed --rows 2000 --seed 42
cargo run -- run --import-name demo --reader cursor --chunk-size 200 --fetch-size 128
cargo run -- verify --import-name demo
cargo run -- run --import-name demo_paging --reader paging --chunk-size 200 --page-size 128
cargo run -- verify --import-name demo_paging
```

`ci/validate ci` runs the full contract (format, clippy `-D warnings`,
build, an `.unwrap()`/`.expect()` production-path guard, real-PostgreSQL
integration tests, and a golden-path smoke sequence). `ci/validate msrv`
matches the declared `rust-version` in `Cargo.toml` with a locked,
database-free build.

## Current evidence scope (PR 3)

Proven, with real PostgreSQL 18, in this PR (see
[Crash / rollback / recovery evidence](#crash--rollback--recovery-evidence-pr-3)
above for the full detail; on top of everything PR 1 and PR 2 already
proved, all of which still holds and still passes):

- pre-commit typed rollback, hard-crash-before-commit, and
  hard-crash-after-commit, each for both reader modes, with the whole
  crash/recover/restart cycle driven through real child processes;
- genuine new-OS-process recovery continuation (never the crashed process's
  own memory/state) through the public `recover` command;
- source-mutation isolation across a crash: a mutated source resolves to a
  distinct `JobInstance`, never a silent resume of a stale, differently-keyed
  checkpoint;
- recovered-vs-clean full business projection equivalence, including a
  whole-projection content digest, not merely a row count;
- `recover`'s own negative cases (no instance, already-terminal execution,
  wrong reader mode) fail closed.

Proven in PR 2 (still holds and still passes):

- exact published `oxide-batch = "=0.6.0"` provenance (registry-resolved
  lockfile, unchanged and byte-for-byte identical to PR 1's, no path/git/
  patch substitution -- enforced by the repository's own
  `validate-oxidebatch-provenance.py`);
- clean paging-mode execution through the real `JobLauncher`/`ChunkJob`
  path and the released `postgres_paging_reader`, exactly once per row,
  independently verified (`tests/paging_clean_run.rs`);
- cursor/paging business-result parity: one shared source dataset,
  transformed once under each mode with non-divisible `fetch-size`/
  `page-size`/`chunk-size` values, compared field by field with zero
  divergence in either direction (`tests/reader_parity.rs`);
- paging boundary correctness under a page size smaller than the dataset,
  non-aligned page/chunk sizes, and a non-contiguous (gapped) `customer_id`
  keyset -- no missing, extra, or duplicated rows (`tests/paging_boundary.rs`);
- reader mode as identifying state: the same import name and exact same
  source content, run once per mode, provably resolve to two distinct
  `JobInstance`s, with `reader_mode` itself persisted as an identifying
  parameter in framework metadata (`tests/reader_mode_identity.rs`);
- the database-enforced source-stability guard closing the digest/read
  TOCTOU window under a real adversarial concurrent-write attempt, for
  **both** reader modes independently (`tests/source_stability.rs`);
- configuration negative cases fail closed and are never recorded as a
  completed (or any) job execution: a mode-incompatible option, a zero
  page size, and an omitted `--reader` (`tests/reader_config.rs`).

Still true from PR 1, exercised again here since the shared launch path
changed: deterministic seed/source generation and digest determinism/
sensitivity; the released `PostgresBatchWriter` enlisted in the
same-resource atomic business transaction; independent verification
including three corruption negative controls; collision-free destination
scoping across distinct import names/source identities; workload-owned
`reset` never touching `oxide_batch` metadata.

## Explicit limitations (not this PR)

- **No benchmark, throughput, or resource-usage evidence.** Not attempted
  before the correctness/recovery campaign closes (see `ROADMAP.md`). That
  is PR 4.
- **No larger retained-resource/long-running evidence, and no retained
  evidence manifest or campaign closure.** That is PR 4.
- **Not every conceivable crash point.** This PR's failure boundaries are
  deterministic, chunk/commit-boundary-targeted: post-write/pre-commit
  (typed and hard-killed) and immediately-post-commit. It does not attempt
  e.g. a crash mid-read, mid-page-fetch, or during a reader's own
  `ItemStream` checkpoint write outside a chunk transaction.
- **No Spring Batch comparison, no cross-workload shared framework, no
  central-CI workload-specific branching, no shared/repository-wide failure-
  injection framework.** Out of scope for this campaign entirely (see the
  parent issue's non-goals); `src/failpoint.rs` is an independent,
  workload-local copy of `csv-postgres/src/failpoint.rs`'s established
  shape, not a shared dependency.
- **No production readiness or exactly-once claim.** This PR demonstrates
  same-resource atomic chunk durability, checkpoint/business-row consistency
  across a real process crash, and deterministic operator-driven restart
  behavior for the specific boundaries above -- do not read anything here as
  a broader durability, high-availability, or concurrency guarantee.
- **Upstream [luceat-lux-vestra/oxide-batch#218](https://github.com/luceat-lux-vestra/oxide-batch/issues/218)
  (no `TIMESTAMPTZ` `BusinessValue` variant) remains open and visible.**
  This workload's schema still has no `TIMESTAMPTZ` column anywhere and is
  not worked around here -- see [Schemas](#schemas) above.
