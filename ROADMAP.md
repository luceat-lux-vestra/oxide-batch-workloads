# Workload Validation Roadmap

**State:** Active

This repository is the external-consumer evidence program for published OxideBatch releases. It validates real workloads against exact crates.io artifacts and uses the resulting evidence to drive framework work. It does not mirror every Spring Batch class or incubate unpublished OxideBatch changes.

## Program principles

1. **Published artifacts only.** Every workload consumes an exact published OxideBatch version from crates.io; no path/git/workspace patch may satisfy validation.
2. **Correctness before throughput.** Final-state correctness, transaction/checkpoint semantics, restart/recovery, resource bounds, and diagnostics gate benchmark claims.
3. **Workloads drive framework changes.** Framework-owned defects or missing contracts are reproduced here, reported upstream, fixed/released there, then revalidated here against the new published artifact.
4. **No consumer-side concealment.** Workarounds remain explicit and never turn a limitation into an unsupported framework claim.
5. **Real failure boundaries.** Restart claims use deterministic failure points and a genuine new process after hard termination where applicable.
6. **Independent verification.** Evidence must detect deliberate corruption of source/final state.
7. **Bounded resources.** Streaming/large-data claims require bounded memory, connections, file handles, tasks, queues, buffers, and metadata growth.
8. **Scheduler/platform agnostic core.** External ecosystems are validated through public OxideBatch contracts. Specific schedulers, control planes, dashboards, secret stores, brokers, or platform operators are not automatically framework features.

## Evidence baseline

Every significant campaign records, as applicable: workload SHA; exact OxideBatch version/registry provenance; lockfile; deterministic dataset identity; chunk/page/fetch size; failure point; toolchain/OS/container/database/broker versions; clean/recovered final-state digests; checkpoint/resume position; reprocessing/duplicates/skips/losses; runtime/CPU/RSS/I/O/connections/transactions; and external-system configuration that materially affects semantics.

Evidence must distinguish framework guarantees, application idempotency, and external-system guarantees.

## Track A — Item I/O and resources

### P0
- [x] CSV/flat-file -> PostgreSQL (`csv-postgres`)
- [ ] PostgreSQL -> PostgreSQL: cursor/streaming reader, keyset/paging reader, batch insert/update/upsert, same-resource transaction/checkpoint semantics, large-data resource bounds, hard-crash/new-process restart.

### P1
- [ ] Multi-resource files -> PostgreSQL: deterministic resource ordering/identity, restart across resource boundaries, changed-source handling.
- [ ] JSON/JSONL <-> PostgreSQL: structured streaming, malformed records, restart-safe output where applicable.
- [ ] Kafka <-> PostgreSQL: offset/checkpoint/business-commit ordering, replay/idempotency, rebalance/backpressure where supported.
- [ ] Object storage/remote resource -> PostgreSQL: object identity/versioning, bounded streaming, changed-object restart behavior.

### P2 / evidence-driven
- [ ] fixed-width flat files
- [ ] XML streaming
- [ ] Avro or another schema-bearing binary format
- [ ] stored-procedure reader/writer
- [ ] AMQP/JMS/NATS/Pulsar/SQS/Redis Streams according to real demand and released support
- [ ] HTTP pagination/streaming source and webhook/effect sink
- [ ] custom reader/writer proving extension APIs without private framework access

Do not mechanically clone the Spring Batch adapter catalog; add workloads for materially different contracts.

## Track B — Processing, flow, and failure semantics

- [ ] transform/map, filtering, validation
- [ ] bounded retry/backoff, skip, rollback/no-rollback where supported
- [ ] deterministic ordering plus ordering-violation detection
- [ ] duplicate/missing-record detection
- [ ] partial writes and chunk-boundary failures
- [ ] pre-commit, post-business-commit, checkpoint, and ambiguous-commit boundaries where meaningful
- [ ] hard kill + new-process restart
- [ ] changed source/input identity and schema incompatibility during resume
- [ ] idempotent vs non-idempotent writers
- [ ] stop/abandon/recover/restart operator semantics
- [ ] multi-step/conditional-flow persisted-decision restart scenarios when released
- [ ] component lifecycle/leak checks

