# Provider-side operation cardinality authority (#318)

Status: **design gate only — no provider-side authority exists in the deployed v1.5.4 binary.**
This document is not evidence of cardinality, a verification PASS, or permission to start the campaign.

## Source review (payment-sandbox upstream)

The reference stack pins `proxynth/payment-sandbox` v1.5.4 as a downloaded binary
in `deploy/payment-sandbox/Dockerfile`. Its Go sources are **not vendored** in this
workloads repository. The upstream SQLite `payments` repository stores the
current amounts/status/version with optimistic concurrency; the event log stores
business events and runtime sequence; the idempotency repository stores
scope/key/fingerprint/replay response. These are three distinct authorities.

The upstream HTTP handler wraps payment commands in a database transaction and
uses an idempotency reservation/replay wrapper. Successful authorize/capture/
refund/cancel commands update the aggregate and publish a business event.
None of the inspected v1.5.4 APIs exposes an independently qualified
`provider_operation_ledger` snapshot. A final payment GET, event count,
idempotency record, Kill Bill plugin journal, and fault-proxy forwarding count
must **not** be presented as equivalent to a provider financial-effect count.

## Required upstream change (payment-sandbox, not the workloads harness)

Introduce an immutable `provider_operation_effects` table in the **same SQLite
transaction** that commits a successful financial aggregate mutation. Its
producer must live at the provider command/persistence boundary, not at the
HTTP proxy or caller. A row represents **one committed effective financial
transition**, not one request, retry, event delivery, or replay.

Proposed v1 fields:

| Field | Authority / invariant |
| --- | --- |
| `effect_id` | Unique immutable provider-generated ID |
| `provider_payment_id` | Provider aggregate ID |
| `provider_operation_id` | Stable logical mutation identity, not correlation ID |
| `operation_type` | AUTHORIZE, CAPTURE, CANCEL, REFUND |
| `idempotency_scope`, `idempotency_key` | Request identity; never the only effect identifier |
| `amount_minor`, `currency` | Actual committed delta and currency |
| `aggregate_version_before`, `aggregate_version_after` | Strictly +1 for a successful transition |
| `effect_ordinal` | Monotonic per-payment committed-effect ordinal |
| `result` | COMMITTED only; rejected/rolled-back attempts belong in a separate attempts log |
| `committed_at`, `runtime_sequence` | Transactional provenance/order |

Require UNIQUE(`provider_payment_id`, `effect_ordinal`) and UNIQUE(
`provider_payment_id`, `aggregate_version_after`); use a stable unique
operation identity for each effective transition, with explicit semantics for
partial captures/refunds and multiple distinct legitimate operations.
Do **not** make `idempotency_key` globally unique across scopes.

The effect insert, payment aggregate save, business-event append and
idempotency completion must commit or roll back together. A transaction failure
must leave **zero** effect rows. A same-key replay must return the original
response without inserting another effect. A different key following an
ambiguous response must not silently create a duplicate financial effect.

### Read-only observation API

Expose a loopback/Compose-internal read-only endpoint (exact route to be agreed
in the upstream API), scoped to one provider payment ID, returning:
- provider release/source/image identity and SQLite schema version;
- payment ID and current aggregate version;
- complete ordered effect rows and explicit `complete=true` watermark;
- monotonic runtime sequence or snapshot watermark, captured atomically with
  the rows; no pagination truncation;
- a canonical SHA-256 digest over the complete snapshot.

The observer must not perform mutation, recovery, replay or implicit repair.
Missing/partial data, gaps, duplicate ordinals, version discontinuity, digest
mismatch or mismatched provider identity are **BLOCK**, never zero effects.

### Independent workloads verifier (subsequent bounded unit)

Only after a pinned provider release with this API is deployed:
1. Read the provider ledger through the named Compose service, not a generated
   container name; require exact qualified image/source provenance.
2. Verify the complete watermark, row schema, uniqueness, ordinal/version
   continuity and snapshot digest; compare terminal aggregate amounts with
   signed committed deltas.
3. Join the independently retained Merchant/Gateway/Kill Bill mapping and
   operation intents by stable operation IDs. No inferred join from timing.
4. For response-loss/replay, require **one effective transition** for the
   same logical mutation despite multiple HTTP attempts; for distinct valid
   partial operations, require the exact separately authorized cardinality.
5. Write a verifier-owned artifact referencing the immutable raw snapshot
   digest and source identity. Only the independent campaign finalizer may
   include a PASS in terminal day evidence.

## Acceptance matrix (must run against real upstream provider)

| Case | Provider ledger expectation |
| --- | --- |
| Single authorize/capture/refund/cancel | One committed effect per successful mutation |
| Same-key replay | No additional effect; same stored response |
| Post-commit response loss | Committed effect exists once; caller ambiguity cannot erase it |
| Concurrent duplicate requests | Exactly one effective transition, others replay/conflict |
| Transaction rollback / injected failure | Zero committed effect rows |
| Partial captures/refunds | Distinct valid operations, each counted once; totals conserved |
| Provider restart / SQLite reopen | Immutable effects and order survive |
| Tampered/truncated snapshot | Independent verifier BLOCK |

## Execution boundary

Do not modify the pinned Docker image to an unqualified upstream build, query
SQLite through a shell as a substitute for a stable provider API, or label
existing business-event data as an operation ledger. The next implementation
unit must be performed in the **upstream payment-sandbox repository**, then
released/pinned and independently qualified before a workloads verifier can
make a financial cardinality claim. Until then `side_effect_cardinality`
remains explicitly **unverified** and #318 cannot advance its campaign frontier.
