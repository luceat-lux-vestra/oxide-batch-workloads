# Kill Bill -> Hyperswitch bridge qualification

Owner: #256

Qualified upstream:

- repository: `juspay/hyperswitch-killbill-plugin`
- upstream commit: `11433a53e1d0e92c9216e43e5e31ac02f606f23d`
- source POM version: `0.1.1`
- last published release: `version-0.1.0`

## Result

**FORK REQUIRED before runtime integration.**

The upstream plugin is useful as a base because it already implements the Kill
Bill payment-plugin entry points required by the first reference slice:

- AUTHORIZE;
- CAPTURE;
- PURCHASE;
- VOID;
- REFUND;
- payment-info refresh.

It is not safe to consume unchanged for this reference system.

## Blocking gaps

### KRW is not supported

The current `convertCurrency` implementation explicitly maps only:

`USD, AUD, CAD, DKK, EUR, GBP, NZD, SEK`.

The reference contract must support KRW, so silently returning `null` for KRW
is unacceptable.

### Self-hosted Hyperswitch endpoint is not proven

Configuration exposes an `environment` property, but
`buildHyperswitchClient` / `buildHyperswitchRefundsClient` construct
`HsApiClient` from only API-key material. The configured environment is not
passed at those call sites.

The reference system requires an explicit base URL pointing at our pinned
self-hosted Hyperswitch router. A fork must make the target endpoint
configuration explicit and testable.

### Kill Bill identity persistence is suspicious in later operations

AUTHORIZE/PURCHASE persist the supplied Kill Bill payment and transaction IDs.

CAPTURE, VOID and REFUND currently call `addResponse` with
`kbPaymentMethodId` in positions used by the earlier methods for payment /
transaction identity. The fork must correct this and cover the mapping with
tests. We must preserve:

`Kill Bill payment ID != payment-method ID != transaction ID`.

### Payment refresh does not publish the refreshed view

`getPaymentInfo` initializes `wasRefreshed = false`, performs refresh work
for pending transactions, but does not set the flag to true. As written, the
method returns the original transaction list instead of re-reading the updated
state.

The fork must fix this before ambiguous-outcome convergence is trusted.

### Webhook notification is not implemented

`processNotification` returns `null`. That is insufficient for the later
delayed/duplicate/out-of-order webhook scenarios.

The first online happy path may use explicit status retrieval, but the fork must
retain a clear extension point for async notification handling.

### Client compatibility is old and must be proven

The upstream source uses `hyperswitch-java-client 0.0.4`. The selected
Hyperswitch runtime is v1.129.0. Compatibility is not assumed.

The fork must either:

1. prove the old client against v1.129.0 for the exact required API surface; or
2. update/replace the client while preserving the Kill Bill plugin contract.

## Fork acceptance

The fork is acceptable for #256 only when automated tests prove:

- explicit self-hosted Hyperswitch base URL;
- KRW conversion;
- AUTHORIZE / CAPTURE / PURCHASE / VOID / REFUND;
- correct Kill Bill payment/transaction/payment-method identity mapping;
- PENDING refresh returns the refreshed state;
- no production/cloud endpoint is reachable by default;
- no real card data is required.

Do not expand the fork into a general Kill Bill plugin cleanup project.
