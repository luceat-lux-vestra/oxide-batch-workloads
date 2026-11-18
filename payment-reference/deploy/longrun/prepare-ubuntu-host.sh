#!/usr/bin/env bash
set -euo pipefail
umask 027

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../../.." && pwd)"
contract="${script_dir}/host-qualification-contract.json"
compose_file="${repo_root}/payment-reference/deploy/compose.yaml"
evidence_root="${OXIDE_BATCH_HOSTQ_ROOT:-/var/lib/oxide-batch-workloads/payment-reference-host-qualification}"

die() {
  printf 'payment-reference host provisioning error: %s\n' "$*" >&2
  exit 64
}

need_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

[ -r /etc/os-release ] || die "/etc/os-release is required"
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || die "reference provisioning requires Ubuntu"
case "${VERSION_ID:-}" in
  24.04|24.04.*) ;;
  *) die "reference provisioning requires Ubuntu 24.04 LTS; observed VERSION_ID=${VERSION_ID:-unknown}" ;;
esac

case "$(uname -m)" in
  aarch64|arm64) ;;
  *) die "reference provisioning requires ARM64/aarch64; observed $(uname -m)" ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  need_command sudo
  sudo -v
fi

[ -f "${contract}" ] || die "host qualification contract missing: ${contract}"
[ -f "${compose_file}" ] || die "payment-reference compose file missing: ${compose_file}"
git -C "${repo_root}" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
  die "script must run from a Git checkout"
repository_sha="$(git -C "${repo_root}" rev-parse HEAD)"
case "${repository_sha}" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) die "Git HEAD must be a full lowercase 40-hex commit: ${repository_sha}" ;;
esac
[ -z "$(git -C "${repo_root}" status --porcelain)" ] ||
  die "worktree must be clean before host provisioning"

export DEBIAN_FRONTEND=noninteractive
run_root apt-get update
run_root apt-get install -y ca-certificates curl gnupg git jq python3

run_root install -m 0755 -d /etc/apt/keyrings
docker_key_tmp="$(mktemp)"
trap 'rm -f "${docker_key_tmp}"' EXIT
curl --fail --silent --show-error --location \
  https://download.docker.com/linux/ubuntu/gpg \
  --output "${docker_key_tmp}"
run_root install -m 0644 "${docker_key_tmp}" /etc/apt/keyrings/docker.asc

docker_arch="$(dpkg --print-architecture)"
case "${docker_arch}" in
  arm64) ;;
  *) die "Docker package architecture must be arm64; observed ${docker_arch}" ;;
esac
docker_codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
[ -n "${docker_codename}" ] || die "unable to determine Ubuntu codename"

docker_repo_tmp="$(mktemp)"
trap 'rm -f "${docker_key_tmp}" "${docker_repo_tmp}"' EXIT
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
  "${docker_arch}" "${docker_codename}" >"${docker_repo_tmp}"
run_root install -m 0644 "${docker_repo_tmp}" /etc/apt/sources.list.d/docker.list

run_root apt-get update
run_root apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

run_root systemctl enable --now docker
run_root systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

operator_user="${SUDO_USER:-$(id -un)}"
operator_group="$(id -gn "${operator_user}")"
if [ "${operator_user}" != "root" ]; then
  run_root usermod -aG docker "${operator_user}"
fi

run_root install -d -m 0750 "${evidence_root}"
if [ "${operator_user}" != "root" ]; then
  run_root chown "${operator_user}:${operator_group}" "${evidence_root}"
fi

python3 - "${contract}" "${evidence_root}" <<'PY'
import json
import os
import platform
import shutil
import sys
from pathlib import Path

contract_path = Path(sys.argv[1])
evidence_root = Path(sys.argv[2])
contract = json.loads(contract_path.read_text(encoding="utf-8"))

arch = platform.machine().lower()
if arch not in contract["allowed_guest_architectures"]:
    raise SystemExit(
        f"guest architecture {arch!r} not in {contract['allowed_guest_architectures']!r}"
    )

vcpu = os.cpu_count() or 0
if vcpu < contract["minimum_vcpu"]:
    raise SystemExit(f"vCPU {vcpu} < required {contract['minimum_vcpu']}")

mem_total = None
for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
    if line.startswith("MemTotal:"):
        parts = line.split()
        mem_total = int(parts[1]) * 1024
        break
if mem_total is None:
    raise SystemExit("unable to read MemTotal from /proc/meminfo")
if mem_total < contract["minimum_memory_bytes"]:
    raise SystemExit(
        f"memory {mem_total} < required {contract['minimum_memory_bytes']}"
    )

disk = shutil.disk_usage(evidence_root)
if disk.total < contract["minimum_disk_total_bytes"]:
    raise SystemExit(
        f"disk total {disk.total} < required {contract['minimum_disk_total_bytes']}"
    )
if disk.free < contract["minimum_disk_free_bytes"]:
    raise SystemExit(
        f"disk free {disk.free} < required {contract['minimum_disk_free_bytes']}"
    )

print(
    json.dumps(
        {
            "architecture": arch,
            "vcpu": vcpu,
            "memory_total_bytes": mem_total,
            "disk_total_bytes": disk.total,
            "disk_free_bytes": disk.free,
        },
        sort_keys=True,
    )
)
PY

run_root docker version --format '{{.Server.Version}}'
run_root docker compose version --short
docker_engine_arch="$(run_root docker info --format '{{.Architecture}}' | tr '[:upper:]' '[:lower:]')"
case "${docker_engine_arch}" in
  aarch64|arm64) ;;
  *) die "Docker Engine must be ARM64/aarch64; observed ${docker_engine_arch}" ;;
esac

run_root docker compose -f "${compose_file}" config --quiet

python3 "${script_dir}/disk-guard.py" --path "${evidence_root}"

printf '%s\n' \
  "payment-reference host provisioning complete" \
  "repository_sha=${repository_sha}" \
  "evidence_root=${evidence_root}" \
  "guest_sleep_targets=masked" \
  "physical_host_sleep_attestation=NOT_CAPTURED_BY_THIS_SCRIPT" \
  "next=log out/in (or reboot) so docker-group membership is active, then execute the README qualification sequence"

# This helper deliberately does not start Compose services, run smoke tests,
# capture immutable provenance, attest physical-host sleep, or qualify the host.
