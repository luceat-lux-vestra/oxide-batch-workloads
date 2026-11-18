# Payment-reference long-running host qualification

Owner: #318

This directory qualifies the **host**, not the 24-hour campaign itself. The
first service-shaped payment campaign keeps the complete qualified Compose
topology and therefore targets a dedicated ARM64 Linux VM rather than the
Galaxy S10 / Termux node used by #203.

## Reference host class

The reference qualification profile is:

- Linux / ARM64 (`aarch64` or `arm64`);
- 8 or more vCPUs;
- 24 GiB or more guest RAM;
- 120 GiB or more virtual disk;
- 40 GiB or more free before qualification/campaign start;
- persistent local disk for database/evidence state;
- physical host sleep disabled for the qualification/campaign window.

The physical host sleep requirement cannot be proven from inside the guest.
`capture-provenance.py` therefore requires an explicit
`--host-sleep-disabled-attested` operator assertion and retains it in
`host.json`.

Do not reduce the payment-reference topology to fit a smaller host. A host that
cannot satisfy the contract is unqualified.

## Machine-readable contract

`host-qualification-contract.json` freezes the #318 resource and qualification
floors. In particular:

- three consecutive runtime smokes before reboot;
- one additional runtime smoke after a clean guest reboot;
- zero unexpected container restarts/OOMs;
- a 30-minute pre-campaign quiescent observation;
- the #203 disk guard policy: at least 20% **and** 20 GiB free.

The repository test is:

```bash
python3 payment-reference/deploy/longrun/test-host-qualification.py
```

It is also invoked by the normal payment-reference runtime smoke so contract
drift fails before expensive runtime setup.

## Qualification sequence

Use an exact clean repository revision. The canonical first campaign currently
targets protected main or a later explicitly selected #318 revision.

1. Provision the ARM64 Linux VM with the resource floors above.
2. Install Docker Engine + Compose plugin and Git. Rust/Java/Maven on the guest
   are recorded when present but are not required when all builds remain inside
   the pinned container build path.
3. Disable host sleep and record that operator action.
4. Check out the exact selected repository revision and leave the worktree
   clean.
5. Run:
   ```bash
   docker compose -f payment-reference/deploy/compose.yaml config --quiet
   ```
6. Run the full runtime smoke **three consecutive times against the same
   running Compose project and persistent volumes**. Use a unique safe business
   namespace per run; this changes fixture identities only and does not reset
   containers or databases:
   ```bash
   evidence_root=/var/lib/oxide-batch-workloads/payment-reference-host-qualification
   mkdir -p "$evidence_root/smoke"

   export KILLBILL_API_KEY='hostq-318'
   export KILLBILL_API_SECRET='hostq-318-local-only'

   for run in 1 2 3; do
     if [ "$run" = 1 ]; then
       reuse_tenant=false
       skip_build=false
     else
       reuse_tenant=true
       skip_build=true
     fi

     PAYMENT_REFERENCE_SMOKE_NAMESPACE="-hostq-$run" \
     PAYMENT_REFERENCE_REUSE_TENANT="$reuse_tenant" \
     PAYMENT_REFERENCE_SKIP_BUILD="$skip_build" \
     PAYMENT_REFERENCE_RESULT_PATH="$evidence_root/smoke/run-$run.json" \
       bash payment-reference/deploy/smoke.sh
   done
   ```
   Run 1 keeps the existing strict tenant-bootstrap/build path. Runs 2-3 reuse
   the same tenant credentials and existing built containers/images, while
   changing only business fixture identities. The default empty namespace plus
   `REUSE_TENANT=false` / `SKIP_BUILD=false` remains the canonical CI path.
7. Leave the qualified runtime images present. Capture immutable provenance:
   ```bash
   python3 payment-reference/deploy/longrun/capture-provenance.py \
     --evidence-dir "$evidence_root/provenance" \
     --host-sleep-disabled-attested
   ```
