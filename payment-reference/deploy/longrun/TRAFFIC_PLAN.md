# Deterministic Merchant online traffic plan (#318)

This is the **pure planning** part of the next bounded two-logical-day
payment-reference campaign smoke. The source of day identity, date, seed and
the 100-primary-per-day category counts is the already merged
`campaign-contract-v1.json` / `campaign_core.py`.

## Operator-neutral plan

```bash
python3 payment-reference/deploy/longrun/campaign-traffic-plan.py \
  --campaign-id payment-campaign-318 --day 1
python3 payment-reference/deploy/longrun/campaign-traffic-plan.py \
  --campaign-id payment-campaign-318 --day 2
```

These commands only print JSON to stdout. **They do not send HTTP requests,
create Merchant rows, inject faults, run reconciliation, settle a statement,
write evidence, or activate the campaign.**

The plans provide 100 deterministically ordered and uniquely identified
Merchant payment lifecycles per logical day, with 200 disjoint payment/order
identities across days one and two:

| Category | Daily count | Planned Merchant operations |
| --- | ---: | --- |
| Purchase | 40 | auth-capture |
| Authorization/capture | 25 | authorize, capture 600, capture 400 |
| Authorization/void | 10 | authorize, cancel |
| Purchase/refund | 15 | auth-capture, refund 250, refund 750 |
| Fault overlay | 10 | authorize, with separately selected fault-engine script |

All flows use synthetic provider tokens, USD and 1,000 integer minor units.
Requests are shaped for the **Merchant API** (`/merchants`, `/customers`,
`/orders`, `/orders/{id}/payments` and `/payments/{id}/...`), not the
Gateway-only smoke shortcut. The flow records include deterministic
idempotency/operation keys, so later runner retries must explicitly reconcile
prior states rather than invent a new financial mutation identity.

The fault overlay rotates across the five qualified **classes** represented by
the v1 fault matrix, twice per logical day:

1. 503 before provider side effects
2. Provider commit followed by response loss
3. Async success with stale visibility/convergence
4. Async failure
5. Webhook ordering/duplicate/drop/retry

The mapping is now frozen in `fault-catalog-v1.json`. The catalog owns each
class's exact scenario case, seed, fault-engine steps, caller outcome, expected
provider/canonical outcome and reconciliation requirements. Both the runtime
fault-matrix smoke and the campaign planner consume this catalog, so a future
controller must render the same catalog entry rather than re-declare fault
semantics. The planner records the catalog SHA-256 and still makes no terminal
state claim for an overlay.

Planning order is SHA-256 rank over the frozen day seed, kind and
kind-local ordinal, avoiding wall-clock or host-random ordering. The plan
records an explicit `executable=false` and
`external_verification_required=true` to prevent treating this planning
artifact as proof of paid/cancelled/refunded provider truth.

## Separate execution and evidence gate

A future bounded driver must combine this plan with:

- one safe Merchant/customer create step for each day, with read-before-retry
  and durable event recording;
- one ordered mutation sequence per primary payment, without reissuing
  ambiguous mutations blindly;
- the qualified fault proxy and its deterministic fault plans;
- the exact published OxideBatch 0.7.0 recovery and reconciliation workloads;
- independently read provider settlement roster and immutable statement;
- Merchant settlement close plus replay/conflict/cardinality checks;
- actual durable day success only after independent Merchant/Gateway/Kill Bill/
  provider verification, exact per-day financial totals and quiescence.

**A 2-day traffic plan is not the two-day smoke.** Do not use the synthetic
fixtures in this PR to advance the campaign frontier or count an accepted day.
Do not run on the existing OCI digest bundle until source/image/provenance
identity has been reconciled.

## Tests

```bash
python3 payment-reference/deploy/longrun/test-campaign-fault-catalog.py
python3 payment-reference/deploy/longrun/test-campaign-traffic-plan.py
```

The focused tests verify 100/day, 200 collision-free cross-day identities,
all Merchant request shapes, deterministic operation/idempotency keys,
correct partial/final amount breakdown, fault-class frequency and
cross-process identical canonical output, and invalid campaign/day rejection.
