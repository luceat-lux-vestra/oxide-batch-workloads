# PostgreSQL -> PostgreSQL comparative benchmark

Campaign: #73
Primary Track: #13

This directory contains comparison implementations for the correctness-complete
`postgres-postgres` OxideBatch workload. It is not a third validation workload
and is not registered independently in `workloads.json`.

## Comparison class

The authoritative class is `semantic-parity-minimal-durability`.

The raw Rust/sqlx implementation is intentionally **not** a "fastest possible
PostgreSQL" program. It preserves the correctness-critical work needed to make
framework-overhead attribution meaningful:

- the same deterministic `app_source.source_customer` input;
- the same canonical streaming source digest and source-stability SHARE lock;
- the same strict keyset ordering contract (`customer_id`);
- both validated reader shapes: bounded server-side cursor FETCHes and bounded
  keyset pages with no `OFFSET`;
- the same deterministic transform and `app_business.customer_projection`
  representation;
- the same 7 bound destination values per row;
- ordinary multi-row `INSERT ... VALUES`, not COPY;
- the OxideBatch 0.6.0 writer parity bound of 2,000 parameters, therefore at
  most 285 rows / 1,995 binds per full statement;
- chunk-level atomicity between destination business writes and the raw
  implementation's durable checkpoint;
- deterministic typed post-write/pre-commit rollback behavior;
- real external `SIGKILL` coverage both before and after the atomic chunk
  commit, followed by continuation in a newly spawned OS process;
- fail-closed source-identity handling so a changed source cannot silently
  reuse or fork stale checkpoint state under the same raw import identity.

The raw implementation deliberately does not reproduce OxideBatch's broader
JobRepository lifecycle/history/diagnostic feature set. Its checkpoint lives in
`benchmark_raw.*`; it never reads or mutates framework-owned `oxide_batch.*`
metadata. Consequently, raw metadata volume and lifecycle feature breadth are
not semantically comparable metrics.

## PR 1: paging parity and durability core

PR 1 established the initial raw comparison subject:

- bounded keyset pages with no `OFFSET`;
- separate in-memory read position and durable committed position;
- streaming source identity;
- same-shape bounded writer;
- atomic raw checkpoint + business commit;
- clean execution and typed post-write/pre-commit rollback validation against
  real PostgreSQL;
- final-state verification through the existing independent
  `postgres-postgres verify` path.

PR 1 also kept migration/setup as an explicit `migrate` command. `run` never
performs migration, preserving the campaign rule that setup work stays outside
the timed interval.

## PR 2: cursor parity and real crash recovery

PR 2 extends the same raw durability core rather than creating a second
implementation:

- `--reader cursor|paging` is explicit and part of checkpoint identity;
- cursor mode opens a dedicated PostgreSQL transaction, executes
  `DECLARE ... NO SCROLL CURSOR`, and advances through bounded
  `FETCH FORWARD <fetch_size>` batches;
- cursor resume declares a fresh cursor strictly after the last durable
  `customer_id`; the source cursor transaction is distinct from destination
  write/checkpoint transactions;
- paging keeps its independent bounded keyset-page path;
- the PR 1 `benchmark_raw.paging_checkpoint` table is migrated in place to the
  reader-neutral `benchmark_raw.checkpoint` name without touching
  `oxide_batch.*`;
- reader modes use distinct definition revisions so one mode's durable state
  cannot be reinterpreted as the other's;
- a hard-death failpoint writes a marker containing the real child PID and then
  only pauses. The CI parent waits for that exact marker and sends `SIGKILL`;
  the program never self-aborts or self-signals;
- the before-commit crash point occurs after both business INSERTs and the raw
  checkpoint UPSERT have executed inside the still-open transaction. A kill
  there must roll back both;
- the after-commit crash point occurs immediately after PostgreSQL commit
  returns. A kill there must preserve both;
- continuation is launched as a new raw-sqlx OS process and must reach the
  independently verified final representation without missing, extra, or
  duplicate rows;
- a deliberate source mutation after a crash must be rejected before any new
  business/checkpoint effect; restoring the original source identity then
  permits continuation from the original durable prefix.

The crash controls are correctness-test instrumentation only. They are never
part of a retained performance sample.

## Dependency and CI boundary

`raw-sqlx` is a nested Cargo workspace member of `postgres-postgres`. It shares
`postgres-postgres/Cargo.lock`, so the existing registered workload's locked
supply-chain scan covers its dependency graph rather than allowing a nested
unscanned lockfile.

Protected workload CI proves that:

- `raw-sqlx` has no direct or transitive `oxide-batch*` dependency;
- its production source contains no `.fetch_all(` whole-dataset call;
- it does not access framework-owned `oxide_batch.*` metadata;
- cursor mode retains explicit server-side `DECLARE`/`FETCH` semantics;
- the hard-death path contains no self-abort/self-exit/self-signal shortcut;
- workspace format/clippy/build/test and the declared Rust 1.95 MSRV build
  include the raw member;
- clean paging and cursor output both pass the independent verifier;
- typed rollback preserves only the previously committed prefix and resumes
  correctly;
- real SIGKILL before commit rolls back business rows and checkpoint together;
- real SIGKILL after commit preserves business rows and checkpoint together;
- both cursor and paging recover in new OS processes to independently verified
  final state;
- stale checkpoint reuse after source mutation fails closed.

GitHub Actions remains the authoritative verification source. Local builds are
only optional fast feedback.

## Remaining campaign work

No number produced by PR 1 or PR 2 is a performance claim. The next #73 slice
builds the same-host paired measurement/report harness and only then produces
fresh authoritative-main raw-vs-OxideBatch comparison evidence.