8. Run the disk guard against the intended durable data/evidence filesystem:
   ```bash
   python3 payment-reference/deploy/longrun/disk-guard.py \
     --path "$evidence_root"
   ```
   Exit 75 / `BLOCK` is a hard qualification stop.
9. Record container restart/OOM state, then perform one clean guest reboot.
10. After reboot, restore the same persistent volumes and run one more full
    payment-reference smoke with the **same** Kill Bill tenant credentials and:
    ```bash
    PAYMENT_REFERENCE_SMOKE_NAMESPACE='-hostq-reboot' \
    PAYMENT_REFERENCE_REUSE_TENANT=true \
    PAYMENT_REFERENCE_SKIP_BUILD=true \
    PAYMENT_REFERENCE_RESULT_PATH="$evidence_root/smoke/post-reboot.json" \
      bash payment-reference/deploy/smoke.sh
    ```
11. With the full service topology running and no business mutation/batch work,
    retain at least 30 minutes of quiescent host/container observations.
12. Only after all #318 host gates pass may the controller/evidence-schema work
    be activated.

This unit deliberately does not automate the reboot or claim that a host is
qualified merely because provenance files exist.

## Immutable provenance

`capture-provenance.py` writes exactly four create-only documents:

```text
host.json
runtime.json
container-images.json
toolchains.json
```

No capture timestamp is embedded. If the selected repository/host/runtime/image
identity is unchanged, rerunning capture is a byte-identical replay. If a
document changes, capture fails closed instead of overwriting the first
qualification record.

The capture requires:

- a clean Git worktree and exact 40-hex HEAD;
- ARM64 guest and Docker engine architecture;
- resource floors from the frozen contract;
- Compose configuration hash;
- `third-party.lock.json` hash;
- exact running Compose service set, container IDs, restart counts and OOM state;
- actual local Compose image IDs plus architecture/OS/repository digests where
  Docker exposes them;
- guest OS/kernel/memory/swap/filesystem identity;
- Docker/Compose and available host toolchain versions.

Local built images may not have a registry `RepoDigest`; their exact Docker
image ID is still mandatory and is the runtime content identity retained for
that host. Third-party registry digests exposed by Docker are retained
additively. Provenance capture also fails if any expected Compose service is
missing/not running, reports an unhealthy state, has a nonzero restart count,
or was OOM-killed.

The final campaign provenance will later bind these host files together with
the exact OxideBatch 0.7.0 workload/binary identities and campaign contract.

## Disk guard

`disk-guard.py` uses the same fail-safe threshold as #203:

```text
minimum_free_percent = 20
minimum_free_bytes   = 20 GiB
```

Both conditions must pass. It emits machine-readable JSON followed by an
`ALLOW` or `BLOCK` token. A policy block exits 75; an observation/config
error exits 70.

The guard never deletes evidence, database state, or OxideBatch metadata.

## Persistent-state smoke namespace

`PAYMENT_REFERENCE_SMOKE_NAMESPACE` exists only to make repeated host
qualification runs accumulate independent business identities in the same
persistent databases/provider state. It is restricted to at most 32 characters
from `[A-Za-z0-9._-]`.

When unset or empty, all existing CI fixture IDs and retained scenario-result
content remain unchanged. The namespace is appended to Merchant/order/payment,
operation, token/idempotency and fault-matrix provider identities. It is not a
database reset mechanism and must never trigger truncation or volume recreation.

### Smoke lifecycle flags

Two additional host-qualification-only flags preserve container identity across
repeated runs:

- `PAYMENT_REFERENCE_REUSE_TENANT=true` skips tenant creation and relies on the
  explicitly reused `KILLBILL_API_KEY/KILLBILL_API_SECRET`; the actual payment
  calls still prove those credentials work.
- `PAYMENT_REFERENCE_SKIP_BUILD=true` uses `docker compose up -d` instead of
  `up -d --build`, avoiding rebuild-driven container replacement.

Both default to `false`, so ordinary CI retains the original strict
build + HTTP-201 tenant-bootstrap semantics.
