# Gateway -> Kill Bill online contract

Owner: #259. Parent: #256. Epic: #253.

Status: **checkpoint 1 design freeze**.

This document freezes the first online integration contract before any Gateway
source implementation. The already validated provider path remains unchanged:

```text
Merchant
  -> Gateway / UPP
  -> Kill Bill 0.24.22
  -> killbill-payment-sandbox PaymentPluginApi
  -> payment-sandbox v1.5.4
```

The Gateway is responsible for canonical request identity and Kill Bill
orchestration. Kill Bill owns billing/payment transaction truth. The plugin owns
the Kill Bill -> provider translation. payment-sandbox owns provider truth.

## 1. Local endpoint and authentication contract

The first implementation targets the already qualified local Kill Bill listener:

```text
KILLBILL_BASE_URL=http://127.0.0.1:${KILLBILL_PORT:-8080}
```

Gateway credentials are configuration only and must never be committed:

```text
KILLBILL_USERNAME
KILLBILL_PASSWORD
KILLBILL_API_KEY
KILLBILL_API_SECRET
```

Every tenant-scoped Gateway request to Kill Bill must send:

```http
Authorization: Basic <KILLBILL_USERNAME:KILLBILL_PASSWORD>
X-Killbill-ApiKey: <KILLBILL_API_KEY>
X-Killbill-ApiSecret: <KILLBILL_API_SECRET>
X-Killbill-CreatedBy: oxide-batch-payment-reference-gateway
Content-Type: application/json
Accept: application/json
```

The local runtime bootstrap, not the per-payment request path, owns creation of
the Kill Bill tenant. Kill Bill's tenant bootstrap endpoint is:

```text
POST /1.0/kb/tenants
```

with Basic authentication, `X-Killbill-CreatedBy`, and an `apiKey/apiSecret`
body. The Gateway must fail startup/readiness if tenant authentication is not
usable; it must not silently create or rotate a tenant while handling a payment.

## 2. Identity model

Raw Merchant identifiers and payment tokens are not used directly as Kill Bill
external keys. Keys are deterministic, bounded, and non-secret.

The canonical key function is:

```text
digest(parts...) = lowercase hex SHA-256 of UTF-8 parts joined by NUL (0x00)
```

Frozen external keys:

```text
Kill Bill account externalKey
  = pr-acct-v1-<digest(merchantId, currency)>

Kill Bill payment-method externalKey
  = pr-pm-v1-<digest(merchantId, currency, paymentMethodToken)>

Kill Bill payment external key
  = pr-pay-v1-<digest(merchantId, merchantPaymentId)>

Kill Bill transaction external key
  = pr-txn-v1-<digest(merchantId, operationId)>
```

Consequences:

- one Kill Bill account exists per `merchantId + currency`; this preserves the
  Kill Bill account-currency invariant while the canonical Gateway API remains
  multi-currency;
- one Kill Bill payment method exists per distinct synthetic
  `paymentMethodToken` within that merchant/currency account;
- the raw payment-method token is never copied into a Kill Bill external key;
- one Kill Bill payment represents one canonical merchant payment;
- one Kill Bill transaction represents one canonical Gateway operation;
- the Gateway persists Kill Bill UUIDs returned by Kill Bill separately from all
  Merchant and provider identifiers.

The existing Gateway columns remain semantically distinct:

```text
operation_id                  canonical Gateway operation identity
idempotency_key               caller replay identity
merchant_payment_id           Merchant payment identity
killbill_payment_id           Kill Bill Payment UUID
killbill_transaction_id       Kill Bill PaymentTransaction UUID
provider_payment_id           payment-sandbox payment identity
provider_operation_id         provider operation identity when available
provider_idempotency_key      provider mutation replay identity
```

No field may be reused as a substitute for another layer's identifier.

## 3. Kill Bill account and payment-method bootstrap

Before an initial AUTHORIZE or PURCHASE, the Gateway resolves a Kill Bill
account by external key:

```text
GET /1.0/kb/accounts?externalKey=<accountExternalKey>
```

If absent, create:

```text
POST /1.0/kb/accounts
```

The account must carry the operation currency and stable synthetic reference
data. A concurrent create race is resolved by re-reading the external key and
verifying that the resulting account has the expected currency. Never select an
arbitrary account after a conflict.

The Gateway then resolves/creates the synthetic Kill Bill payment method under
that account:

```text
GET  /1.0/kb/accounts/{accountId}/paymentMethods
POST /1.0/kb/accounts/{accountId}/paymentMethods?isDefault=false
```

