# Payment recovery PostgreSQL workload

Issue owner: #271  
Parent Track: #11  
Coordination Epic: #253  
Release-evidence horizon: milestone #4, `OxideBatch 0.7 External Validation`

This workload is the first payment-reference business campaign frozen against the
new OxideBatch 0.7 release family. It validates stale/unknown initial-payment
recovery without treating batch completion as proof of business convergence.

## Exact published subject

```toml
oxide-batch = { version = "=0.7.0", features = ["postgres"] }
```

Published crates.io provenance frozen at activation:

- crate: `oxide-batch`;
- version: `0.7.0`;
- yanked: `false`;
- Rust MSRV: `1.95`;
- checksum:
  `cb884d99629b92fc6740a187a2ebb65350f8924a57f0c8103bad26192f17652e`.

The committed `Cargo.lock` resolves `oxide-batch`,
`oxide-batch-core`, `oxide-batch-plan`, and
`oxide-batch-repository` at exact registry version 0.7.0.

## Frozen first slice

Only ambiguous initial mutations are eligible:

- `AUTHORIZE`;
- `AUTH_CAPTURE`.

Candidate local states are. The Reference Merchant currently maps retryable
upstream failures to `PAYMENT_UNKNOWN / RETRY_PENDING`, while the canonical
Gateway state may also expose `PAYMENT_PENDING / RETRY_PENDING`:

```text
PAYMENT_UNKNOWN / UNKNOWN
PAYMENT_UNKNOWN / RETRY_PENDING
PAYMENT_PENDING / RETRY_PENDING
```

`MANUAL_REVIEW` is not automatically replayed.

Recovery must reuse the exact original Merchant payment/operation/idempotency
identity. It must never generate a new financial identity merely because a
previous response or local checkpoint was lost.

## Workload-owned state

Migration `0001_init.sql` creates:

- `payment_recovery_run`;
- `payment_recovery_candidate`;
- `payment_recovery_attempt`;
- `payment_recovery_result`.

Sensitive replay inputs may exist only in workload-owned ephemeral database
state when required. Raw payment-method tokens and raw idempotency values must
never be emitted into retained evidence or logs.

## CLI contract

Checkpoint 3 freezes these commands:

- `migrate`;
- `snapshot-candidates`;
- `run`;
- `recover`;
- `verify`;
- `reset`.

`migrate` and deterministic `snapshot-candidates` are implemented through
checkpoint 4. The candidate snapshot uses a repeatable-read Merchant transaction,
selects only the latest eligible initial operation older than the explicit logical
cutoff, freezes exact replay inputs, and records secret-safe digests plus a
canonical candidate-set digest.

Checkpoint 4 also establishes two pure foundations consumed by later work:

- `replay::MerchantReplayClient` prepares the exact supported Merchant endpoint
  and request body without generating a new operation/idempotency identity, and
  fail-closes canonical response identity mismatches;
- `oracle::classify` independently maps frozen candidate identity + canonical
  observation + provider truth + duplicate-side-effect evidence into
  `RESOLVED_SUCCEEDED | RESOLVED_FAILED | RETRY_LATER | HOLD_MANUAL`.

Checkpoint 5 implements the actual processing path:

- `postgres_paging_reader` reads frozen candidates in `candidate_id` keyset order;
- the OxideBatch JobInstance identity includes `processing_generation`, so a
  completed `RETRY_LATER` cycle can run again after deterministic provider
  visibility advances without pretending to restart a completed JobInstance;
- the processor performs exact Merchant replay, then supported provider
  `GET /payments/{provider_payment_id}` observation;
- transport-unknown and retryable HTTP outcomes remain `RETRY_LATER`; they are
  never guessed as success or failure;
- the custom writer uses only OxideBatch's enlisted `BusinessTransaction`
  port and writes the recovery attempt plus current result in the same
  PostgreSQL chunk transaction that commits the framework checkpoint;
- each attempt stores canonical/provider identities, visibility, status and
  financial observations. Raw replay secrets are never copied into attempt or
  result audit rows;
- result rows remain `verified = false` until the independent retained-evidence
  checkpoint proves side-effect cardinality.

