# Java attribution controls for PostgreSQL -> PostgreSQL

This directory is campaign #79's JVM-side comparison boundary. It is nested
under the existing `postgres-postgres` workload because it reuses that
workload's canonical PostgreSQL 18 source schema, deterministic generator,
business transformation, destination representation, and independent Rust
verifier. It is not a new OxideBatch workload.

PR1 contains only `raw-jdbc/`. Spring Batch is intentionally absent until the
raw Java control and its dependency/supply-chain boundary pass strict review.

## Frozen PR1 contract

- Java: 21 LTS. Protected CI fails if the selected runtime is not Java 21 and
  records the concrete `java -version` / `mvn -version` output in the job log.
- PostgreSQL JDBC: exact `org.postgresql:postgresql:42.7.13`.
- No Spring or OxideBatch dependency in the raw JDBC module.
- No Maven `SNAPSHOT`, version range, `LATEST`, `RELEASE`, property-indirected
  dependency/plugin version, or custom repository.
- Source identity is the same ordered streaming SHA-256 used by the Rust
  workload and raw-sqlx control.
- The source table is held under `LOCK ... IN SHARE MODE` from digest start
  through the protected read, preventing a digest/read TOCTOU window.
- Cursor mode uses pgjdbc cursor fetching prerequisites: `autoCommit=false`,
  a forward-only result set, ordered query, and positive `fetchSize` (500 by
  default).
- Paging mode is bounded keyset pagination on unique `customer_id`, 750 rows
  by default, with no `OFFSET`.
- Chunk size defaults to 1000.
- Primary writer parity is ordinary multi-row `INSERT ... VALUES`: 7 bound
  columns, at most 2000 parameters, 285 rows / 1995 binds per full statement,
  and therefore at most four statements for a 1000-row chunk. JDBC
  `executeBatch`, PostgreSQL `COPY`, and `reWriteBatchedInserts` are forbidden.
- Raw durability metadata lives only in `benchmark_java.raw_checkpoint`.
  Business rows plus checkpoint advancement commit atomically in one JDBC
  transaction.
- `--fail-after-chunk N` is a typed pre-commit failure used only for rollback
  evidence. External SIGKILL/new-process recovery belongs to PR3.

## Local build

The benchmark reactor is Maven-based:

```text
mvn -B -ntp -f postgres-postgres/benchmark/java/pom.xml verify
```

Runtime database credentials are supplied through environment variables, not
command-line arguments:

```text
RAW_JDBC_DATABASE_URL=jdbc:postgresql://localhost:5434/postgres_postgres_workload
RAW_JDBC_DATABASE_USER=oxide_batch_workload
RAW_JDBC_DATABASE_PASSWORD=oxide_batch_workload
```

`migrate` creates only `benchmark_java.*`; canonical source/business schemas
remain owned by the existing workload migration. `run` never performs setup or
migration, keeping setup outside future timed intervals.

The canonical final-state oracle remains `postgres-postgres verify`. Java does
not implement a second verifier.

## Supply-chain coverage

The repository-wide Cargo scan remains unchanged for the Rust workload graph.
Because this directory adds a nested Maven ecosystem, the central
`supply-chain` validator now fails closed if Maven manifests exist without the
workload-owned executable `ci/validate-supply-chain` hook. The hook enforces
this Java manifest boundary. Protected ordinary workload CI additionally
resolves the Maven runtime dependency tree and builds the reactor on Java 21.
GitHub `dependency-review` remains the diff-scoped dependency gate, and
Dependabot has a dedicated Maven entry for this reactor.

No performance number emitted by PR1 is campaign evidence or a performance
claim. Four-way measurement starts only in PR4 after raw Java and Spring
correctness/recovery obligations pass.
