# Campaign #93 local-partition scaling harness

This directory contains the **measurement harness only** for the accepted
`postgres-local-partition` external-consumer workload. Correctness, recovery,
and cancellation gates live in `ci/validate` and must stay green before any
numeric artifact is interpreted.

## Canonical shape

The intended retained campaign is:

- exact subject: `oxide-batch = "=0.6.0"` from crates.io;
- PostgreSQL 18;
- 262,144 deterministic source rows;
- 1,024 durable partitions;
- worker budgets `1 -> 2 -> 4 -> 8 -> 16 -> 32 -> 64`;
- one warm-up round;
- seven measured rounds;
- cyclic point rotation so each worker point appears in each run-order position
  once across the seven measured rounds;
- one fresh database cloned from the same migrated/seeded template for every
  sample.

The scaling metric is the published framework's durable job-execution interval
(`created_at -> ended_at`) read through the public `JobRepository` API. Database
clone, migration, seed, verification, statistics snapshot, and cleanup are
outside that interval.

The harness additionally records:

- process-window user/system CPU and peak RSS from `/usr/bin/time` (explicitly
  labelled as including the workload-owned post-launch verifier);
- durable worker-step duration distribution and parent aggregation tail;
- sampled framework and business PostgreSQL session/active/lock-wait maxima by
  distinct `application_name`, after an explicit observer-ready handshake;
- `pg_stat_statements` calls, rows, execution time, and block observations on
  the isolated sample database when enabled;
- exact runner, Rust, PostgreSQL, workload-binary, control-binary, and
  `Cargo.lock` subject provenance;
- per-round paired speedup against that round's one-worker sample and scaling
  efficiency (`speedup / workers`).

## Claim boundary

The report is observational evidence. It contains **no numeric regression
threshold** and does not automatically classify a bottleneck. The generated
ownership block is deliberately `UNKNOWN` with
`optimization_issue_allowed=false`.

A curve, plateau, or regression is insufficient to create a core optimization
issue. Framework ownership requires later profiling and/or a minimal reproducer
that separates PostgreSQL cost, locks, business SQL, connection pressure,
repository metadata/CAS, scheduler/runtime behavior, and partition skew.

Three limits are intentionally explicit rather than guessed:

- business-pool acquire wait is not directly observable without changing the
  already-qualified external-consumer run path;
- `pg_stat_statements` and `/usr/bin/time` process windows include the
  workload-owned verifier after the durable job has completed;
- PostgreSQL session values are sampled maxima at the configured interval, not
  exact instantaneous peaks, and the observer remains active until the unchanged
  `run` process returns, so those samples may also include post-launch verifier
  activity.

## Local bounded smoke

After `cargo build --locked --all-targets` and with the normal PostgreSQL fixture
running, the protected CI contract executes a small smoke campaign. For manual
use:

```bash
python3 benchmark/scale.py \
  --binary target/debug/postgres-local-partition \
  --measurementctl target/debug/postgres-local-partition-measurementctl \
  --base-database-url "$DATABASE_URL" \
  --rows 64 --partitions 8 --seed 20260908 \
  --warmups 0 --measured-runs 1 --worker-points 1,2,4,8 \
  --output /tmp/postgres-local-partition-scale-smoke.json
```

The canonical GitHub Actions workflow additionally enables and requires
`pg_stat_statements`; protected CI does not change its PostgreSQL runtime flags
for the sake of benchmarking.
