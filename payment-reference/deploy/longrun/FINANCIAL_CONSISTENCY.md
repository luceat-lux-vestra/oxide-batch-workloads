# Read-only cross-system financial consistency probe (#318)

This bounded slice supplies an **independent read path** for normal terminal
Merchant payments. It is *not* the complete independent campaign verifier and
cannot by itself advance the #318 completed-day frontier.

The command performs only these reads on the existing bound OCI Compose host:

1. Merchant `GET /payments/{merchantPaymentId}` on loopback port 18083:
   independently verify prepared order, amount/currency and business state.
2. Gateway `GET /payments/{merchantPaymentId}` on loopback port 18082:
   independently verify canonical terminal state and the distinct Kill Bill and
   provider payment ID mapping.
3. Kill Bill `GET /1.0/kb/payments/{killbillPaymentId}/?withPluginInfo=true`
   on loopback port 8080: verify payment identity and **exact expected
   transaction count/type/status**.
4. Payment sandbox `GET /payments/{providerPaymentId}` through a read-only
   `docker exec … curl` in the existing `oxide-payment-reference` project:
   verify terminal provider state and exact authorized/captured/refunded minor
   amounts.

All three HTTP base URLs must be local `127.0.0.1` and mapped to the expected
ports; configured HTTP proxies and redirects are disabled. Kill Bill
credentials are read from the environment, never embedded into a request URL
or serialized into the result. No POST/PUT/DELETE or Docker control action is
executed.

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
The `fault_overlay` category intentionally cannot pass this normal-case
verifier: it needs an independently qualified ambiguity/recovery procedure.

## Gaps deliberately left unresolved

Even a successful four-system snapshot does **not** prove:

- exact provider mutation side-effect cardinality from the provider operation
  journal, including lost-response replay and duplicate webhooks;
- correct **provider-settlement roster and immutable statement**;
- Merchant settlement close idempotence, replay/conflict and batch accounting;
- published OxideBatch 0.7.0 recovery/reconciliation outputs and PostgreSQL
  batch execution history;
- signed/bound snapshots, crash recovery, longitudinal resource evidence,
  or the per-day independent-verification artifact owner.

The next work units must implement these gates and feed verified SHA-256
artifacts into the canonical `campaign-evidence.py` day-completion protocol.
Do not replace the independent verifier with a synthetic JSON PASS envelope,
or treat the existing 30-minute idle observation as new campaign evidence.

## Testing

```bash
python3 payment-reference/deploy/longrun/test-campaign-financial-consistency.py
```

Tests use mock read-only endpoints and complete normal-case fixtures; they
intentionally reject wrong payment/order/provider identity, amount/currency,
terminal state, Kill Bill extra/missing/failed transactions, unsafe URLs,
unknown financial cases and missing Kill Bill credentials. They cannot prove
real OCI cross-system reads until the user runs a qualified read-only probe.
