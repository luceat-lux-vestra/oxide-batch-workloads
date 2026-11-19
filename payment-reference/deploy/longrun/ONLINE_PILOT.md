# Merchant one-slot online pilot (#318)

This script is the first **real Merchant HTTP mutation** component for the
payment-reference endurance program. It is deliberately restricted to **one
normal traffic slot** per invocation; it is *not* the 2-logical-day smoke, a
financial verifier, a long-running controller, or a campaign acceptance tool.

## Safe default

```bash
python3 payment-reference/deploy/longrun/campaign-online-pilot.py \
  --campaign-id pilot318 --day 1 --slot 2
```

Without `--execute-one`, the script prints the deterministic slot plan.
It never sends HTTP or writes evidence in this default mode. A chosen
`fault_overlay` slot is refused even in plan mode; actual fault injections
require a separately qualified fault-script driver.

## Opt-in normal slot execution

Only after a **new exact-source OCI image bundle** and a **new 4-vCPU host
provenance directory** have been qualified, an operator can explicitly choose
a normal slot. The historical OCI image bundle from
`e2fa622f2ec2fa9a12dd9ad2522dd52bc2004c0f` and its original
provenance **do not meet** the new-source attestation check. Do not run this
against the existing old-runtime stack.

Example **template, not a command to run against the current host**:

```bash
python3 payment-reference/deploy/longrun/campaign-online-pilot.py \
  --campaign-id pilot318-qualified-new-bundle \
  --day 1 --slot <one-normal-slot> \
  --execute-one \
  --expected-source-sha '<exact-new-main-40-character-SHA>' \
  --provenance-dir '<newly-qualified-provenance-directory>' \
  --evidence-root '<pre-created-dedicated-evidence-root>'
```

The CLI accepts the exact loopback Merchant endpoint
`http://127.0.0.1:18083` only, with HTTP proxies and redirects disabled.
Execution first checks a clean Git HEAD equal to the explicitly supplied SHA,
the same `repository_sha` in all four host provenance documents,
the upgraded host profile, and eight Compose service names. Immediately
before any POST the CLI also compares **all eight live Docker container IDs,
image IDs, Compose labels, running/healthy status, zero restart counts and
zero OOM-killed flags** against the recorded immutable container-image
provenance; each image reference must be digest-pinned. The live
Merchant container must also publish container port `8080/tcp` **only**
on `127.0.0.1:18083`, matching the pilot's hard-coded Merchant HTTP target.
A mismatch blocks all payment mutation attempts.

This is a point-in-time read-only topology guard, **not an atomic freeze of
Docker against another host operator's concurrent restart**. The operator
must separately verify image release/signatures, quiescence, memory/disk
admission, other services' isolation and actual financial acceptance;
this pilot guard does not substitute for those gates.

## Durable request semantics

The evidence root must exist as a real operator-chosen directory;
a symlink as the evidence-root argument is rejected before network I/O.

The pilot uses a distinct evidence root, not the canonical campaign
`campaign.json / events.jsonl / days` acceptance directory. Each logical
day has a create-only identity bound to the full deterministic plan, source
SHA and the SHA-256 of all four provenance documents. Day-level Merchant
and Customer POST setup occurs once and is reusable only after **both
HTTP 201 response records are durable**.

For one chosen normal slot it makes the following ordered requests:

1. Create Merchant (first slot of that day only)
2. Create Customer (first slot of that day only)
3. Create Order
4. Prepare Merchant payment
5. Perform the exact normal mutation sequence from the frozen day plan
6. Check final Merchant state with a GET

Before every POST, the exact method/path/body hash is written to a
`NN-intent.json` file with fsync. When an HTTP response is received,
a separate `NN-response.json` records status, response digest and
decoded JSON, also with fsync. There are **no automatic HTTP retries**.
An inconclusive transport, HTTP 202, unexpected status, state mismatch, or
process interruption leaves evidence behind and blocks continuation.

An already created slot directory always rejects a second execution,
even if the first process reached the final GET. A complete day setup may
be reused for another *new* slot, but a partial/failed setup blocks every
subsequent slot until an independent operator reconciliation has occurred.
There is intentionally **no repair/reset/replay CLI** to avoid recovering by
blindly reissuing financial effects.

Output on successful normal payment is
`ONE_SLOT_EXECUTED_NOT_FINANCIALLY_VERIFIED`, *not* PASS.
It is not accepted for the #318 day frontier until independent Merchant,
Gateway, Kill Bill, provider operation-cardinality, settlement and OxideBatch
reconciliation/recovery evidence are all present and verified.

The pilot has no fault overlay execution, crash injection, parallelism,
settlement handling or automatic repair; these are separate bounded units.

## Offline negative controls

```bash
python3 payment-reference/deploy/longrun/test-campaign-online-pilot.py
```

Tests use a fake Merchant transport: happy path, consecutive slots sharing a
completed setup, durable intent on lost transport, HTTP 202, HTTP 409,
nonterminal final GET, interrupted Merchant/Customer setup, source/provenance
identity drift, fault-slot refusal, default no-I/O plan and missing
source/provenance gate. No real HTTP POST or OCI container operation occurs
during these unit tests.
