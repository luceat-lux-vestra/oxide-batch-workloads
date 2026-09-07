# postgres-local-partition

External-consumer correctness qualification for the published `oxide-batch = "=0.6.0"` local-partition runtime. This workload belongs to campaign #93 / Track E #14.

## Contract under test

A deterministic PostgreSQL source is divided into contiguous, non-overlapping ranges. Each OxideBatch partition owns exactly one range and writes the transformed rows into a workload-owned PostgreSQL destination table.

The partition tasklet's business transaction and OxideBatch's durable partition-result commit are separate durable boundaries. This workload therefore does **not** claim `AtomicSameResource` or framework exactly-once for partition business work. The destination primary key plus deterministic upsert makes the final business state replay-safe at the application layer; crash-window replay semantics are qualified separately in a later campaign slice.

PR1 is a correctness qualification only. It publishes no throughput or scaling-performance claim.

## PR1 qualification

Protected CI uses a bounded deterministic dataset of 512 rows and 64 partitions, then executes the same workload at worker budgets:

`1 -> 2 -> 4 -> 8 -> 16 -> 32 -> 64`

Every worker point must prove:

- completed job, parent step, and all durable partition rows;
- the exact deterministic durable partition-key set and range contexts;
- exact source/destination row-count equality;
- independent streamed SHA-256 equality for the expected transformed destination and the actual destination;
- no missing source identity and no extra/duplicate destination identity;
- contiguous range ownership with no overlap or gap;
- observed peak worker occupancy equals the configured point and no worker remains active after the parent returns;
- normalized durable state is identical to the one-worker baseline apart from run identity/timing.

The qualification also proves a PostgreSQL repository pool one connection below the released `workers + 1` requirement fails closed with `InsufficientPoolCapacity` before any job instance or child worker is created.

Finally, CI deliberately corrupts one destination row and requires the independent verifier to reject it.

## Local reproduction

```bash
# starts PostgreSQL 18 on localhost:5435
bash ci/validate ci
```

The workload-owned entrypoint owns its database fixture and semantic smoke sequence. Repository-level CI invokes it only through the stable `ci/validate <ci|msrv>` contract.

For focused use:

```bash
export DATABASE_URL='postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5435/postgres_local_partition_workload'

cargo run --locked -- migrate
cargo run --locked -- seed --rows 512 --seed 20260907
cargo run --locked -- qualify --prefix local --rows 512 --partitions 64
```

## Scope not yet qualified

PR1 intentionally does not establish:

- external process-kill restart semantics under concurrency;
- graceful cancellation/drain latency under load;
- throughput, speedup, scaling efficiency, CPU, RSS, connection-wait, or PostgreSQL bottleneck ownership;
- a public performance claim.

Those remain later gates in campaign #93. Correctness and recovery qualification precede retained numeric measurement.