## Track C — Database and metadata portability

PostgreSQL remains the reference database. Portability campaigns must validate semantics, not just connectivity.

- [ ] PostgreSQL across supported majors
- [ ] Oracle enterprise portability
- [ ] MySQL/MariaDB when a released adapter exists
- [ ] SQL Server when a released adapter exists
- [ ] SQLite/other repository modes only when claimed

Each campaign should cover bind/type behavior, decimals/timestamps/time zones, Unicode/large values, cursor/fetch/paging/keyset behavior, generated keys, batch updates/upserts, isolation/locking/deadlocks, optimistic conflicts/concurrent launch, metadata schema/migrations, realistic history query/index behavior, backup/restore, retention/archive/purge, same-resource enlistment, cross-resource delivery semantics, TLS and least-privilege roles.

## Track D — Benchmark program

Benchmarking starts only after comparable correctness/recovery passes.

Comparison layers:
1. raw Rust + direct driver baseline;
2. OxideBatch with equivalent semantics;
3. comparable Spring Batch reference implementation.

Controlled variables: identical logical input/final state, same DB/schema/indexes/durability, same chunk/page/fetch sizes where possible, same host/container limits and DB placement, release builds/production-equivalent JVM settings, recorded runtime/driver/framework versions, startup/warm-up separated from steady state, verification cost equalized or reported separately.

Initial matrix:
- [ ] 100k / 1M / 10M records; larger manual runs where CI cost is excessive
- [ ] chunk sizes 100 / 500 / 1k / 5k
- [ ] cold and warm/repeated runs where meaningful
- [ ] multiple measured runs; report median/distribution
- [ ] clean processing and deterministic ~50% crash/recovery

Metrics: wall-clock/throughput, CPU, peak/steady RSS, connections and transaction/commit counts, commit/ack latency, startup, I/O, recovery duration, reprocessed/duplicated/skipped/lost items, final-state digest equivalence.

Raw-driver results are required before interpreting Rust-vs-Java results.

## Track E — Scalability and concurrency

Only after single-worker semantics and baseline performance are trustworthy:
- [ ] bounded-memory scaling with dataset size
- [ ] repeated-job connection/file/task/metadata retention
- [ ] parallel independent steps/workloads
- [ ] local multi-threaded/chunk processing where released
- [ ] deterministic partition/range ownership and restart
- [ ] bounded prefetch/backpressure/queue depth/slow-sink behavior
- [ ] cancellation/graceful drain/crash-restart under load
- [ ] remote chunking/partitioning/step execution only when published capabilities exist

Distributed campaigns must additionally validate duplicate/delayed messages, leases/fencing, stale workers, coordinator/worker restart, network partitions, artifact/version mismatch, reassignment, bounded credits/queues, and rolling protocol compatibility.

## Track F — Scheduler and orchestrator interoperability

OxideBatch remains scheduler-agnostic. Validate external scheduling through public CLI/operator/API boundaries.

Integration shapes:
- [ ] process-oriented scheduler: cron/systemd timer/Kubernetes CronJob/enterprise scheduler
- [ ] in-process scheduler library via public Rust APIs
- [ ] workflow/orchestrator via process/service boundary

Contract:
- [ ] deterministic job identity and typed parameter mapping
- [ ] external run identity correlated with JobInstance/JobExecution
- [ ] duplicate launch and overlapping-trigger behavior
- [ ] restart vs new-instance semantics
- [ ] machine-readable status/exit categories
- [ ] async launch + bounded polling/inspection
- [ ] stop/cancel and scheduler-visible outcome
- [ ] retry after timeout/lost response without duplicate business effects
- [ ] misfire/backfill handled externally
- [ ] scheduler owns timezone/DST; OxideBatch receives explicit parameters
- [ ] independent scheduler/launcher/batch-process crashes
- [ ] multiple scheduler nodes cannot bypass durable launch/idempotency guards

Do not open a framework issue merely to “add Quartz”; open one only for a missing scheduler-neutral contract.

## Track G — Event-driven launch and completion feedback

