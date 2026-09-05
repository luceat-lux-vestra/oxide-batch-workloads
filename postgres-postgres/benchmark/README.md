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
- the same keyset ordering contract (`customer_id`);
- the same deterministic transform and `app_business.customer_projection`
  representation;
- the same 7 bound destination values per row;
- ordinary multi-row `INSERT ... VALUES`, not COPY;
- the OxideBatch 0.6.0 writer parity bound of 2,000 parameters, therefore at
  most 285 rows / 1,995 binds per full statement;
- chunk-level atomicity between destination business writes and the raw
  implementation's durable checkpoint;
- deterministic post-write/pre-commit rollback behavior.

The raw implementation deliberately does not reproduce OxideBatch's broader
JobRepository lifecycle/history/diagnostic feature set. Its checkpoint lives in
`benchmark_raw.*`; it never reads or mutates framework-owned `oxide_batch.*`
metadata. Consequently, raw metadata volume and lifecycle feature breadth are
not semantically comparable metrics.

## PR 1 scope

PR 1 establishes only paging parity and the durability core in
`raw-sqlx/`:

- bounded keyset pages with no `OFFSET`;
- separate in-memory read position and durable committed position;
- streaming source identity;
- same-shape bounded writer;
- atomic raw checkpoint + business commit;
- clean execution and post-write/pre-commit rollback validation against real
  PostgreSQL;
- final-state verification through the existing independent
  `postgres-postgres verify` path.

Cursor mode, hard process death/new-process recovery, source-mutation recovery
checks, and paired performance measurement belong to later #73 PRs. No PR 1
number is a performance claim.

## Dependency and CI boundary

`raw-sqlx` is a nested Cargo workspace member of `postgres-postgres`. It shares
`postgres-postgres/Cargo.lock`, so the existing registered workload's locked
supply-chain scan covers its dependency graph rather than allowing a nested
unscanned lockfile.

Protected workload CI additionally proves that:

- `raw-sqlx` has no direct or transitive `oxide-batch*` dependency;
- its production source contains no `.fetch_all(` whole-dataset call;
- it does not access framework-owned `oxide_batch.*` metadata;
- workspace format/clippy/build/test and the declared Rust 1.95 MSRV build
  include the raw member;
- clean paging output passes the independent verifier;
- an injected failure in a later chunk preserves the previously committed
  prefix while making neither that failing chunk's business writes nor its
  checkpoint advancement durable; a subsequent run resumes from the durable
  prefix and reaches independently verified final state.

GitHub Actions remains the authoritative verification source. Local builds are
only optional fast feedback.
