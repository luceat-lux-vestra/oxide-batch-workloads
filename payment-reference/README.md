# Payment reference system

Issue owner: `#256` under coordination Epic `#253`.

This directory hosts a realistic payment-system reference environment used to
create business state that later OxideBatch workloads can reconcile. It is not
itself an OxideBatch validation workload yet and therefore must not be
registered under `workloads` until an exact published OxideBatch consumer is
introduced.

## v1 topology

```text
Deterministic Customer Scenario
  -> Reference Merchant
  -> Thin Gateway / UPP
  -> Kill Bill
  -> Hyperswitch
  -> payment-sandbox
```

## Current implementation boundary

The Merchant and Gateway are now real HTTP/PostgreSQL processes, but the
Billing/Phub/PG path is intentionally not wired yet.

- Merchant owns customer/order/payment business truth.
- Gateway owns canonical mutation validation and durable idempotency identity.
- A newly accepted Gateway mutation is persisted as
  `PAYMENT_PENDING / PENDING`.
- Replaying the same idempotency key with the same canonical mutation returns
  the retained operation.
- Reusing the same key for a conflicting mutation returns HTTP 409.
- Kill Bill/Hyperswitch/provider IDs are nullable until the next integration
  checkpoints populate them.

Runtime configuration:

```text
MERCHANT_DATABASE_URL
MERCHANT_BIND=127.0.0.1:8081

GATEWAY_DATABASE_URL
GATEWAY_BIND=127.0.0.1:8080
```

Both processes run embedded SQLx migrations on startup.

## Non-negotiable boundaries

- Merchant-facing APIs are provider-neutral.
- Money is integer minor units plus explicit three-letter currency.
- Merchant, Gateway, Kill Bill, Hyperswitch and provider identifiers stay
  distinct and are mapped explicitly.
- Transport failure is not equivalent to business failure.
- `UNKNOWN` is a first-class operation outcome for ambiguous external effects.
- Same idempotency key + conflicting canonical mutation is rejected.
- No workload may query undocumented Kill Bill/Hyperswitch internal tables.
- External OSS is consumed as pinned artifacts, never Git submodules.
- No real PAN/CVV or live PSP credential belongs in this system.

## Project layout

- `api/openapi.yaml` — canonical Merchant -> Gateway HTTP surface.
- `crates/payment-contract/` — provider-neutral domain and identity contract.
- `merchant/` — reference Merchant HTTP process and PostgreSQL schema.
- `gateway/` — thin Gateway/UPP HTTP process and durable idempotency schema.
- `scenarios/v1/` — deterministic scenario identity contract.

## Deferred from this checkpoint

- Merchant -> Gateway HTTP client orchestration.
- Kill Bill/Hyperswitch/payment-sandbox deployment pins.
- Kill Bill <-> Hyperswitch bridge qualification in executable tests.
- Hyperswitch -> payment-sandbox connector.
- End-to-end payment lifecycle.
- broader deterministic fault matrix.
- OxideBatch workloads.