Launch sources:
- [ ] file/object arrival
- [ ] SFTP/FTP ingestion boundary
- [ ] Kafka/broker message
- [ ] webhook/HTTP request
- [ ] database/outbox event when realistic

Contract:
- [ ] event identity -> typed JobParameters mapping
- [ ] duplicate/redelivered event -> idempotent launch
- [ ] acknowledgement at a documented safe point
- [ ] launch failure/timeout does not lose the trigger
- [ ] bounded intake/backpressure under bursts
- [ ] validation/security for untrusted trigger metadata
- [ ] explicit ownership of archive/quarantine behavior

Completion/failure feedback:
- [ ] stable completion/failure projection
- [ ] correlation to initiating trigger and OxideBatch execution
- [ ] duplicate notification tolerance
- [ ] notification failure cannot rewrite durable job outcome
- [ ] adapters remain external unless a released generic framework event port is under test

## Track H — Control-plane and automation interoperability

Validate a thin external REST/gRPC wrapper using only public operator/explorer APIs.

- [ ] launch/inspect/list/stop/restart/abandon/recover
- [ ] bounded pagination for large history
- [ ] idempotency keys and optimistic conflicts
- [ ] machine-readable errors without SQL/credentials/raw parameters/payloads
- [ ] deployment-owned auth/RBAC with sufficient external enforcement context
- [ ] concurrent operator requests and lost-response retries

The wrapper is API-usability evidence, not an embedded control-plane commitment.

## Track I — Observability and operational tooling

- [ ] OpenTelemetry-compatible traces/metrics/events where public support exists
- [ ] Prometheus/OTLP reference collection
- [ ] job/step/chunk duration and throughput
- [ ] read/process/write/filter/skip/retry counts
- [ ] active executions, restart/recovery counters, terminal outcomes
- [ ] correlation across scheduler/event/control-plane boundaries
- [ ] bounded cardinality and exporter queues
- [ ] exporter outage cannot corrupt or indefinitely block batch work
- [ ] redaction tests for credentials, raw payloads, SQL values, sensitive parameters
- [ ] diagnostic/root-cause provenance sufficient to explain failed/recovered runs

## Track J — Deployment and process lifecycle

- [ ] standalone/systemd-style process
- [ ] Docker/container
- [ ] Kubernetes Job/CronJob
- [ ] ECS/Nomad/other runtime only for a distinct contract

Scenarios:
- [ ] SIGTERM graceful shutdown and bounded drain
- [ ] forced SIGKILL after deadline + recovery
- [ ] stable process exit codes
- [ ] immutable artifact/version identity
- [ ] config/environment injection and validation
- [ ] external task/container ID correlation
- [ ] restart on another host/container from durable state only
- [ ] resource limits/cgroup pressure/OOM where practical
- [ ] rolling deployment/version mismatch refusal where required

## Track K — Configuration, secrets, and security integration

- [ ] typed config precedence/validation
- [ ] secret injection without secrets in CLI args/evidence/logs/diagnostics
- [ ] Vault/Kubernetes Secret/cloud secret-store only as thin deployment examples
- [ ] credential rotation/reconnect where meaningful
- [ ] TLS/certificate failure diagnostics
- [ ] least-privilege DB/broker roles
- [ ] untrusted path/URL/resource validation
- [ ] no raw PII/business payloads in logs/traces/metrics/evidence
- [ ] reproducible dependency/supply-chain provenance

Auth/RBAC/secret storage remain deployment concerns unless a generic hook is proven missing.

## Track L — Upgrade, migration, retention, and disaster recovery

- [ ] workload dependency upgrade from one published OxideBatch release to the next without silent identity drift
- [ ] metadata schema forward migration and unsupported-newer-version refusal
- [ ] rollback/restore where claimed
- [ ] definition/config fingerprint change across restart
- [ ] large execution-history query behavior
- [ ] archive/purge/retention without breaking restartable executions
- [ ] DB backup/restore followed by inspect/restart/reconciliation
- [ ] corrupt/partial durable state rejection and diagnostics
- [ ] N/N-1 compatibility when published

Spring-to-OxideBatch migration/differential workloads belong here only when the framework reaches that compatibility milestone.