Creation uses:

```json
{
  "externalKey": "<pr-pm-v1-...>",
  "pluginName": "killbill-payment-sandbox"
}
```

The payment method is routing metadata for Kill Bill. The current synthetic
provider plugin has no provider-side payment-method resource. The raw canonical
`paymentMethodToken` therefore remains Gateway-owned input; only its digest is
used for deterministic Kill Bill payment-method identity.

## 4. Operation mapping

### AUTHORIZE

Canonical:

```text
POST /payments/authorize
```

Kill Bill:

```text
POST /1.0/kb/accounts/{accountId}/payments?paymentMethodId={paymentMethodId}
```

Body fields used by the Gateway:

```json
{
  "transactionType": "AUTHORIZE",
  "amount": "<major-unit decimal>",
  "currency": "<ISO-4217>",
  "paymentExternalKey": "<pr-pay-v1-...>",
  "transactionExternalKey": "<pr-txn-v1-...>"
}
```

### AUTH_CAPTURE

Canonical `/payments/auth-capture` maps to Kill Bill `PURCHASE`.

```text
POST /1.0/kb/accounts/{accountId}/payments?paymentMethodId={paymentMethodId}
transactionType = PURCHASE
```

The repository-owned plugin already translates PURCHASE into distinct provider
AUTHORIZE and CAPTURE mutations with separate provider idempotency keys.

### CAPTURE

```text
POST /1.0/kb/payments/{killbillPaymentId}/
```

Body contains amount, currency, canonical payment external key and a new
transaction external key derived from this Gateway `operationId`.

### CANCEL / VOID

```text
DELETE /1.0/kb/payments/{killbillPaymentId}/
```

The request carries the new transaction external key. Canonical CANCEL maps to
Kill Bill VOID and then to provider CANCEL.

### REFUND

```text
POST /1.0/kb/payments/{killbillPaymentId}/refunds
```

Body contains refund amount, currency, canonical payment external key and the
new transaction external key.

### GET / inquiry

Primary read after the Kill Bill payment UUID is known:

```text
GET /1.0/kb/payments/{killbillPaymentId}?withPluginInfo=true
```

Recovery read when an initial create response was lost before the UUID was
persisted:

```text
GET /1.0/kb/payments?externalKey=<paymentExternalKey>&withPluginInfo=true
```

The Gateway must locate the exact transaction by
`transactionExternalKey`. It must not infer the current operation outcome from
HTTP 2xx alone or from whichever transaction happens to be last in the array.

## 5. Amount conversion

The canonical Gateway API stores integer minor units. Kill Bill REST accepts
major-unit decimals.

The Gateway client must use a currency-aware exact conversion:

```text
minor units -> exact decimal using ISO currency fraction digits
```

No floating-point type is permitted. Conversion must be reversible for all
accepted currencies; a value that cannot be represented exactly is rejected
before dispatch.

The Kill Bill plugin converts the resulting `BigDecimal` back to provider minor
units using Kill Bill's `KillBillMoney.toMinorUnits`.

## 6. Kill Bill -> canonical status mapping

HTTP status is transport/API evidence, not canonical payment truth.

The exact matching Kill Bill transaction status is authoritative:

| Kill Bill transaction status | Gateway operation state | Payment interpretation |
| --- | --- | --- |
| `SUCCESS` | `SUCCEEDED` | Apply operation-specific successful payment state |
| `PENDING` | `PENDING` | Preserve current payment state / PAYMENT_PENDING for initial mutation |
| `UNKNOWN` | `UNKNOWN` | `PAYMENT_UNKNOWN` until inquiry resolves it |
| `PAYMENT_FAILURE` | `FAILED` | Business/payment failure |
| `PLUGIN_FAILURE` | `RETRY_PENDING` only after inquiry confirms no successful transaction | Do not blind-retry |
| `PAYMENT_SYSTEM_OFF` | `RETRY_PENDING` | No success claim |

Operation-specific successful payment states:

```text
AUTHORIZE -> AUTHORIZED
PURCHASE  -> PAID
CAPTURE   -> PAID, or PARTIALLY_CAPTURED when cumulative captured < authorized
VOID      -> CANCELLED
REFUND    -> REFUNDED, or PARTIALLY_REFUNDED when cumulative refunded < captured
```

Cumulative capture/refund state must come from Kill Bill/provider read data, not
from the current request amount alone.

## 7. Dispatch and persistence rule

For a newly accepted Gateway mutation:

