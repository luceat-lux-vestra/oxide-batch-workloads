# Galaxy S10 / Termux endurance deployment

This directory prepares campaign #203's `postgres-postgres` endurance controller for the real
Galaxy S10 / Termux node. It does **not** authorize or perform a device smoke by itself.

## Runtime layout

Source checkout and runtime state stay separate:

```text
~/server/repos/oxide-batch-workloads/                  # source checkout
~/server/workloads/oxide-batch-workloads/postgres-postgres/
  releases/<repository-sha>/                           # immutable deployed release
  current -> releases/<repository-sha>
~/server/config/oxide-batch-workloads/postgres-postgres-endurance/
  campaign.env                                         # operator-owned runtime configuration
~/server/data/oxide-batch-workloads/postgres-postgres-endurance/
  controller.pid                                       # lifecycle ownership marker only
~/server/evidence/oxide-batch-workloads/<campaign-id>/ # durable campaign evidence
~/server/logs/oxide-batch-workloads/postgres-postgres-endurance/
  current, @*, config                                  # raw svlogd output only
```

The runit service name is `oxide-batch-postgres-endurance`. `install.sh` creates it as a symlink
directly to the selected immutable `releases/<repository-sha>/service` directory, never through
`current/service`. This keeps runit supervision bound to one release while `current` is switched
during deployment. Every release contains a `service/down` file, so registration never
auto-starts the campaign. `device-control start` uses `sv once`: the controller is run once and
is not automatically restarted after either semantic success or failure.

The controller remains the only campaign/sequence authority. The service wrapper does not choose
or increment logical sequences.

## Exact validation subject

Every deployment manifest records:

```text
oxide-batch = { version = "=0.6.0", features = ["postgres"] }
```

`install.sh` also records the selected repository SHA plus SHA-256 digests of the workload and
controller binaries. No path/git/unpublished OxideBatch substitution belongs in this deployment.

## Build and install

Build the exact selected repository revision for the device, then invoke:

```sh
sh ./deploy/s10/install.sh \
  --repo-sha "$(git rev-parse HEAD)" \
  --workload-bin target/release/postgres-postgres \
  --controller-bin target/release/postgres-postgres-endurancectl \
  --shell-bin "$PREFIX/bin/sh" \
  --server-root "$HOME/server" \
  --service-root "$PREFIX/var/service"
```

On Termux, invoke the installer through `sh` rather than relying on the source checkout's
`#!/usr/bin/env sh` shebang. The installer then rewrites every installed shell entry point
(`device-control`, `pre-iteration-guard`, and both runit `run` scripts) to the explicit
`--shell-bin` path. This avoids any runtime dependency on the nonexistent Android
`/usr/bin/env`. Outside Termux, `--shell-bin` may be omitted and defaults to the absolute
`sh` found on `PATH`.

The selected shell path is recorded in `deployment.json` as `shell_binary` for deployment
provenance.

Installation does not migrate, reset, seed, start, or resume a campaign. It only installs runtime
assets, preserves an existing `campaign.env`, registers the one-shot runit service, and prepares
bounded raw logging.

Before a later device-smoke checkpoint, edit:

```text
~/server/config/oxide-batch-workloads/postgres-postgres-endurance/campaign.env
```

The local-only PostgreSQL default intentionally retains the existing S10 policy:

- host `127.0.0.1`;
- Termux user `u0_a1510`;
- trust/no-password;
- database `postgres_postgres_workload`.

No credential-hardening change is part of campaign #203.

## Lifecycle

From the selected release:

```sh
current="$HOME/server/workloads/oxide-batch-workloads/postgres-postgres/current"
"$current/bin/device-control" validate
"$current/bin/device-control" status
```

The later smoke/campaign owner may use:

```sh
"$current/bin/device-control" start
"$current/bin/device-control" stop
"$current/bin/device-control" status
```

`start` delegates to `sv once`, not `sv up`, specifically to prevent a completed or failed bounded
controller invocation from becoming an automatic restart loop. Planned SIGKILLs target workload
children inside the controller; they do not change this supervisor contract.

`controller.pid` is only a process-ownership marker. A leftover marker causes `start` to fail closed.

`status` reports one of:

- `none` when no marker exists;
- `live:<pid>` when the marker PID accepts a signal probe;
- `stale:<pid>` when the PID is not observable;
- `indeterminate:<pid>` when the PID appears to exist but the observer cannot authoritatively
  signal it. Android ADB `run-as` may produce this state when SELinux denies `kill -0`.

Only a proven `stale` marker may be cleared:

```sh
"$current/bin/device-control" clear-stale
```

`clear-stale` refuses both `live` and `indeterminate` states, and `start` requires `none`.
There is no automatic stale-state deletion.

## Disk guard

The merged controller executes `bin/pre-iteration-guard` before every selected logical iteration.
The bridge invokes the existing device policy at:

```text
~/server/scripts/disk-guard.sh
```

The device policy remains 20% minimum free space and 20 GiB minimum absolute free space. The bridge
does not rewrite those thresholds.

Bridge behavior is fail-closed:

- underlying nonzero exit: propagated;
- output containing an `ALLOW` token with exit 0: allow;
- output containing a `BLOCK` token with exit 0: exit 75 / block;
- missing or unrecognized output: exit 70 / block.

Guard stdout/stderr is inherited by the endurance controller and therefore reaches the service raw
log. The controller's own structured `GuardAttempt` remains the durable semantic record.

The guard never deletes evidence, business rows, or `oxide_batch.*` metadata to recover disk space.

## Raw-log bounds

The service log uses Termux/runit `svlogd` with this checked-in configuration:

```text
s8388608
n16
```

That rotates a raw log at 8 MiB and retains at most 16 rotated files (roughly 128 MiB of rotated raw
logs plus the active file). This bound applies only to:

```text
~/server/logs/oxide-batch-workloads/postgres-postgres-endurance/
```

It does **not** target the durable evidence root. In particular, rotation never deletes or truncates:

- `campaign.json`
- `events.jsonl`
- `iterations/`
- `samples.jsonl`
- `failures/`
- `summary.json`

Crash/failure diagnostics required by the evidence contract belong under durable `failures/`, not
only in raw stdout/stderr.

Existing `s10-health` five-minute snapshots remain separate and are correlated later by timestamps;
this deployment does not duplicate that service.

## Readiness boundary

Issue #232 is complete when these assets are merged and repository-side tests pass. The next bounded
checkpoint may install the merged revision on the S10 and perform path/permission/toolchain/service
readiness checks only. A clean device iteration, deterministic crash/recovery smoke, and the real
24-hour / 100-iteration campaign remain later checkpoints.
