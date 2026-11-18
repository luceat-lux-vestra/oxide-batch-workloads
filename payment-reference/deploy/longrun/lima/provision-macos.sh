#!/usr/bin/env bash
set -euo pipefail

LIMA_VERSION="2.3.0"
LIMA_ARCHIVE_SHA256="b7af444bd8f91d3c307477e562f2cf3bd7595d62b9429185e4fa841563578cfc"
LIMA_ARCHIVE="lima-${LIMA_VERSION}-Darwin-arm64.tar.gz"
LIMA_URL="https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/${LIMA_ARCHIVE}"
LIMA_PREFIX="${HOME}/.local/opt/lima-${LIMA_VERSION}"
VM_NAME="oxide-payment-longrun"
REPO_URL="https://github.com/luceat-lux-vestra/oxide-batch-workloads.git"
REPO_SHA=""

usage() {
  cat >&2 <<'EOF'
usage: provision-macos.sh --repo-sha <40-hex> [--vm-name <name>]

Creates a NEW dedicated Lima/VZ ARM64 VM for #318.
The script refuses to overwrite or reuse an existing Lima instance.
EOF
  exit 64
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo-sha)
      [ "$#" -ge 2 ] || usage
      REPO_SHA="$2"
      shift 2
      ;;
    --vm-name)
      [ "$#" -ge 2 ] || usage
      VM_NAME="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

case "$REPO_SHA" in
  ""|*[!0-9a-f]*)
    usage
    ;;
esac
[ "${#REPO_SHA}" -eq 40 ] || usage

case "$VM_NAME" in
  ""|*[!A-Za-z0-9._-]*)
    echo "vm name must contain only [A-Za-z0-9._-]" >&2
    exit 64
    ;;
esac

[ "$(uname -s)" = "Darwin" ] || {
  echo "this provisioning path requires macOS" >&2
  exit 69
}
[ "$(uname -m)" = "arm64" ] || {
  echo "this provisioning path requires Apple Silicon / arm64" >&2
  exit 69
}

host_memory_bytes="$(sysctl -n hw.memsize)"
host_cpus="$(sysctl -n hw.logicalcpu)"
[ "$host_memory_bytes" -ge 34359738368 ] || {
  echo "host must provide at least 32 GiB RAM before allocating the 24 GiB guest" >&2
  exit 69
}
[ "$host_cpus" -ge 8 ] || {
  echo "host must provide at least 8 logical CPUs" >&2
  exit 69
}

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
lima_yaml="$script_dir/lima.yaml"
[ -f "$lima_yaml" ] || {
  echo "missing Lima config: $lima_yaml" >&2
  exit 66
}

install_lima() {
  mkdir -p "$LIMA_PREFIX"
  if [ -x "$LIMA_PREFIX/bin/limactl" ]; then
    installed="$("$LIMA_PREFIX/bin/limactl" --version | awk '{print $3}' | sed 's/^v//')"
    [ "$installed" = "$LIMA_VERSION" ] || {
      echo "unexpected Lima version under $LIMA_PREFIX: $installed" >&2
      exit 65
    }
    return
  fi

  tmp_dir="$(mktemp -d)"
  archive="$tmp_dir/$LIMA_ARCHIVE"

  curl --fail --location --silent --show-error "$LIMA_URL" --output "$archive"
  observed="$(shasum -a 256 "$archive" | awk '{print $1}')"
  [ "$observed" = "$LIMA_ARCHIVE_SHA256" ] || {
    rm -rf "$tmp_dir"
    echo "Lima archive checksum mismatch: expected=$LIMA_ARCHIVE_SHA256 observed=$observed" >&2
    exit 65
  }

  tar -xzf "$archive" -C "$LIMA_PREFIX"
  rm -rf "$tmp_dir"
  [ -x "$LIMA_PREFIX/bin/limactl" ] || {
    echo "Lima archive did not install bin/limactl" >&2
    exit 65
  }
}

install_lima
LIMACTL="$LIMA_PREFIX/bin/limactl"

"$LIMACTL" --version

existing="$("$LIMACTL" list --quiet "$VM_NAME" 2>/dev/null || true)"
if [ "$existing" = "$VM_NAME" ]; then
  echo "refusing to reuse existing Lima instance: $VM_NAME" >&2
  echo "delete or rename it explicitly before creating the canonical #318 host" >&2
  exit 73
fi

"$LIMACTL" validate "$lima_yaml"
"$LIMACTL" --tty=false start --name="$VM_NAME" "$lima_yaml"

"$LIMACTL" shell "$VM_NAME" -- bash -lc "
  set -euo pipefail

  repo_root=/var/lib/oxide-batch-workloads/src/oxide-batch-workloads
  if [ -e \"\$repo_root\" ]; then
    echo \"refusing to overwrite existing repository checkout: \$repo_root\" >&2
    exit 73
  fi

  git clone '$REPO_URL' \"\$repo_root\"
  cd \"\$repo_root\"
  git checkout --detach '$REPO_SHA'

  actual=\$(git rev-parse HEAD)
  [ \"\$actual\" = '$REPO_SHA' ] || {
    echo \"repository SHA mismatch: expected=$REPO_SHA observed=\$actual\" >&2
    exit 65
  }
  [ -z \"\$(git status --porcelain)\" ] || {
    echo 'repository checkout is dirty immediately after provisioning' >&2
    exit 65
  }

  printf 'guest_arch=%s\\n' \"\$(uname -m)\"
  printf 'guest_kernel=%s\\n' \"\$(uname -r)\"
  printf 'guest_vcpu=%s\\n' \"\$(nproc)\"
  printf 'guest_mem_total_kib=%s\\n' \"\$(awk '/MemTotal:/ {print \$2}' /proc/meminfo)\"
  printf 'docker=%s\\n' \"\$(docker version --format '{{.Server.Version}}')\"
  printf 'docker_compose=%s\\n' \"\$(docker compose version --short)\"
  printf 'repo_sha=%s\\n' \"\$actual\"
"

cat <<EOF
created_vm=$VM_NAME
lima_version=$LIMA_VERSION
repo_sha=$REPO_SHA

The VM is provisioned, but it is NOT yet host-qualified.
Before qualification:
  1. keep the Mac on AC power;
  2. prevent macOS sleep for the full qualification/campaign window;
  3. enter the VM with:
       $LIMACTL shell $VM_NAME
  4. follow payment-reference/deploy/longrun/README.md exactly.

Do not treat provisioning success as #318 qualification evidence.
EOF