1. persist canonical mutation first as `PENDING`;
2. derive deterministic Kill Bill external keys;
3. resolve required account/payment method for an initial payment;
4. dispatch exactly one Kill Bill mutation;
5. parse the returned payment and find the transaction whose
   `transactionExternalKey` equals the expected key;
6. persist Kill Bill payment UUID and transaction UUID separately;
7. update canonical state from that exact transaction status.

A process crash after step 4 must not cause a blind second mutation. On restart,
the same Gateway operation first performs read-side inquiry by deterministic
payment/transaction external keys and only dispatches when absence is proven.

## 8. Transport and API failure policy

Kill Bill documents payment APIs with, among others:

- `402`: gateway/payment decline;
- `502`: failed to submit payment transaction;
- `503`: payment in unknown status / gateway response not received;
- `504`: payment operation timeout.

Frozen policy:

- `402`: terminal only when the returned/read transaction confirms
  `PAYMENT_FAILURE`;
- `503` and request timeout: immediately `UNKNOWN`, then inquiry; never blind
  retry;
- `502`, `504`, connection reset, EOF, DNS/connect failure: inquiry first.
  Retry only when read-side evidence proves the transaction external key does
  not exist;
- malformed 2xx or a 2xx without the expected transaction external key:
  `UNKNOWN` and inquiry;
- authentication/tenant failure: infrastructure failure, no provider retry;
- account/payment-method provisioning conflict: re-read deterministic external
  key and verify immutable identity before proceeding.

## 9. Negative test matrix frozen for the next implementation units

| Case | Required evidence | Expected behavior |
| --- | --- | --- |
| same idempotency key, changed canonical mutation | Gateway DB | 409, zero Kill Bill calls |
| same operationId, changed amount/currency/kind | Gateway DB | conflict before Kill Bill |
| same completed replay | Gateway DB + persisted KB IDs | return retained result, zero new mutation |
| account create race | account external key read | converge only on same merchant/currency identity |
| payment-method create race | PM external key read | converge only on same account/plugin identity |
| Kill Bill 201 + matching transaction SUCCESS | response transaction | persist KB IDs, canonical success |
| Kill Bill 201 + PENDING | response transaction | remain pending |
| Kill Bill 201 + UNKNOWN | response transaction | PAYMENT_UNKNOWN/UNKNOWN |
| Kill Bill 201 without expected transaction key | response | UNKNOWN then inquiry |
| Kill Bill 402 | response + inquiry if needed | FAILED only when PAYMENT_FAILURE confirmed |
| Kill Bill 503 | response | UNKNOWN; inquiry; no blind retry |
| Kill Bill 504 | response/inquiry | inquiry before retry |
| connection timeout after write | inquiry by payment external key | UNKNOWN until exact txn found/absence proved |
| response lost after provider commit | Kill Bill read + plugin/provider info | recover same KB txn; no duplicate provider mutation |
| Gateway crash after Kill Bill commit before DB update | deterministic external keys | read and persist existing KB IDs |
| wrong KB payment ID attached to merchant payment | DB + KB response | fail closed, no follow-up mutation |
| currency differs from KB account currency | account read | fail closed |
| raw paymentMethodToken appears in outbound external key/log | captured request/log | test failure |
| malformed Kill Bill JSON | HTTP capture | UNKNOWN/infrastructure error; no success claim |
| 401/403 or invalid tenant headers | HTTP response | infrastructure failure, zero provider retry |
| GET finds multiple incompatible payments for deterministic external key | read result | invariant violation/manual review |

## 10. Explicit non-goals for checkpoint 1

This checkpoint does not implement:

- a Rust Kill Bill HTTP client;
- Gateway database migrations;
- account/payment-method provisioning code;
- AUTHORIZE/PURCHASE dispatch;
- CAPTURE/VOID/REFUND dispatch;
- Compose changes;
- Merchant orchestration;
- failure injection.

Those start in checkpoint 2 only after this contract is committed.

## Source anchors inspected

The freeze was checked against Kill Bill source contracts used by 0.24.x:

- JAX-RS prefix `/1.0/kb`;
- `X-Killbill-ApiKey`, `X-Killbill-ApiSecret`,
  `X-Killbill-CreatedBy`;
- AccountResource account lookup/create and payment-method endpoints;
- AccountResource AUTHORIZE/PURCHASE payment path;
- PaymentResource CAPTURE/VOID/REFUND/GET paths;
- `PaymentTransactionJson` external payment/transaction keys;
- Kill Bill `TransactionStatus` semantics;
- current repository runtime smoke Basic auth defaults and validated
  `killbill-payment-sandbox` plugin registration.