## Track M — Developer-facing extension and test-kit usability

- [ ] custom reader/processor/writer/listener/policy using only public APIs
- [ ] deterministic clean/failure/restart/corruption fixtures
- [ ] published test-kit failure injection ergonomics
- [ ] API/compile-time friction recorded as framework ergonomics findings rather than hidden helpers
- [ ] extension compatibility across releases when the SDK contract stabilizes

## Coverage model

No single workload proves the entire framework.

| Workload/campaign | I/O | Restart/Tx | Integration | Ops | Perf | Portability |
|---|---|---|---|---|---|---|
| `csv-postgres` | CSV -> PostgreSQL | strong | minimal | resource/diagnostic baseline | baseline | PostgreSQL |
| `postgres-postgres` | DB -> DB | primary | operator-ready | metadata/resource | primary benchmark | PostgreSQL first |
| multi-resource | files -> DB | file-boundary | file/object arrival | identity/quarantine | secondary | PostgreSQL |
| Kafka/DB | broker <-> DB | cross-resource | event/messaging | backpressure | secondary | broker/version-specific |
| scheduler/control-plane | launch/control | duplicate/restart | scheduler/API | primary | not primary | repository-specific |
| Oracle campaign | DB -> DB | primary | deployment | operational | comparative | Oracle |
| distributed campaign | remote work | fencing/recovery | transport | primary | scale-out | topology-specific |

## Near-term sequence

1. Keep `csv-postgres` as the v0.6.0 external correctness/recovery reference.
2. Build `postgres-postgres` and drive DB reader/writer/same-resource requirements from real usage.
3. Establish a small reusable measurement harness and raw Rust/sqlx baseline.
4. Add comparable Spring Batch implementation and publish methodology/distributions, not marketing claims.
5. Exercise external launch/control: process scheduler first, then thin control-plane wrapper.
6. Add event-driven file/object/message launch plus completion feedback.
7. Add real observability exporter integration and container lifecycle scenarios.
8. Add multi-resource/structured input coverage.
9. Add Kafka item I/O/cross-resource delivery only after DB transaction/restart semantics are stable.
10. Add Oracle portability before broad enterprise-database claims.
11. Exercise upgrade/migration/retention/DR across the next published release transition.
12. Add distributed execution only after released protocol/fencing/resource contracts exist.

## Spring Batch as comparison baseline

Spring Batch is used for capability discovery and differential scenarios, not as an architecture to copy. Periodically re-audit item readers/writers/item streams; JobRepository/JobOperator/JobExplorer/parameters/restart metadata; retry/skip/flow/testing; local/remote scaling; message-based job launch and feedback; observability; enterprise scheduler integration; metadata migration and operational history.

Update this roadmap when Spring Batch changes materially affect an OxideBatch compatibility goal or reveal a useful external workload contract.

## Framework issue ownership

Open an issue in `luceat-lux-vestra/oxide-batch` only when external evidence shows framework ownership: incorrect semantics/data loss; missing scheduler/event/control-plane-neutral contracts; public API ergonomics forcing internal access; insufficient diagnostics; a missing capability already claimed by the release; demonstrated resource/performance overhead versus a fair raw baseline; or portability/transaction/restart defects belonging to core/adapter abstractions.

Do not open framework issues merely to add a specific scheduler, dashboard, secret manager, orchestration product, or deployment platform.

## Release feedback loop

1. Reproduce externally here.
2. Classify workload bug vs framework bug/missing capability/API ergonomics/docs/diagnostics/compatibility/performance.
3. Open/link a narrow upstream issue when framework-owned.
4. Fix and publish under the framework repository's own gates.
5. Deliberately advance the affected workload to the exact new published dependency.
6. Rerun correctness, crash/restart, resource, interoperability, portability, and relevant benchmark evidence.
7. Update claims only after the released artifact passes.

## When to return to framework-led development

Do not start a broad OxideBatch feature cycle merely because backlog exists. Resume broad framework work when workload evidence identifies a high-impact correctness gap or when repeated independent workloads expose the same missing abstraction, interoperability contract, or operational limitation.
