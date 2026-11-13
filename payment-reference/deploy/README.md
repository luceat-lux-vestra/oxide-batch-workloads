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
deterministic Merchant requests
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

Gateway and Kill Bill are published only on host loopback for the development
and CI harness. Both PostgreSQL services and payment-sandbox remain internal to
the Compose network. The fault proxy is transparent unless explicitly armed by
the smoke test and its control listener is also loopback-only.

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

It proves the full Merchant -> Gateway -> Kill Bill -> repository-owned plugin
-> payment-sandbox path for:

- AUTHORIZE;
- partial and final CAPTURE;
- partial and final REFUND;
- AUTH_CAPTURE / Kill Bill PURCHASE;
- AUTHORIZE -> CANCEL / Kill Bill VOID;
- side-effect-free canonical GET.

It also proves negative idempotency behavior by checking that exact replay,
conflicting idempotency-key reuse and operation-id conflict do not increase the
Kill Bill transaction count.

For ambiguity, the test-only fault proxy forwards one armed provider mutation
to payment-sandbox, fully consumes the upstream response, then drops only that
downstream response. This deterministically models provider commit followed by
response loss. The smoke requires the provider mutation count to remain one
across the exact Gateway replay, demonstrating inquiry-before-retry rather than
blind duplicate mutation.

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
