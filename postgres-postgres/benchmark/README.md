# PostgreSQL -> PostgreSQL comparative benchmark

Campaign: #73
Primary Track: #13

This directory contains comparison implementations and measurement tooling for
the correctness-complete `postgres-postgres` OxideBatch workload. It is not a
third validation workload and is not registered independently in
`workloads.json`.

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
  the raw program never self-aborts or self-signals;
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
part of a retained performance sample except where PR 3 deliberately arms the
pre-commit hard-death point for the recovery measurement described below.

## PR 3: same-host paired measurement/report harness

`benchmark/paired.py` is the campaign measurement harness. It measures raw sqlx
and OxideBatch in the **same process-hosting GitHub Actions job/runner**, against
one PostgreSQL 18 service and one committed benchmark implementation/config.
The workflow is manual-only:

`.github/workflows/benchmark-postgres-postgres-paired.yml`

The canonical retained configuration is:

- rows: `1,000,000`;
- seed: `20260904`;
- chunk size: `1000`;
- cursor fetch size: `500`;
- paging page size: `750`;
- warmup pairs: `2` per reader mode;
- measured pairs: `7` per reader mode;
- recovery: `1` deterministic pre-commit hard-death pair per reader mode.

Workflow inputs may use bounded overrides for diagnostic runs, but every
effective value is retained in the JSON report. A non-canonical diagnostic run
is not campaign acceptance evidence.

### Fresh database isolation

The harness does not repeatedly reset one long-lived execution repository and
pretend later samples are fresh. Before timing, it creates one deterministic
**template database** from `template0`, applies both the normal OxideBatch
workload migration and the benchmark-owned raw migration, and seeds the source
exactly once.

Each warmup, measured candidate, and recovery candidate then receives a fresh
PostgreSQL database cloned from that template. Database creation/cloning,
migration, deterministic seed generation, teardown, and independent final
verification all remain outside timed intervals.

This gives raw and OxideBatch samples the same initial source/schema state while
preventing a previous sample's framework metadata, raw checkpoint, destination
rows, or completion state from leaking into the next one.

### Paired order and clean timing

Within each pair, candidate order alternates deterministically:

1. OxideBatch -> raw sqlx;
2. raw sqlx -> OxideBatch;
3. repeat.

Warmups use the same pairing/order policy but are excluded from measured
summaries.

A clean candidate's timed interval is only its normal `run` command. Source
digest calculation remains inside that interval because both candidates perform
it as part of normal execution. Compilation, PostgreSQL startup, database clone,
migration, seed, and verification are not timed.

Every candidate sample must pass the existing independent
`postgres-postgres verify` implementation. Paired candidates must also report
the same source digest. An incorrect run is a failed campaign sample, regardless
of speed.

### Recovery timing

For each reader mode the harness also measures one deterministic restart pair.
The target chunk is chosen so the last durable prefix before the killed chunk is
near 50% of the source rows. The same semantic boundary is used for both
candidates: real business writes have happened inside the target chunk's still-
open transaction, but the chunk has not committed.

- OxideBatch uses its public workload failpoint with `during-write` plus
  `--pause-for-kill`;
- raw sqlx uses `before-commit` plus `--pause-marker`;
- the child writes its real PID to a marker;
- the harness sends `SIGKILL` to that exact PID rather than using a timing
  guess;
- the killed PID must no longer exist before recovery starts;
- the durable business prefix is checked outside either implementation's
  private metadata;
- OxideBatch's public `recover` command is included as a separately timed
  operator phase because it is required by the released framework contract;
- raw sqlx needs no operator-recovery command;
- continuation runs in a new command/process and must pass the independent
  verifier.

The report retains first-phase time, optional operator-recovery time,
continuation time, combined active processing time, durable position after the
kill, reprocessed rows, and zero duplicate/skipped/lost counts after successful
verification.

The expected reprocessed rows are the uncommitted target chunk. This is not a
claim that the two implementations have equal metadata/lifecycle work; it is a
correctness/accounting measure for the deliberately equivalent pre-commit crash
boundary.

### Report and metrics

The JSON artifact is observational evidence and has no numeric pass/fail
throughput or RSS threshold. It records, where reliable:

- wall-clock elapsed time;
- rows/second;
- GNU `time` user/system CPU and peak RSS;
- committed rows/chunks;
- derived writer statement/sub-batch counts and effective bind counts under the
  pinned 7-column / 2,000-parameter writer contract;
- independent source/destination digests and correctness verdict;
- candidate exit status;
- candidate binary SHA-256;
- exact GitHub commit/run/attempt/runner identity;
- rustc/cargo versions;
- raw sqlx 0.9.0 Cargo.lock source/checksum;
- exact OxideBatch 0.6.0 Cargo.lock source/checksum;
- OS/kernel/CPU/memory;
- PostgreSQL configured image, server version, and image id.

Clean summaries contain min/median/max/p95 distributions for elapsed,
throughput, CPU, and peak RSS plus paired raw/Oxide ratios.

Transaction/commit/rollback counts are represented explicitly as
`not-reliably-observed`/`null` rather than invented. The campaign did not find a
symmetric, non-perturbing observation path that would make those counts honest
for both candidates.

Recovery timing includes the small external harness reaction interval between
observing the marker and sending `SIGKILL`; the same mechanism is used for both
candidates and the limitation is retained in the report.

### Retention and failure behavior

The manual workflow uploads the JSON artifact even if the campaign fails, so a
partial report and failure reason survive diagnosis. The artifact retention
period is 30 days. GitHub-hosted-runner numbers are observational distributions,
not a merge-blocking performance threshold.

Only a **fresh run from authoritative `main` after PR 3 merges** can become the
canonical #73 performance evidence. PR CI's small database smoke and any branch
workflow run are semantic harness validation only.

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
- stale checkpoint reuse after source mutation fails closed;
- the paired Python harness's report/order/input/security logic passes unit
  tests;
- while PostgreSQL is live in protected CI, a bounded 401-row debug-binary
  semantic smoke executes the entire paired harness for cursor+paging,
  raw+Oxide clean runs, and both recovery paths. These debug measurements are
  discarded as performance evidence; only their semantic success matters.

GitHub Actions remains the authoritative verification source. Local builds are
only optional fast feedback.

## Remaining campaign work

PR 1 and PR 2 establish correctness/durability parity; PR 3 establishes the
measurement mechanism. None of their protected-CI smoke timings is a campaign
performance claim.

After PR 3 merges and authoritative-main aggregate CI is green:

1. dispatch the paired benchmark workflow from fresh authoritative `main` with
   the canonical configuration above;
2. review the retained JSON artifact under the same proof-obligation gate;
3. record run id, commit SHA, artifact identity/hash, environment/config,
   correctness verdicts, distributions, paired ratios, and limitations in #73;
4. close #73 only if every acceptance criterion is supported by that evidence.
