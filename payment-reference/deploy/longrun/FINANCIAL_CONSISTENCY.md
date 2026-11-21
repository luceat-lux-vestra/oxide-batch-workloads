# Read-only cross-system financial consistency probe (#318)

This bounded slice supplies the pure cross-system checker used by the day-level
financial-invariants proof. The CLI remains a **single-payment diagnostic read
path** and cannot by itself advance the #318 completed-day frontier.

The command performs only these reads on the existing bound OCI Compose host:

1. Merchant `GET /payments/{merchantPaymentId}` on loopback port 18083:
   independently verify prepared order, amount/currency and business state.
2. Gateway `GET /payments/{merchantPaymentId}` on loopback port 18082:
   independently verify canonical terminal state and the distinct Kill Bill and
   provider payment ID mapping.
3. Kill Bill `GET /1.0/kb/payments/{killbillPaymentId}/?withPluginInfo=true`
   on loopback port 8080: verify payment identity and **exact expected
   transaction count/type/status**.
4. Payment sandbox `GET /payments/{providerPaymentId}` through
   `docker compose ... exec -T payment-sandbox curl`, using the Compose service
   name rather than a generated container name:
   verify terminal provider state and exact authorized/captured/refunded minor
   amounts.

All three HTTP base URLs must be local `127.0.0.1` and mapped to the expected
ports; configured HTTP proxies and redirects are disabled. Kill Bill
credentials are read from the environment, never embedded into a request URL
or serialized into the result. No POST/PUT/DELETE request is executed and the
probe does not start, stop, recreate or otherwise mutate containers; Compose
`exec` is used only to issue the provider application's GET from inside the
already-running sandbox service.

## Invocation

**Not a campaign activation command.** Execute only against a completed
normal payment already created by an authorized separately gated runner.

```bash
export KILLBILL_USERNAME='admin'
export KILLBILL_PASSWORD='<existing-local-password>'
export KILLBILL_API_KEY='<existing-local-key>'
export KILLBILL_API_SECRET='<existing-local-secret>'

python3 payment-reference/deploy/longrun/campaign-financial-consistency.py \
  --payment-id '<known-existing-merchant-payment-id>' \
  --order-id '<known-existing-merchant-order-id>' \
  --case authorize_capture
```

The verifier uses the canonical plan's synthetic USD 1,000-minor-unit amount.
Its four normal case contracts are:

| Case | Merchant/Gateway state | Provider | Kill Bill transactions | Captured | Refunded |
| --- | --- | --- | --- | ---: | ---: |
| auth_capture_purchase | PAID | captured | PURCHASE×1 | 1,000 | 0 |
| authorize_capture | PAID | captured | AUTHORIZE×1, CAPTURE×2 | 1,000 | 0 |
| authorize_cancel | CANCELLED | cancelled | AUTHORIZE×1, VOID×1 | 0 | 0 |
| auth_capture_refund | REFUNDED | refunded | PURCHASE×1, REFUND×2 | 1,000 | 1,000 |

Successful output has the **non-acceptance** decision
`CROSS_SYSTEM_CONSISTENT_NOT_CAMPAIGN_ACCEPTED`. Any identity, cardinality,
terminal-state or monetary mismatch is a hard `BLOCK` (exit code 75).
The CLI still exposes only the four normal cases. The module additionally exposes
`canonical_fault_consistency` for the five qualified fault-overlay classes at the
post-recovery end-of-day boundary. That pure helper is consumed by the 100-slot
`campaign_day_financial_invariants.py` verifier; it is not a standalone campaign
PASS and does not replace recovery or provider-cardinality evidence.

## Day-level authority boundary

A single successful four-system snapshot still proves none of the surrounding
campaign authorities by itself. Day verification v4 closes that gap only by
binding the 100-slot financial artifact to the separately produced provider
cardinality, settlement, reconciliation and recovery proofs.

See [DAY_FINANCIAL_INVARIANTS.md](DAY_FINANCIAL_INVARIANTS.md). The live
100-slot read-only collector/controller remains a separate implementation slice;
this module must not be replaced by a synthetic JSON PASS envelope.

## Testing

```bash
python3 payment-reference/deploy/longrun/test-campaign-financial-consistency.py
```

Tests use mock read-only endpoints and complete normal/fault EOD fixtures; they
intentionally reject wrong payment/order/provider identity, amount/currency,
terminal state, Kill Bill extra/missing/failed transactions, unsafe URLs,
unknown financial cases and missing Kill Bill credentials. They cannot prove
real OCI cross-system reads until the user runs a qualified read-only probe.
