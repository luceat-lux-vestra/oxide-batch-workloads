#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: deploy-oci.sh --manifest <image-manifest.json> --data-path <persistent-data-root>
EOF
  exit 64
}

manifest=""
data_path=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --manifest)
      [ "$#" -ge 2 ] || usage
      manifest="$2"
      shift 2
      ;;
    --data-path)
      [ "$#" -ge 2 ] || usage
      data_path="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$manifest" ] && [ -n "$data_path" ] || usage

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel)"
cd "$repo_root"

manifest="$(realpath "$manifest")"
data_path="$(realpath "$data_path")"
[ -f "$manifest" ] || { echo "manifest not found: $manifest" >&2; exit 66; }
[ -d "$data_path" ] || { echo "data path not found: $data_path" >&2; exit 66; }

required_runtime_env=(
  KILLBILL_USERNAME
  KILLBILL_PASSWORD
  KILLBILL_API_KEY
  KILLBILL_API_SECRET
)
for name in "${required_runtime_env[@]}"; do
  value="${!name:-}"
  if [ -z "${value//[[:space:]]/}" ]; then
    echo "required OCI runtime environment variable is blank: $name" >&2
    exit 64
  fi
done

source_sha="$(python3 - "$manifest" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
print(value.get("source_sha", ""))
PY
)"
[[ "$source_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "manifest source SHA is invalid" >&2
  exit 65
}
actual_sha="$(git rev-parse HEAD)"
[ "$actual_sha" = "$source_sha" ] || {
  echo "checkout SHA $actual_sha does not match manifest source SHA $source_sha" >&2
  exit 65
}
[ -z "$(git status --porcelain)" ] || {
  echo "OCI deployment requires a clean worktree" >&2
  exit 65
}

python3 payment-reference/deploy/longrun/image-manifest.py verify   --manifest "$manifest"   --source-sha "$actual_sha"

runtime_dir="$data_path/payment-reference-runtime"
mkdir -p "$runtime_dir"
manifest_copy="$runtime_dir/image-manifest.json"
env_file="$runtime_dir/images.env"

if [ -e "$manifest_copy" ]; then
  cmp -s "$manifest" "$manifest_copy" || {
    echo "immutable image manifest conflict at $manifest_copy" >&2
    exit 65
  }
else
  install -m 0600 "$manifest" "$manifest_copy"
fi

python3 payment-reference/deploy/longrun/image-manifest.py write-env   --manifest "$manifest_copy"   --out "$env_file"

compose_file="$repo_root/payment-reference/deploy/longrun/compose.oci.yaml"
config_json="$(mktemp)"
trap 'rm -f "$config_json"' EXIT

docker compose --env-file "$env_file" -f "$compose_file" config --format json > "$config_json"

python3 - "$config_json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)

services = config.get("services")
if not isinstance(services, dict) or not services:
    raise SystemExit("OCI Compose config contains no services")
expected_services = {
    "killbill-db",
    "gateway-db",
    "merchant-db",
    "payment-sandbox",
    "payment-sandbox-fault-proxy",
    "killbill",
    "gateway",
    "merchant",
}
observed_services = set(services)
if observed_services != expected_services:
    raise SystemExit(
        "OCI Compose service set mismatch: "
        f"expected={sorted(expected_services)!r} observed={sorted(observed_services)!r}"
    )
for name, service in services.items():
    if "build" in service:
        raise SystemExit(f"OCI Compose service {name!r} unexpectedly contains build configuration")
    image = service.get("image")
    if not isinstance(image, str) or not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise SystemExit(f"OCI Compose service {name!r} is not digest-pinned: {image!r}")
PY

python3 payment-reference/deploy/longrun/check-admission.py --data-path "$data_path"

docker compose --env-file "$env_file" -f "$compose_file" pull

# Pulling images consumes durable disk and may perturb host pressure. Re-admit
# immediately before process creation.
python3 payment-reference/deploy/longrun/check-admission.py --data-path "$data_path"

if ! docker compose \
  --env-file "$env_file" \
  -f "$compose_file" \
  up -d --no-build --pull never --wait --wait-timeout 120
then
  echo "OCI payment-reference runtime failed to reach running/healthy state" >&2
  docker compose --env-file "$env_file" -f "$compose_file" ps --all >&2 || true
  docker compose --env-file "$env_file" -f "$compose_file" logs --no-color --tail=250 >&2 || true
  exit 69
fi

expected_services=(
  killbill-db
  gateway-db
  merchant-db
  payment-sandbox
  payment-sandbox-fault-proxy
  killbill
  gateway
  merchant
)
for service in "${expected_services[@]}"; do
  container_id="$(docker compose --env-file "$env_file" -f "$compose_file" ps -q "$service")"
  if [ -z "$container_id" ]; then
    echo "OCI payment-reference service is not running: $service" >&2
    docker compose --env-file "$env_file" -f "$compose_file" ps --all >&2 || true
    docker compose --env-file "$env_file" -f "$compose_file" logs --no-color --tail=250 "$service" >&2 || true
    exit 69
  fi
  read -r running oom_killed restart_count < <(
    docker inspect --format '{{.State.Running}} {{.State.OOMKilled}} {{.RestartCount}}' "$container_id"
  )
  if [ "$running" != "true" ] || [ "$oom_killed" != "false" ] || [ "$restart_count" != "0" ]; then
    echo "OCI payment-reference service failed runtime-state gate: service=$service running=$running oom_killed=$oom_killed restart_count=$restart_count" >&2
    exit 69
  fi
done

echo "OCI payment-reference runtime started without host-side image builds."
echo "compose_file=$compose_file"
echo "env_file=$env_file"
echo "manifest=$manifest_copy"
echo "source_sha=$source_sha"
