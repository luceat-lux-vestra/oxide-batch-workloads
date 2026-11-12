# Kill Bill -> payment-sandbox adapter core

Owner: oxide-batch-workloads #256.

This module is the bounded Java core for the revised payment-reference v1 bridge:

```text
Kill Bill PaymentPluginApi binding (next checkpoint)
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

It deliberately does **not** contain the Kill Bill OSGi activator, plugin DAO,
or full Compose wiring yet. The current checkpoint freezes and tests the
provider translation semantics before introducing Kill Bill runtime lifecycle
and persistence concerns.

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

The next checkpoint binds these values to Kill Bill `PaymentPluginStatus` and
persists Kill Bill payment/transaction/provider identities in the plugin DAO.

## Validation

Requires Java 11+ and Maven:

```bash
mvn -B -ntp test
```

Tests use only an in-process JDK HTTP server or a fake client. They do not call
an external PSP or payment-sandbox deployment.
