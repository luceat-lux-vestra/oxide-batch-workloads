# Provider effective-side-effect cardinality authority (#318)

## Decision

For the long-running payment-reference campaign, **payment-sandbox v1.5.4
immutable payment timeline is the authoritative provider-side operation
ledger**.

The runtime image is checksum-pinned to payment-sandbox v1.5.4. The source
contract used by this verifier is tag commit
`3b6d6e1864ea5a6d017fcb005f547cf26c37352a`.

The provider exposes the authenticated read-only endpoint:

```text
GET /admin/payments/{providerPaymentId}/timeline
```

It returns current payment state plus immutable business events. In v1.5.4,
payment state and its event are persisted in the same transaction.

## Authority boundaries

| Source | Authority |
| --- | --- |
| payment-sandbox admin timeline | effective provider business side effects |
| payment-sandbox payment snapshot | final provider state and monetary totals |
| Gateway operation journal | canonical caller operation/idempotency outcome |
| Kill Bill/plugin transaction journal | Kill Bill/plugin attempts and results |
| fault proxy counters/history | harness forwarding/blocking/synthetic faults |
| controller execution evidence | request-attempt count and intent/response chronology |

A proxy forwarding count is not a provider financial side effect. A Kill Bill
transaction is not proof that the provider mutated exactly once.

## Cardinality contract

Payment Sandbox creates a new payment at aggregate version 1 and emits
`payment.created`. Every later successful lifecycle transition increments the
aggregate version once and emits exactly one immutable business event.

The verifier therefore requires all of the pinned v1.5.4 invariants before
using the timeline as cardinality evidence:

1. response, payment and event fields match the pinned timeline schema;
2. event count equals the payment aggregate version;
3. version 1 is exactly `payment.created`, and creation appears once;
4. event IDs exactly bind payment ID + event type + aggregate version;
5. event aggregate versions are exactly `1..version`;
6. causation forms an exact previous-event chain;
7. runtime sequences are positive, unique and increasing;
8. every event belongs to the requested provider payment;
9. current status, final event type and monetary lifecycle invariants agree.

Only after those checks:

```text
effective provider mutations = provider version - 1 creation event
```

A 404 from the authenticated timeline endpoint is a separate zero-effect proof:
the provider aggregate does not exist, so creation-event count and effective
mutation count are both zero. Other HTTP statuses fail closed.

## Replay proof

For exact idempotent replay or post-commit response loss:

```text
same timeline digest
+ same effective mutation count
+ same terminal provider state
= no additional provider business side effect
```

Request-attempt cardinality remains separate execution evidence. The provider
ledger deliberately does not infer attempt count from business events.

## Verifier

Present payment:

```bash
python3 payment-reference/deploy/longrun/campaign-provider-cardinality.py \
  --provider-payment-id '<provider-id>' \
  --expected-effective-mutations 1 \
  --expected-status authorized \
  --expected-authorized 1000 \
  --expected-captured 0 \
  --expected-refunded 0
```

Expected provider absence:

```bash
python3 payment-reference/deploy/longrun/campaign-provider-cardinality.py \
  --provider-payment-id '<provider-id>' \
  --expect-absent
```

Both collectors perform authenticated reads inside the `payment-sandbox`
Compose service. They do not use generated container names, do not mutate
provider state and do not put the admin token value on the host command line.

A successful result is deliberately
`PROVIDER_EFFECT_CARDINALITY_VERIFIED_NOT_CAMPAIGN_ACCEPTED`. It is an
independent verification input, never a day/campaign PASS.

## Runtime qualification

The payment-reference runtime smoke proves:

- authorize + two captures + two refunds = five effective provider mutations;
- exact replay/conflicts leave the provider timeline digest unchanged;
- auth-capture/PURCHASE = two provider mutations (authorize + capture);
- authorize + cancel = two provider mutations;
- post-commit response loss + canonical replay leaves one authorize mutation
  and an unchanged provider timeline;
- provider 503 before create has authenticated timeline HTTP 404 before and
  after canonical replay, proving zero provider effects.

This source/runtime qualification does not start or advance the long-running
campaign.
