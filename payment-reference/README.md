# Payment reference system

Coordination owner: `#253`.

The online-path foundation (`#256`) and deterministic failure/async engine
(`#263`) are complete. This directory is the realistic payment-system
reference environment consumed by top-level OxideBatch business workloads; it
remains reference-system substrate and is intentionally not registered as a
workload itself.

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

The merged v1 runtime is a real multi-process payment path:

```text
Deterministic Customer Scenario
  -> Reference Merchant + PostgreSQL
  -> Thin Gateway / UPP + PostgreSQL
  -> Kill Bill 0.24.22 + PostgreSQL
  -> repository-owned PaymentPluginApi
  -> deterministic fault proxy
  -> payment-sandbox v1.5.4 durable provider truth
```

The canonical Merchant online path completed via PR #261
(`main@9884a20e049edf3f64a91146f80443c3ee944f43`). The deterministic external
failure/async-outcome engine completed via PR #264
(`main@b095c2ed495b7e7f8e91238fa5b8e8ca3330e940`).

The runtime proves:

- AUTHORIZE;
- partial and final CAPTURE;
- partial and final REFUND;
- AUTH_CAPTURE / Kill Bill PURCHASE;
- AUTHORIZE -> CANCEL / Kill Bill VOID;
- side-effect-free canonical GET;
- exact replay without a second financial side effect;
- conflicting idempotency and operation identity rejection;
- provider commit followed by response loss without blind duplicate mutation;
- deterministic pre-side-effect failure, stale/not-found visibility, async
  outcome and webhook scheduling through the qualified fault engine.

Ownership remains separated:

- Reference Merchant owns customer/order/payment/business truth and persists
  canonical operation identity before calling the Gateway.
- Gateway owns provider-neutral validation, durable idempotency, deterministic
  Kill Bill identities, payment-scoped serialization and inquiry-before-retry.
- Kill Bill owns Billing payment-transaction truth.
- `adapters/killbill-payment-sandbox` owns Kill Bill/provider translation,
  mutation-scoped provider idempotency, provider-status mapping and the actual
  Kill Bill `PaymentPluginApi`.
- payment-sandbox owns independent provider/PG truth.
- Kill Bill, provider and canonical Merchant/Gateway identities remain distinct.

The reference system is already consumed by two real OxideBatch business
workloads:

- `payment-reconciliation-postgres` — #148 / PR #270, retained supplemental
  OxideBatch 0.6.0 reconciliation/publication evidence;
- `payment-recovery-postgres` — #271 / PR #306, exact OxideBatch 0.7.0
  stale/unknown recovery and duplicate-side-effect safety.

At `main@2f1c57e0bf79f2dd78a9bd4ce5efb8f0bc211a24`, both workloads and the
reference runtime are merged. The next program boundary is the periodized
provider-settlement-statement / Merchant-settlement-close substrate described in
#253, followed by a business-shaped long-running campaign after release and
target-host qualification.

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
- `deploy/` — pinned third-party inputs plus the bounded Merchant -> Gateway ->
  Kill Bill -> payment-sandbox runtime and deterministic fault qualification
  harness.
- `adapters/killbill-payment-sandbox/` — Java 11-compatible Kill Bill OSGi payment plugin
  (built and tested on Java 25 LTS),
  transaction journal DAO, payment-sandbox translation core, and focused
  HTTP/identity/idempotency/binding tests.

## Current program boundary

The next bounded reference-system addition is the settlement-statement substrate
owned by #253: deterministic provider roster, immutable provider settlement
statement, and an idempotent Merchant settlement-close surface.

The first business-shaped long-running campaign follows after:

- the settlement boundary is executable and independently verifiable;
- all OxideBatch workloads contributing to the campaign claim are aligned to one
  exact published OxideBatch release (the current reconciliation workload is
  still 0.6.0 while recovery is 0.7.0);
- the target host is qualified for the full multi-module topology. The current
  runtime is container-oriented; using the Galaxy S10 requires a separately
  proven native/Termux deployment, otherwise a container-capable successor host
  is required.

The long-running controller should reuse the durable campaign/evidence model
already proven by #203 where applicable instead of inventing a second
incompatible endurance format.

Later enrichments, not first-long-run blockers:

- dispute/chargeback and payout flows;
- FX and richer settlement correction/reversal;
- Hyperswitch interoperability;
- Vault activation.