Checkpoint 6 adds deterministic hard-crash/new-process recovery mechanics.
Retained evidence and the full `verify` command intentionally remain the next
durable work unit.

## Correctness boundary

The external Merchant/provider financial effect is not transactionally enlisted
with the workload PostgreSQL/OxideBatch checkpoint transaction.

Later checkpoints must prove the cross-resource invariant through exact replay
identity, downstream idempotency, and independent provider truth:

```text
external success + lost local checkpoint
  -> restart may repeat the recovery call
  -> repeated call uses exact original identity
  -> duplicate provider financial side effect == 0
```

Do not query or mutate undocumented Merchant/Gateway/Kill Bill/provider internal
state to make recovery pass.

## Checkpoint 5 transaction boundary

The item processor performs the external Merchant/provider calls before the
chunk writer is enlisted. The external financial system therefore cannot share
the PostgreSQL transaction.

Inside PostgreSQL, however, the boundary is atomic:

```text
processor observation
        |
        v
RecoveryWrite
        |
        v
OxideBatch enlisted BusinessTransaction
        |
        +-- INSERT payment_recovery_attempt
        |
        +-- UPSERT payment_recovery_result
        |
        +-- framework checkpoint / component state
        |
        v
single chunk commit or rollback
```

A crash after the external call but before this commit can legitimately replay
the same frozen Merchant operation identity. Proving that this never duplicates
the provider financial effect is checkpoint 6, not something checkpoint 5
claims.

A completed cycle with `RETRY_LATER` results may start a later
`processing_generation`; terminal results are excluded from the next reader
query. A run left in `PROCESSING` or `RECOVERING` is not allowed to advance
generation and must use checkpoint-6 recovery instead.

## Checkpoint 6 crash/restart boundary

Hard-crash validation uses in-process HTTP rather than a spawned `curl`
process. This is deliberate: a child process could outlive a SIGKILLed workload
and continue a financial request after the workload PID disappeared, making the
crash point ambiguous for the wrong reason. The checkpoint-6 transport uses a
bounded Tokio TCP/HTTP client so the external request lifecycle belongs to the
same OS process that the harness kills.

The crash harness exposes four deterministic points for one frozen candidate:

```text
after-candidate-read-before-replay
while-replay-outcome-unknown
after-external-convergence-before-local-commit
after-local-write-before-checkpoint-commit
```

The `while-replay-outcome-unknown` point pauses after the HTTP request bytes
have been flushed but before the workload reads the Merchant response. The
process is then SIGKILLed by the harness. Recovery must therefore treat the
request outcome as unknown and must not infer whether a financial effect
occurred.

`recover` does not increment `processing_generation`. It:

1. requires workload state `PROCESSING | RECOVERING`;
2. reconstructs the exact current JobInstance key for the existing generation;
3. inspects the latest OxideBatch execution;
4. for an active/unknown execution, records an explicit
   `RecoveryRequest::mark_failed` through the public repository API;
5. accepts an already-`FAILED` execution only when an audited recovery
   decision already exists;
6. treats an already-`COMPLETED` execution as a local post-commit bookkeeping
   gap and only repairs workload run state;
7. relaunches the exact same generation with the same frozen candidate/replay
   identity.

The executable `ci/recovery-matrix.sh` matrix uses fresh Reference Merchant
payments and real SIGKILL/new-process recovery. For each crash point it checks
the intended runtime assertions:

- the killed run remains `PROCESSING`;
- no workload attempt row escaped an uncommitted chunk;
- recovery remains on `processing_generation = 1`;
- exactly one workload attempt/result is committed after recovery;
- the candidate converges to `RESOLVED_SUCCEEDED`;
- provider `/authorize` forward count stays exactly one across the repeated
  exact replay;
- a later invocation does not replay an already terminal
  `RESOLVED_SUCCEEDED` candidate.

The matrix also checks the separate completed-`RETRY_LATER` path: a later
normal processing cycle advances to a new `processing_generation`, rather
than misclassifying that cycle as hard-crash recovery.

This checkpoint defines and wires the executable matrix into
`ci/validate ci`; a PASS from that matrix is not claimed until the executable
validation checkpoint runs it. Retained evidence construction and negative
corruption controls remain checkpoint 7.
