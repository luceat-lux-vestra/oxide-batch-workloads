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
  -> reference payment adapter
  -> payment-sandbox
```

The adapter is the first-slice Phub/connector boundary. Full Hyperswitch is a
deferred interoperability lane, not a v1 runtime dependency.

## Current implementation boundary

The Merchant and Gateway are real HTTP/PostgreSQL processes. The repository-owned
Kill Bill payment plugin is validated as an OSGi bundle and the bounded
Kill Bill -> payment-sandbox runtime topology is now defined under `deploy/`.
Merchant/Gateway -> Kill Bill orchestration remains deferred.

- Merchant owns customer/order/payment business truth.
- Gateway owns canonical mutation validation and durable idempotency identity.
- `adapters/killbill-payment-sandbox` owns deterministic Kill Bill/provider
  identity translation, mutation-scoped provider idempotency identities,
  payment-sandbox v1.5.4 HTTP calls, provider-status mapping, and ambiguous
  transport classification.
- The adapter now includes the actual Kill Bill `PaymentPluginApi`, OSGi
  registration, transaction journal DAO, MySQL/PostgreSQL DDL, and binding
  tests.
- Ambiguous provider mutation transport failures become Kill Bill
  `PaymentPluginStatus.UNDEFINED`, not a false business failure.
- Completed Kill Bill transaction replay is served from the journal; conflicting
  replay identity fails closed before a provider mutation.
- A newly accepted Gateway mutation is persisted as
  `PAYMENT_PENDING / PENDING`.
- Replaying the same idempotency key with the same canonical mutation returns
  the retained operation.
- Reusing the same key for a conflicting mutation returns HTTP 409.
- Kill Bill/provider/backend IDs are nullable until the next integration
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
- Merchant, Gateway, Kill Bill and provider identifiers stay distinct and are
  mapped explicitly.
- Optional backend identities such as Hyperswitch IDs remain distinct if that
  interoperability lane is activated later.
- Transport failure is not equivalent to business failure.
- `UNKNOWN` is a first-class operation outcome for ambiguous external effects.
- Same idempotency key + conflicting canonical mutation is rejected.
- No workload may query undocumented Kill Bill or payment-backend internal
  tables.
- External OSS is consumed as pinned artifacts, never Git submodules.
- No real PAN/CVV or live PSP credential belongs in this system.

## Project layout

- `api/openapi.yaml` — canonical Merchant -> Gateway HTTP surface.
- `crates/payment-contract/` — provider-neutral domain and identity contract.
- `merchant/` — reference Merchant HTTP process and PostgreSQL schema.
- `gateway/` — thin Gateway/UPP HTTP process and durable idempotency schema.
- `scenarios/v1/` — deterministic scenario identity contract.
- `deploy/` — exact third-party qualification/pinning and runtime inputs.
- `adapters/killbill-payment-sandbox/` — Java 11 Kill Bill OSGi payment plugin,
  transaction journal DAO, payment-sandbox translation core, and focused
  HTTP/identity/idempotency/binding tests.

## Deferred from this checkpoint

- Merchant -> Gateway HTTP client orchestration.
- authoritative runtime smoke evidence for the new Compose topology.
- Merchant/Gateway -> Kill Bill orchestration.
- end-to-end payment lifecycle.
- broader deterministic fault matrix and webhook matrix.
- Hyperswitch interoperability lane.
- OxideBatch workloads.
