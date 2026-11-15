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

Candidate local states are:

```text
PAYMENT_UNKNOWN / UNKNOWN
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

The actual outbound replay transport, OxideBatch processing, hard-crash recovery,
retained evidence, and full `verify` command remain later durable work units.

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
