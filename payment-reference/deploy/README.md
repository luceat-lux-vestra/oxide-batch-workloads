# Third-party deployment pins

This directory freezes the external component inputs selected or qualified for
payment reference v1 and contains the bounded Merchant -> Gateway -> Kill Bill
-> payment-sandbox runtime used by the canonical online-payment qualification.

The source of truth for third-party selections is `third-party.lock.json`.

## Active v1 pins

- Kill Bill 0.24.22.
- payment-sandbox v1.5.4.
- PostgreSQL 18.6.

## Qualified but deferred inputs

- Hyperswitch v1.129.0 standalone router.
- Redis 7.4.12 for a future Hyperswitch interoperability lane.
- Card Vault v0.9.0.
- `juspay/hyperswitch-killbill-plugin` source was qualified and rejected for
  unchanged v1 use.
- `deepanshu-iiitu/killbill-prism-plugin` was reviewed as an interoperability
  candidate and is not selected for v1.

Floating `latest`, floating Git branches and unverified payment-sandbox
downloads are forbidden.

## Runtime topology

`compose.yaml` is intentionally bounded to:

```text
Reference Merchant
  |
  +-- JDBC --> merchant-db / PostgreSQL 18.6
  |
  v
Gateway
  |
  +-- JDBC --> gateway-db / PostgreSQL 18.6
  |
  v
Kill Bill 0.24.22
  |
  +-- JDBC --> killbill-db / PostgreSQL 18.6
  |              + Kill Bill 0.24.22 DDL
  |              + payment-sandbox plugin DDL
  |
  v
repository-owned PaymentPluginApi
  |
  v
test-only one-shot fault proxy
  |
  v
payment-sandbox v1.5.4
```

Merchant, Gateway and Kill Bill are published only on host loopback for the
development and CI harness. Merchant/Gateway/Kill Bill communicate over the
internal Compose network. All PostgreSQL services and payment-sandbox remain
internal-only. The fault proxy is transparent unless explicitly armed by the
smoke test and its control listener is also loopback-only.

Kill Bill is pinned to the qualified 0.24.22 image-index digest. Its database
schema is assembled from the exact 0.24.22 source commit and every downloaded
DDL is verified against its Git blob SHA before admission to the image.

The repository-owned plugin is built with `mvn verify` and installed into the
Kill Bill bundle tree through KPM during the derived image build. Gateway is
built from the repository Rust workspace and receives Kill Bill tenant
credentials only through environment variables.

## Runtime smoke

Run the bounded qualification with:

```bash
bash payment-reference/deploy/smoke.sh
```

The smoke owns local tenant bootstrap; the per-payment Gateway path never
silently creates or rotates a tenant.

The merged baseline proves the canonical Gateway -> Kill Bill ->
repository-owned plugin -> payment-sandbox lifecycle for AUTHORIZE, partial/final
CAPTURE, partial/final REFUND, AUTH_CAPTURE/PURCHASE, CANCEL/VOID and
side-effect-free GET.

The current #256 candidate additionally places the Reference Merchant service
and its PostgreSQL truth in the Compose path. Its bounded integrated negative
scenario prepares Merchant state, calls Merchant AUTHORIZE, and traverses
Merchant -> Gateway -> Kill Bill -> plugin -> fault proxy.

It also proves negative idempotency behavior by checking that exact replay,
conflicting idempotency-key reuse and operation-id conflict do not increase the
Kill Bill transaction count.

For ambiguity, the test-only fault proxy forwards one armed provider mutation
to payment-sandbox, fully consumes the upstream response, then drops only that
downstream response. This deterministically models provider commit followed by
response loss.

For the bounded 503-before-side-effect scenario, the same proxy can instead
consume a one-shot synthetic HTTP response **before opening any upstream
connection**. The candidate smoke arms HTTP 503 on the provider's initial
`POST /payments`, requires the Merchant result to remain
`PAYMENT_PENDING / RETRY_PENDING`, requires the proxy upstream-forward count
for that provider create to remain zero, and requires direct provider lookup to
return 404. Exact Merchant replay must retain the same Kill Bill identity and
still create no provider payment.

Hyperswitch remains intentionally deferred. Reintroducing it later requires a
specific interoperability claim and executable proof that the additional
stateful layer adds validation value.

## payment-sandbox image

`payment-sandbox/Dockerfile` builds a small runtime image from the exact
v1.5.4 published Linux binary. Both amd64 and arm64 asset hashes are embedded
and checked before installation.

This deliberately avoids building an evolving upstream `main` checkout.

## Evidence rule

Before any official campaign, resolve every container tag used by that campaign
to the exact runtime image digest on the target registry/architecture and retain
those digests in campaign provenance. A release tag is sufficient for
development bootstrap but not for final evidence.
