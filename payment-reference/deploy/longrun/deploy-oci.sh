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

docker compose   --env-file "$env_file"   -f "$compose_file"   up -d --no-build --pull never

echo "OCI payment-reference runtime started without host-side image builds."
echo "compose_file=$compose_file"
echo "env_file=$env_file"
echo "manifest=$manifest_copy"
echo "source_sha=$source_sha"
