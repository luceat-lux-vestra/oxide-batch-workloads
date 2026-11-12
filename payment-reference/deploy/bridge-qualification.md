# Kill Bill payment bridge qualification

Owner: #256

Qualified upstream:

- repository: `juspay/hyperswitch-killbill-plugin`
- upstream commit: `11433a53e1d0e92c9216e43e5e31ac02f606f23d`
- source POM version: `0.1.1`
- last published release: `version-0.1.0`
- upstream last commit observed: 2024-03-18

Additional alternative reviewed:

- repository: `deepanshu-iiitu/killbill-prism-plugin`
- observed repository history: one `init` commit (`36962154...`, 2026-05-20)

## Result

**DO NOT CREATE A SEPARATE FORK FOR v1.**

The upstream Hyperswitch plugin cannot be consumed unchanged, but maintaining a
fork is not the smallest path to the #256 objective. Full Hyperswitch would
also require a new Hyperswitch -> `payment-sandbox` connector, so the fork is
only the first of two custom integration surfaces.

For the first payment-reference slice, use a repository-owned thin Kill Bill
payment adapter that talks directly to the pinned `payment-sandbox` HTTP API.

This adapter is the v1 Phub/connector boundary. Hyperswitch remains a later
interoperability lane and can be reintroduced after the first business workload
has produced useful validation signal.

## What the Hyperswitch plugin source actually shows

The upstream plugin already contains Kill Bill entry points for:

- AUTHORIZE;
- CAPTURE;
- PURCHASE;
- VOID;
- REFUND;
- payment-info refresh.

It is nevertheless unsuitable unchanged.

### Currency mapping

`convertCurrency` explicitly maps only:

`USD, AUD, CAD, DKK, EUR, GBP, NZD, SEK`.

The Hyperswitch Java client itself contains `KRW`; the missing piece is only
the plugin's enum mapping. This is not foreign-exchange conversion.

More importantly, #256 does not require KRW as a first-slice acceptance
condition. The canonical contract requires an explicit currency, not a
Korea-specific currency. v1 can use USD for deterministic fixtures and add KRW
only when a Korea-specific scenario actually needs it.

Therefore **KRW is not a v1 fork blocker**.

### Self-hosted endpoint

`hyperswitch-java-client 0.0.4` defaults its base path to
`https://sandbox.hyperswitch.io` and exposes `setBasePath(...)`.

The Kill Bill plugin constructs `HsApiClient` with only API-key material and
never calls `setBasePath`. Its configured `environment` value is not applied
to the client construction path.

A self-hosted Hyperswitch deployment therefore requires source change or an
equivalent adapter layer.

### Kill Bill identity persistence

AUTHORIZE and PURCHASE persist the supplied Kill Bill payment and transaction
IDs.

CAPTURE, VOID and REFUND instead pass `kbPaymentMethodId` in positions used by
the earlier operations for payment / transaction identity. That is not
acceptable for a reference system whose reconciliation model explicitly keeps
payment, transaction and payment-method identity separate.

### PENDING refresh

`getPaymentInfo` initializes `wasRefreshed = false`, performs refresh work
for pending transactions, but never flips the flag. It can therefore return the
pre-refresh list after updating storage.

### Webhook entry point

`processNotification` returns `null`.

This matters for the later webhook matrix, but it is **not** a blocker for the
first #256 happy path. #256 already allows delayed/duplicate/out-of-order/lost
webhook coverage to move to the next bounded owner.

### Client age / compatibility

The plugin depends on `hyperswitch-java-client 0.0.4`, generated in early
2024. The selected router release is Hyperswitch v1.129.0.

Compatibility would need executable proof before this path could be trusted.

## Alternatives considered

### 1. Narrow fork of `juspay/hyperswitch-killbill-plugin`

Rejected for v1.

The code fixes themselves are small, but the resulting architecture still
requires:

1. maintaining the Kill Bill -> Hyperswitch fork;
2. qualifying the old Java client against v1.129.0;
3. running the stateful Hyperswitch router stack;
4. implementing and maintaining a Hyperswitch -> `payment-sandbox` connector.

That is too much integration ownership before OxideBatch has received any new
business-workload validation signal.

### 2. Pinned upstream + local patch overlay

Technically viable and preferable to a permanent fork if full Hyperswitch is
reintroduced later.

It would allow a reproducible build from an exact upstream commit plus
repository-owned patch digests. It does not, however, remove the second custom
connector or the full-router runtime cost, so it is not selected for v1.

### 3. `killbill-prism-plugin`

Not selected for v1.

The reviewed public repository is not an official Juspay/Kill Bill repository,
has a single observed commit, and currently documents Stripe/Adyen only. Its
Prism SDK is not available from the normal Maven Central path in the reviewed
setup, requires a native Rust/JNA library build, and still has no
`payment-sandbox` connector.

It is an interesting future interoperability candidate, not a lower-risk first
slice.

### 4. Repository-owned Kill Bill -> `payment-sandbox` adapter

**Selected for v1.**

The pinned `payment-sandbox v1.5.4` API already supplies the exact business
surface needed here:

- caller-supplied payment identity;
- authorize;
- capture;
- cancel;
- refund;
- status retrieval;
- integer minor-unit money;
- `Idempotency-Key` replay/conflict semantics;
- correlation identity;
- durable async work and deterministic failure scenarios.

A thin Kill Bill `PaymentPluginApi` implementation can translate only the
required v1 operations and keep provider-specific concerns below the canonical
Merchant/Gateway contract.

## v1 adapter acceptance

The repository-owned adapter must prove:

- AUTHORIZE;
- CAPTURE;
- PURCHASE / auth-capture semantics;
- VOID / CANCEL;
- REFUND;
- GET / payment-info convergence;
- distinct Kill Bill payment / transaction / payment-method identities;
- deterministic provider identity mapping;
- mutation-scoped idempotency with no duplicate provider side effect;
- USD fixture support using integer minor units;
- no real PAN/CVV or live PSP credential;
- no public/cloud payment endpoint reachable by default.

Webhook ingestion and Hyperswitch interoperability remain explicitly deferred.

## Reintroduction rule for Hyperswitch

Reconsider full Hyperswitch only after the direct reference path and first
OxideBatch business workload are working.

At that point, add a separate interoperability lane if the additional
independent Phub state materially improves a concrete validation claim. Prefer
an exact upstream commit plus bounded patch overlay before creating a permanent
fork; create a fork only if the patch surface becomes durable product code.
