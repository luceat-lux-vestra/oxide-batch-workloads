# Java attribution controls for PostgreSQL -> PostgreSQL

This directory is campaign #79's JVM-side comparison boundary. It is nested
under the existing `postgres-postgres` workload because it reuses that
workload's canonical PostgreSQL 18 source schema, deterministic generator,
business transformation, destination representation, and independent Rust
verifier. It is not a new OxideBatch workload.

The reactor contains two deliberately separate candidates:

- `raw-jdbc/`: the PR1 raw Java/JDBC control;
- `spring-batch/`: the PR2 Spring Batch 6.0.5 candidate.

The raw module must remain free of Spring and OxideBatch dependencies. The
Spring module must remain free of OxideBatch dependencies. Neither candidate
is a performance claim by itself.

## Frozen JVM-side contract

- Java: 21 LTS. Protected CI fails if the selected runtime is not Java 21 and
  records the concrete `java -version` / `mvn -version` output in the job log.
- PostgreSQL JDBC: exact `org.postgresql:postgresql:42.7.13`.
- Spring Batch: exact stable `org.springframework.batch:spring-batch-core:6.0.5`.
- Spring Batch 6.0.5's published graph reaches `org.jspecify:jspecify` 1.0.0
  through Spring Framework and 1.0.1 through Micrometer. The Spring module has
  one scoped `dependencyConvergence` exception for exactly that coordinate and
  immediately applies `requireUpperBoundDeps` to exactly the same coordinate,
  so Maven must select the published upper bound rather than silently allowing
  an arbitrary convergence escape. No other convergence exception is allowed.
- No Maven `SNAPSHOT`, version range, `LATEST`, `RELEASE`, property-indirected
  dependency/plugin version, custom repository, profile, or build extension.
- Source identity is the same ordered streaming SHA-256 used by the Rust
  workload and raw-sqlx control.
- The source table is held under `LOCK ... IN SHARE MODE` from digest start
  through the protected read, preventing a digest/read TOCTOU window.
- Cursor mode uses pgjdbc cursor fetching prerequisites: `autoCommit=false`,
  a forward-only result set, ordered query, and positive `fetchSize` (500 by
  default).
- Paging mode is bounded PostgreSQL keyset paging on unique `customer_id`, 750
  rows by default, with no `OFFSET`.
- Chunk size defaults to 1000.
- Primary writer parity is ordinary multi-row `INSERT ... VALUES`: 7 bound
  columns, at most 2000 parameters, 285 rows / 1995 binds per full statement,
  and therefore at most four statements for a 1000-row chunk. JDBC
  `executeBatch`, PostgreSQL `COPY`, and `reWriteBatchedInserts` are forbidden.

## Raw JDBC durability and hard-death recovery

Raw durability metadata lives only in `benchmark_java.raw_checkpoint`.
Business rows plus checkpoint advancement commit atomically in one JDBC
transaction. `--fail-after-chunk N` is a typed pre-commit failure used for the
PR1 rollback/continuation proof.

PR4 closes the remaining raw-Java hard-death obligation before any four-way
performance evidence is accepted. Test-only `--pause-at-chunk`, `--pause-phase`
and `--pause-marker` are all-or-none controls. They create an exclusive marker
containing the live JVM PID and wait passively; the candidate never exits,
aborts, or signals itself. `before-commit` fires after business INSERTs and the
raw checkpoint UPSERT while the JDBC transaction is still open. `after-commit`
fires only after `Connection.commit()` returns successfully. The CI parent
sends `SIGKILL` to the exact marker PID and requires status 137.

Protected real-PostgreSQL coverage proves cursor and paging before/after-commit
prefixes (200/300 rows on the 550-row semantic fixture), continuation in a
genuinely new JVM, source-mutation rejection without checkpoint/business drift,
and final equivalence through the independent Rust verifier. Raw JDBC has no
separate operator-recovery phase: continuation reloads its benchmark-owned
durable checkpoint in the new JVM.

## Spring Batch durability and crash recovery

