# Payment reference system

Issue owner: `#256` under coordination Epic `#253`.

This directory hosts a realistic payment-system reference environment used to
create business state that later OxideBatch workloads can reconcile. It is not
itself an OxideBatch validation workload yet and therefore must not be
registered under `workloads` until an exact published OxideBatch consumer is
introduced.

## v1 topology

The selected v1 online-payment path is:

```text
Deterministic Customer Scenario
  -> Reference Merchant
  -> Thin Gateway / UPP
  -> Kill Bill 0.24.22
  -> repository-owned PaymentPluginApi
  -> payment-sandbox v1.5.4
```

The repository-owned Kill Bill payment adapter is the first-slice
Phub/connector boundary. Full Hyperswitch remains a qualified but deferred
interoperability lane and is not a v1 runtime dependency.

## Current implementation boundary

Gateway -> Kill Bill -> repository-owned payment plugin -> payment-sandbox is
implemented and authoritatively validated.

The validated PR head
`31ecd4573c50e4fc0d6ed153fa0472a64dbd8093` passed GitHub Actions CI run
`43364373618` (#572), including Reserved Cargo Projects, the full
payment-reference runtime smoke, workload aggregates, and Merge Gate. It was
squash-merged as
`bb8fa6b0b8a110a033330a2a13ede75ecc7d5039`.

The runtime smoke proves:

- AUTHORIZE;
- partial and final CAPTURE;
- partial and final REFUND;
- AUTH_CAPTURE / Kill Bill PURCHASE;
- AUTHORIZE -> CANCEL / Kill Bill VOID;
- side-effect-free canonical GET;
- exact replay without a second Kill Bill transaction;
- conflicting idempotency and operation identity rejection;
- provider commit followed by response loss without blind duplicate mutation.

The implementation boundary is intentionally narrower than the complete #256
owner:

- Reference Merchant is a real HTTP/PostgreSQL process and owns
  customer/order/payment business truth. On the current #256 candidate it also
  persists canonical operation identity before calling the Gateway and writes
  canonical outcomes back into Merchant state.
- Gateway is a real HTTP/PostgreSQL process and owns canonical request
  validation, durable idempotency, deterministic Kill Bill identities,
  payment-scoped serialization, inquiry-before-retry, and canonical state
  recovery.
- `adapters/killbill-payment-sandbox` owns deterministic Kill Bill/provider
  identity translation, mutation-scoped provider idempotency identities,
  payment-sandbox HTTP calls, provider-status mapping, ambiguous transport
  classification, the actual Kill Bill `PaymentPluginApi`, OSGi registration,
  and its transaction journal.
- Ambiguous provider mutation transport failures become Kill Bill
  `PaymentPluginStatus.UNDEFINED`, not a false business failure.
- Successful provider cancellation is reported as a successfully processed Kill
  Bill VOID; `PaymentPluginStatus.CANCELED` is not used to represent a
  successful provider-side cancellation.
- Kill Bill payment/transaction UUIDs, provider identities, and canonical
  Merchant/Gateway identities remain separate.

The Reference Merchant -> Gateway boundary is now implemented on the current
#256 candidate branch. Merchant payment operations are persisted before the
outbound call, canonical Gateway outcomes are written back into Merchant payment
and operation state, and Gateway/Kill Bill/provider identities are retained in
Merchant mappings/events.

This candidate has **not yet received authoritative PR/CI validation**, so the
last merged and validated runtime remains #260. The candidate runtime harness
contains the deterministic provider-503-before-side-effect scenario and now
materializes one machine-readable v1 scenario-result JSON after all bounded
runtime assertions pass. Broader deterministic failure/async-outcome coverage
remains deferred until the later owner.

Runtime configuration:

```text
MERCHANT_DATABASE_URL
MERCHANT_GATEWAY_BASE_URL=http://127.0.0.1:8080
MERCHANT_BIND=127.0.0.1:8081

GATEWAY_DATABASE_URL
GATEWAY_BIND=127.0.0.1:8080

KILLBILL_BASE_URL
KILLBILL_USERNAME
KILLBILL_PASSWORD
KILLBILL_API_KEY
KILLBILL_API_SECRET
```

Merchant and Gateway run embedded SQLx migrations on startup. The bounded
Compose runtime under `deploy/` additionally owns local Kill Bill tenant
bootstrap for smoke qualification; the per-payment Gateway path does not create
or rotate tenants.

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
- `gateway/` — thin Gateway/UPP HTTP process and durable idempotency/recovery
  implementation.
- `scenarios/v1/` — deterministic scenario identity contract plus the
  machine-readable v1 result schema.
- `deploy/` — pinned third-party inputs and the bounded Gateway -> Kill Bill ->
  payment-sandbox runtime qualification harness.
- `adapters/killbill-payment-sandbox/` — Java 11 Kill Bill OSGi payment plugin,
  transaction journal DAO, payment-sandbox translation core, and focused
  HTTP/identity/idempotency/binding tests.

## Deferred from the current #256 checkpoint

- authoritative PR/CI/runtime validation of the Merchant -> Gateway,
  provider-503-before-side-effect, and retained result-artifact candidates;
- broader deterministic fault and async/webhook matrix;
- Hyperswitch interoperability lane;
- OxideBatch business workloads and release-evidence campaigns.
