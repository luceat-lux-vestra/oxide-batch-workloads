# Kill Bill -> payment-sandbox adapter core

Owner: oxide-batch-workloads #256.

This module is the bounded Java core for the revised payment-reference v1 bridge:

```text
Kill Bill PaymentPluginApi
              |
              v
 transaction journal DAO
              |
              v
      KillBillPaymentAdapter
              |
              v
     PaymentSandboxClient
              |
              v
      payment-sandbox v1.5.4
```

It now contains the bounded Kill Bill runtime binding: OSGi activator,
`PaymentPluginApi`, transaction journal DAO, MySQL/PostgreSQL DDL, and focused
binding tests. Full Compose wiring and a live Kill Bill runtime are still
deferred.

## Frozen v1 semantics

- provider payment ID is deterministic: `kb-<killBillPaymentId>`;
- provider payment creation and financial mutations use separate idempotency
  identities;
- each Kill Bill transaction gets mutation-scoped provider idempotency keys;
- PURCHASE is modeled as `authorize -> capture` because payment-sandbox exposes
  the canonical lifecycle operations rather than a separate sale endpoint;
- AUTHORIZE, CAPTURE, PURCHASE, VOID, REFUND and GET are represented;
- money is integer minor units plus explicit three-letter currency;
- no cloud/public provider endpoint exists by default: the HTTP client requires
  an explicit base URI;
- HTTP 429/5xx are classified retryable;
- transport failure on a mutation is classified **ambiguous**, not business
  failure, because the provider may have committed before the response was lost;
- the adapter never blindly retries an ambiguous financial mutation;
- provider identity mismatch fails closed.

## Provider status mapping

Pinned payment-sandbox v1.5.4 states map to the future Kill Bill binding as:

| payment-sandbox | adapter status |
| --- | --- |
| `pending` | `PENDING` |
| `authorized` | `PROCESSED` |
| `partially_captured` | `PROCESSED` |
| `captured` | `PROCESSED` |
| `partially_refunded` | `PROCESSED` |
| `refunded` | `PROCESSED` |
| `cancelled` | `CANCELED` |
| `failed` | `ERROR` |

The binding maps these values to Kill Bill `PaymentPluginStatus`. Ambiguous
mutation transport failures map to Kill Bill `UNDEFINED`, whose contract is
explicitly “may or may not have succeeded”. The DAO retains Kill Bill payment
and transaction IDs, provider payment ID, payment-creation idempotency key, and
operation idempotency keys. PURCHASE retains both authorize and capture keys.

## Validation

Requires Java 11+ and Maven:

```bash
mvn -B -ntp test
```

Tests use only an in-process JDK HTTP server, H2 in MySQL mode, or a fake
client. They do not call an external PSP or payment-sandbox deployment.

Kill Bill runtime configuration requires:

```text
org.killbill.billing.plugin.paymentSandbox.baseUri=http://payment-sandbox:8080/
```

There is no implicit public provider endpoint.

For Docker/Compose, the same property is supplied through Kill Bill's standard
environment-variable bridge:

```text
KB_org_killbill_billing_plugin_paymentSandbox_baseUri=http://payment-sandbox:8080/
```
