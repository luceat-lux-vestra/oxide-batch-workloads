# postgres-local-partition

External-consumer qualification for the published `oxide-batch = "=0.6.0"` local-partition runtime. This workload belongs to campaign #93 / Track E #14.

## Contract under test

A deterministic PostgreSQL source is divided into contiguous, non-overlapping ranges. Each OxideBatch partition owns exactly one range and writes the transformed rows into a workload-owned PostgreSQL destination table.

The partition tasklet's business transaction and OxideBatch's durable partition-result commit are separate durable boundaries. This workload therefore does **not** claim `AtomicSameResource` or framework exactly-once for partition business work. The destination primary key plus deterministic upsert makes the final business state replay-safe at the application layer. PR2 explicitly qualifies the replay window rather than inferring exactly-once from the duplicate-free final table.

No PR in this workload publishes a throughput or scaling-performance claim until correctness and recovery gates are accepted.

## PR1: dense correctness qualification

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

## PR2: crash/restart and cancellation qualification

PR2 adds a merge gate over a 1,024-row / 128-partition deterministic source. These are semantic qualification cases, not benchmarks.

### External crash/restart matrix

The harness runs worker budgets `1`, `8`, and `64` at both boundaries:

- `before-write`: the selected partition has not committed workload-owned business rows;
- `after-business-write`: the selected partition's PostgreSQL autocommit write has returned, but OxideBatch has not yet durably published that partition as completed.

For every case, the harness must:

- launch the real workload as a child process;
- wait for a durable PID + semantic-boundary marker;
- deliver an external `SIGKILL` from the harness;
- prove the old PID is gone and the durable execution remains nonterminal;
- inspect both durable partition metadata and workload-owned business rows before recovery;
- use only the published OxideBatch recovery API (`RecoveryRequest::mark_failed` / `recover_job_execution`) through the qualification control binary; no framework-metadata DML is used;
- relaunch in a new process and require a different PID;
- prove partitions already durable as `COMPLETED` retain their original worker-step execution identity;
- prove the unfinished target partition receives a new worker-step execution identity;
- require zero stale/nonterminal executions after continuation;
- independently verify exact final row count, digest, and range ownership.

At `after-business-write`, replay of the target partition is expected. Duplicate-free final business state is attributed to the workload's deterministic upsert/idempotency boundary, not to a framework exactly-once guarantee.

### Source-identity fail-closed case

After an externally killed run, the harness deliberately mutates one source row and requires recovery to refuse the stale durable partition assignment because its recorded source digest no longer matches the live source digest. It then restores the exact original source content, verifies the digest is restored, performs public recovery, and requires normal continuation to pass the independent final-state verifier.

### Cooperative cancellation under load

The cancellation case starts 64 workers behind a qualification-only pre-write hold. CI waits until the workload proves full 64-worker occupancy, then requests stop externally through a file watched by a task that owns the public `StopSource`.

The merge gate requires:

- all 64 workers were concurrently active before the stop request;
- no workload-owned business rows were written before cancellation became terminal;
- the job and partition parent durably end `STOPPED`;
- no stale/nonterminal execution remains;
- the launcher does not return until owned workers drain (`active_workers_after_join == 0`);
- stop-to-process-terminal latency is recorded as an observation only. The harness timeout is a liveness guard, not a product SLA or regression threshold.

Qualification controls are disabled unless their explicit environment variables are set; ordinary `run` / `qualify` execution retains the PR1 path.

## PR3: measurement and ownership instrumentation

PR3 adds a measurement harness without changing the already-qualified `run` path. The canonical intended campaign uses 262,144 rows, 1,024 durable partitions, worker budgets `1 -> 2 -> 4 -> 8 -> 16 -> 32 -> 64`, one warm-up round, and seven cyclic measured rounds.

Each sample is a fresh PostgreSQL database cloned from the same migrated/seeded deterministic template, so framework metadata and business rows do not accumulate across worker points. Scaling ratios use the durable job-execution `created_at -> ended_at` interval read through the published `JobRepository` API; clone, seed, verifier, statistics snapshot, and cleanup are outside that interval.

The harness records paired speedup/efficiency, durable worker-step durations, parent aggregation tail, process-window CPU/RSS, sampled framework/business PostgreSQL session occupancy and lock waits by distinct `application_name`, and `pg_stat_statements` evidence when the manual workflow enables it. Raw artifacts are retained by Actions and are not promoted into canonical repository evidence by this PR.

Measurement limitations are explicit rather than inferred away: process CPU/RSS and `pg_stat_statements` include the workload-owned post-launch verifier; PostgreSQL session values are sampled maxima rather than exact instantaneous peaks and may also include post-launch verifier activity; and business-pool acquire wait is not directly visible without modifying the accepted external-consumer execution path.

Most importantly, PR3 does **not** infer bottleneck ownership from a curve. Generated reports remain `ownership.status = UNKNOWN` and `optimization_issue_allowed = false`; a framework optimization issue requires later profiling/minimal reproduction that isolates framework-owned cost.

## Local reproduction

```bash
# starts PostgreSQL 18 and runs PR1 + PR2 semantic gates plus a bounded PR3 measurement smoke
bash ci/validate ci
```

The workload-owned entrypoint owns its database fixture and semantic smoke sequence. Repository-level CI invokes it only through the stable `ci/validate <ci|msrv>` contract.

For focused PR1 use:

```bash
export DATABASE_URL='postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5435/postgres_local_partition_workload'

cargo run --locked -- migrate
cargo run --locked -- seed --rows 512 --seed 20260907
cargo run --locked -- qualify --prefix local --rows 512 --partitions 64
```

The PR2 harness expects the debug binaries produced by `cargo build --locked --all-targets` and is then run as:

```bash
python3 ci/validate-recovery.py
```

The bounded PR3 smoke is documented in `benchmark/README.md`. The full scaling workflow is manual-only and retains its JSON report as an Actions artifact before any result is considered for promotion.

## Scope not yet qualified

This workload still does not establish:

- a framework-owned scaling bottleneck or optimizer issue;
- a performance regression budget or SLA;
- distributed execution or remote-worker semantics;
- a public performance claim from unreviewed/unretained benchmark output.

Campaign #93 therefore remains open after the measurement harness lands. Numeric output must be reviewed together with ownership evidence before any core optimization issue or README performance claim is justified.
