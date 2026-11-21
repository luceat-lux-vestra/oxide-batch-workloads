# Day financial-invariants verification contract v1 (#318)

This contract removes the last legacy `financial_invariants: PASS + SHA`
placeholder from terminal-day verification.

The retained proof is:

`days/day-NNNNNN.financial-invariants.verification.json`

with schema:

`oxide-batch-workloads.payment-reference-day-financial-invariants-verification/v1`

and decision:

`FINANCIAL_INVARIANTS_VERIFIED_NOT_DAY_ACCEPTED`.

## 100-slot raw cross-system evidence

The proof contains exactly one observation for each deterministic day-plan slot.
Every observation retains the raw read-only end-of-day views used by the
verifier:

- Reference Merchant payment view;
- Gateway canonical payment/mapping view;
- Kill Bill payment/transaction view;
- payment-sandbox payment view;
- the independently recomputed per-slot non-acceptance proof.

The verifier rejects reordered, missing, duplicate or foreign Merchant payment
identities.

## Normal 90-slot contract

The four normal payment categories replay the existing pure
`campaign-financial-consistency.py::canonical_consistency` contract:

- 40 auth-capture / purchase;
- 25 authorize + two captures;
- 10 authorize + void;
- 15 purchase + two refunds.

Merchant/Gateway terminal states, distinct Kill Bill/provider mappings, exact
Kill Bill transaction type/status cardinality and provider authorized/captured/
refunded amounts must all agree with the deterministic day plan.

## Fault-overlay 10-slot contract

The existing five deterministic fault classes occur exactly twice per day.

The pure `canonical_fault_consistency` verifier evaluates their **end-of-day**
state, not the online transient state:

- EOD `AUTHORIZED` -> Merchant/Gateway `AUTHORIZED/SUCCEEDED`,
  provider `authorized`, one successful Kill Bill AUTHORIZE;
- EOD `FAILED` -> Merchant/Gateway `PAYMENT_FAILED/FAILED`,
  provider `failed`, one Kill Bill AUTHORIZE with `PAYMENT_FAILURE`.

This does not replace recovery or provider-cardinality authority. It only proves
the retained four-system EOD snapshot is internally consistent.

## Exact day totals

The proof independently derives totals from all 100 deterministic slots.

For the frozen v1 geometry this yields:

- 100 observed lifecycles;
- 90 normal slots;
- 10 fault-overlay slots;
- 98,000 authorized minor units;
- 80,000 captured minor units;
- 15,000 refunded minor units;
- 65,000 net-settlement minor units;
- 80 settlement-eligible provider payments.

The verifier computes these values from the plan rather than trusting the
retained totals.

## Frontier cross-binding

The day frontier does not accept this artifact in isolation.

It additionally requires:

1. every financial provider snapshot to equal the separately retained
   day-cardinality provider identity, status, authorized/captured/refunded
   amounts and provider version;
2. financial captured/refunded/net totals and eligible count to equal the
   independent settlement proof;
3. the stable Merchant-payment/provider-payment pair set to equal the
   independently retained reconciliation snapshot;
4. every recovery candidate represented in the financial proof to agree with
   the recovery result's final canonical payment state, operation state and
   provider state.

These checks prevent a controller from combining individually valid artifacts
from different logical payments or different points in time.

## Authority and execution boundary

`campaign_day_financial_invariants.py` is an offline validator. It does not
perform payment mutations, run batch workloads, activate OCI, or advance the
campaign frontier.

The online controller/collector that performs the 100 read-only Merchant,
Gateway, Kill Bill and provider GETs and writes this retained artifact is a
separate implementation/execution slice.

The existing single-payment command remains a diagnostic read-only probe; a
single successful invocation is not a day PASS.