Spring Batch owns only `spring_batch.*` metadata, initialized from Spring
Batch's official PostgreSQL schema script. Migration fails closed on a partial
metadata schema instead of attempting to repair it implicitly. The candidate
uses a real JDBC `JobRepository`, `StepExecution`, and reader `ExecutionContext`.
It never performs direct Spring metadata DML.

Both Spring readers persist restart state. The cursor candidate uses
`JdbcCursorItemReader`; the paging candidate uses `JdbcPagingItemReader` with a
`PostgresPagingQueryProvider` and unique `customer_id` sort key. The custom
`ItemWriter` uses `JdbcTemplate` on the same `DataSource` and transaction
manager as the Spring step, so business writes and the framework checkpoint
share the chunk transaction. It never commits or rolls back privately.

PR2 proves typed rollback and public restart. `--fail-after-chunk N` injects a
typed failure after business writes but before the chunk commit; re-running the
same identifying job parameters resumes through
`JobOperator.restart(JobExecution)` without duplicates or skips.

PR3 adds real external-process death evidence. The candidate never kills,
aborts, or exits itself. Test-only `--pause-at-chunk`, `--pause-phase`, and
`--pause-marker` controls only create an exclusive marker containing the live
JVM PID and then wait passively for the CI parent to send `SIGKILL`. The
`before-commit` marker is emitted after business SQL while the chunk transaction
is still open. The `after-commit` marker is emitted from Spring transaction
`afterCommit`, after the chunk transaction has successfully committed.

An externally killed Spring execution remains non-terminal. A separate JVM uses
public `JobOperator.recover(JobExecution)` to mark that execution failed; a
third JVM then uses the normal public restart path. CI proves both crash phases
for cursor and paging readers, validates the durable 200/300-row prefixes, and
finishes against the independent Rust verifier. Before either a new run or
public recovery, the candidate pages existing JobRepository history for the
same import/reader/definition identity and rejects a changed source digest.
This prevents a mutated source from silently becoming a new JobInstance after
a crash.

## Local build

The benchmark reactor is Maven-based:

```text
mvn -B -ntp -f postgres-postgres/benchmark/java/pom.xml verify
```

Runtime database credentials are supplied through environment variables, not
command-line arguments. Raw JDBC uses `RAW_JDBC_DATABASE_*`; Spring Batch uses
`SPRING_BATCH_DATABASE_*`.

Candidate `migrate` commands only create candidate-owned durability metadata;
canonical source/business schemas remain owned by the existing workload
migration. `run` never performs setup or migration, keeping setup outside
future timed intervals.

The canonical final-state oracle remains `postgres-postgres verify`. Java does
not implement a second verifier.

## Supply-chain coverage

The repository-wide Cargo scan remains unchanged for the Rust workload graph.
Because this directory contains a nested Maven ecosystem, the central
`supply-chain` validator fails closed if Maven manifests exist without the
workload-owned executable `ci/validate-supply-chain` hook. The hook enforces the
reviewed Maven manifest and Java source inventory, adversarially tests the
single-coordinate Spring convergence exception, guards Spring and raw-JDBC
external crash/recovery controls against self-termination or transaction-
boundary drift, and executes the four-way report/order/parser policy tests.
Protected workload CI also resolves both Java runtime dependency trees and
builds the reactor on Java 21. GitHub `dependency-review` remains the
diff-scoped dependency gate. Frozen comparison subjects such as pgjdbc and
Spring Batch are advanced only by an explicit validation campaign, not routine
dependency churn.

Four-way measurement starts only in PR4; PR1, PR2, and PR3 remain correctness,
recovery, and parity evidence rather than performance evidence.

No number emitted by PR1, PR2, PR3, PR4 branch CI, or the bounded PR4 semantic
smoke is campaign performance evidence. The four-way methodology and manual
canonical workflow are documented in `../FOUR_WAY.md`; accepted numbers can
come only from a fresh canonical run on authoritative `main` after PR4 is
merged and its post-merge gates are green.
