# Third-party deployment pins

This directory freezes the external component inputs selected for payment
reference v1. It does not claim that the complete stack is runnable yet.

The source of truth is `third-party.lock.json`.

## Current pins

- Kill Bill 0.24.22.
- Hyperswitch v1.129.0 standalone router.
- payment-sandbox v1.5.4.
- PostgreSQL 18.6.
- Redis 7.4.12.
- Card Vault v0.9.0 is recorded but deferred.

Floating `latest`, floating Git branches and unverified payment-sandbox
downloads are forbidden.

## Why there is no full compose file yet

The Kill Bill -> Hyperswitch bridge did not pass source qualification unchanged.
See `bridge-qualification.md`.

Publishing a compose file that points Kill Bill at an unqualified plugin would
make the repository look runnable while the canonical AUTHORIZE/CAPTURE/etc.
path is not yet trustworthy. The next bounded owner must create or select the
narrow plugin fork, pin its built artifact, and only then wire the full
container topology.

Hyperswitch's selected release is also newer than the upstream Kill Bill
plugin's Java client. Runtime compatibility must be demonstrated rather than
assumed.

## payment-sandbox image

`payment-sandbox/Dockerfile` builds a small runtime image from the exact
v1.5.4 published Linux binary. Both amd64 and arm64 asset hashes are embedded
and checked before installation.

This deliberately avoids building an evolving upstream `main` checkout.

## Evidence rule

Before any official campaign, resolve every container tag to the exact runtime
image digest on the target registry/architecture and retain those digests in
campaign provenance. A release tag is sufficient for development bootstrap but
not for final evidence.
