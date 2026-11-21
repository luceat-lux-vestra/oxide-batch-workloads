# Day-independent payment recovery proof (#318)

This contract turns the legacy day-verification `recovery: PASS + SHA` assertion
into a retained, independently replayable proof for the **actual logical day**.

It reuses the already-qualified #271 / `payment-recovery-v1` semantics, but the
historical #271 validation artifacts are **not** accepted as campaign-day
evidence.

## Authorities

The recovery proof combines three separate authorities:

1. **payment-recovery-postgres workload database**
   - frozen candidate identity and secret-safe digests;
   - processing generation and terminal run state;
   - committed attempts and persisted result.
2. **#271 retained verifier semantics**
   - candidate-set digest;
   - candidate identity;
   - terminal classification;
   - result digest.
3. **authenticated payment-sandbox v1.5.4 timeline**
   - provider identity;
   - terminal provider status and amounts;
   - immutable event/version ledger;
   - effective provider mutation cardinality via the existing pinned verifier.

The recovery workload's own
`duplicate_provider_mutation_count = 0` is never promoted to independent
provider authority. The producer independently re-reads provider timelines and
replays the pinned provider-cardinality verifier.

## Deterministic day binding

The campaign recovery run ID and scenario-set ID are both:

```text
<campaign-id>:day-NNNNNN:recovery
```

Every frozen candidate must belong to one of the exact 100 payment identities
in that day's deterministic traffic plan. In v1, recovery candidates are
restricted to the campaign's fault-overlay `AUTHORIZE` slots.

The verifier re-derives and checks:

- Merchant payment/order identity;
- exact operation ID;
- amount and currency;
- hash of the planned idempotency key;
- hash of the planned payment-method token;
- candidate ID;
- candidate-set digest.

Raw idempotency keys and payment-method tokens are forbidden in retained
recovery evidence.

The workload `business_date` remains the source-query calendar filter used by
`payment-recovery-postgres`; it is retained but is **not** treated as the
logical-day identity. Exact planned payment membership is the authoritative
logical-day boundary. This intentionally fails closed if the workload snapshot
accidentally captures unrelated eligible payments from the same calendar date.

## Terminal result

Every candidate must have one result and at least one committed attempt.
Attempt numbers must be contiguous and processing generations may not move
backwards or exceed the terminal run generation.

The day proof accepts only:

- `RESOLVED_SUCCEEDED`;
- `RESOLVED_FAILED`.

`RETRY_LATER`, `HOLD_MANUAL`, `NOT_ELIGIBLE`, missing results, retained
oracle violations, or a non-`PROCESSED` run block day acceptance.

For the latest attempt, the verifier imports the exact repository
`payment-recovery-postgres/evidence/verify-retained-evidence.py` semantics and
recomputes classification and result digest. The source file SHA-256 is retained
in the artifact, and the exact payment-recovery binary SHA-256 is bound from
campaign provenance.

## Provider cross-binding

For each terminal recovery candidate, the producer performs a fresh authenticated
payment-sandbox timeline read and retains the raw timeline plus the independently
recomputed provider proof. Provider payment identity must match the frozen
candidate and the latest canonical/provider observation.

The day frontier later compares the recovery proof's provider identity, terminal
status, mutation count and timeline digest with the separately produced 100-slot
day-cardinality artifact. This comparison is deliberately performed at the
frontier rather than making either proof producer depend on the other.

For terminal success:

- `AUTHORIZE` requires provider `authorized` with exactly one effective
  provider mutation;
- `AUTH_CAPTURE` would require provider `captured` with exactly two
  effective provider mutations.

For terminal failure, provider `failed` with exactly one effective mutation
and zero successful financial amounts is required.

This contract therefore cannot declare recovery PASS from workload-local state
while independent provider truth says something different.

## Scheduled recovery hard crash

For logical days whose frozen campaign plan schedules
`recovery-after-external-convergence-before-local-commit`, the retained proof
also requires execution evidence for:

- semantic crash marker observed;
- SIGKILL (`9`);
- killed PID proven gone;
- pre-recovery workload state `PROCESSING`;
- target candidate's uncommitted attempt;
- recovery launched in a distinct process;
- same processing generation completed after recovery.

The process/crash record is execution evidence, not cryptographic host
attestation. The independently replayed workload/provider state is what prevents
that record from being sufficient by itself.

Recovery-crash evidence is forbidden on days without that scheduled recovery
crash.

## Retained artifact

Expected path:

```text
days/day-NNNNNN.recovery.verification.json
```

Schema:

```text
oxide-batch-workloads.payment-reference-day-recovery-verification/v1
```

Decision:

```text
RECOVERY_VERIFIED_NOT_DAY_ACCEPTED
```

The producer reads the recovery PostgreSQL service with `psql -X`, selecting
only secret-safe fields, and performs authenticated **read-only** payment-sandbox
timeline requests through the existing Compose service. It calls no
Merchant/Gateway/provider mutation endpoint and never changes workload state. Output uses create-only immutable
retention; exact replay is allowed, conflicting content cannot overwrite the
first proof.

Example after the recovery workload reaches terminal state:

```bash
python3 payment-reference/deploy/longrun/campaign_day_recovery.py \
  --campaign-dir /srv/oxide-batch-workloads/evidence/<campaign-id> \
  --day 1
```

On a scheduled recovery-crash day, add the separately retained execution record:

```bash
  --crash-evidence /srv/oxide-batch-workloads/evidence/<campaign-id>/days/<crash-evidence>.json
```

## Cross-phase truth contract

The fault catalog now freezes two distinct meanings:

- `expected_provider_truth`: online fault-phase observation;
- `end_of_day_provider_truth`: post-recovery EOD observation used by settlement
  roster and day-wide cardinality.

For `plan-503-before-side-effect`, online truth remains `PENDING` with no
provider side effect. The one-shot 503 is then consumed, and payment recovery
replays the exact same operation/idempotency identity. EOD truth is therefore
`AUTHORIZED` with exactly one effective provider mutation. The other fault
classes keep the same provider truth across both phases.

The recovery proof and day-wide cardinality proof independently read provider
timelines. The frontier requires their provider identity, status, mutation
count and timeline digest to agree, so phase confusion cannot be hidden by
either producer.

## Claim boundary

This verifier does not execute the recovery workload, create/freeze candidates,
retry payment mutations, inject a crash, or advance the campaign frontier.

A completed recovery proof is only one of the day-independent verification
inputs. Reconciliation and financial-invariant proof contracts remain separate.
