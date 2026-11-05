#!/usr/bin/env sh
set -eu

usage() {
  cat >&2 <<'EOF'
usage: install.sh --repo-sha <40-hex> --workload-bin <path> --controller-bin <path> [--server-root <path>] [--service-root <path>]
EOF
  exit 64
}

server_root="${HOME:?HOME is required}/server"
service_root="${PREFIX:-}/var/service"
repo_sha=""
workload_bin=""
controller_bin=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server-root)
      [ "$#" -ge 2 ] || usage
      server_root="$2"
      shift 2
      ;;
    --service-root)
      [ "$#" -ge 2 ] || usage
      service_root="$2"
      shift 2
      ;;
    --repo-sha)
      [ "$#" -ge 2 ] || usage
      repo_sha="$2"
      shift 2
      ;;
    --workload-bin)
      [ "$#" -ge 2 ] || usage
      workload_bin="$2"
      shift 2
      ;;
    --controller-bin)
      [ "$#" -ge 2 ] || usage
      controller_bin="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

case "$server_root" in
  /*) ;;
  *) echo "server root must be absolute: $server_root" >&2; exit 64 ;;
esac
case "$service_root" in
  /*) ;;
  *) echo "service root must be absolute: $service_root" >&2; exit 64 ;;
esac
case "$server_root$service_root" in
  *"'"*) echo "deployment paths must not contain single quotes" >&2; exit 64 ;;
esac
case "$repo_sha" in
  *[!0-9a-f]*|"") echo "repo SHA must be lowercase hexadecimal" >&2; exit 64 ;;
esac
[ "${#repo_sha}" -eq 40 ] || { echo "repo SHA must be exactly 40 hexadecimal characters" >&2; exit 64; }
[ -x "$workload_bin" ] || { echo "workload binary is not executable: $workload_bin" >&2; exit 66; }
[ -x "$controller_bin" ] || { echo "controller binary is not executable: $controller_bin" >&2; exit 66; }

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
deployment_base="$server_root/workloads/oxide-batch-workloads/postgres-postgres"
release_dir="$deployment_base/releases/$repo_sha"
config_root="$server_root/config/oxide-batch-workloads/postgres-postgres-endurance"
evidence_root="$server_root/evidence/oxide-batch-workloads"
log_root="$server_root/logs/oxide-batch-workloads/postgres-postgres-endurance"
state_root="$server_root/data/oxide-batch-workloads/postgres-postgres-endurance"
service_name="oxide-batch-postgres-endurance"
service_link="$service_root/$service_name"
disk_guard="$server_root/scripts/disk-guard.sh"

mkdir -p \
  "$release_dir/bin" \
  "$release_dir/etc" \
  "$release_dir/service/log" \
  "$config_root" \
  "$evidence_root" \
  "$log_root" \
  "$state_root" \
  "$service_root"

install -m 0755 "$workload_bin" "$release_dir/bin/postgres-postgres"
install -m 0755 "$controller_bin" "$release_dir/bin/postgres-postgres-endurancectl"
install -m 0755 "$script_dir/bin/device-control" "$release_dir/bin/device-control"
install -m 0755 "$script_dir/bin/pre-iteration-guard" "$release_dir/bin/pre-iteration-guard"
install -m 0755 "$script_dir/service/run" "$release_dir/service/run"
install -m 0755 "$script_dir/service/log/run" "$release_dir/service/log/run"
: > "$release_dir/service/down"

install -m 0644 "$script_dir/svlogd.config" "$log_root/config"

campaign_config="$config_root/campaign.env"
if [ ! -e "$campaign_config" ]; then
  install -m 0600 "$script_dir/campaign.env.example" "$campaign_config"
fi

cat > "$release_dir/etc/runtime.env.tmp" <<EOF
OXIDE_SERVER_ROOT='$server_root'
OXIDE_DEPLOYMENT_BASE='$deployment_base'
OXIDE_CONFIG_ROOT='$config_root'
OXIDE_EVIDENCE_ROOT='$evidence_root'
OXIDE_LOG_ROOT='$log_root'
OXIDE_STATE_ROOT='$state_root'
OXIDE_SERVICE_ROOT='$service_root'
OXIDE_SERVICE_NAME='$service_name'
OXIDE_DISK_GUARD='$disk_guard'
EOF
chmod 0600 "$release_dir/etc/runtime.env.tmp"
mv -f "$release_dir/etc/runtime.env.tmp" "$release_dir/etc/runtime.env"

workload_sha256=$(sha256sum "$release_dir/bin/postgres-postgres" | awk '{print $1}')
controller_sha256=$(sha256sum "$release_dir/bin/postgres-postgres-endurancectl" | awk '{print $1}')
installed_at_epoch=$(date +%s)

cat > "$release_dir/deployment.json.tmp" <<EOF
{
  "schema": "oxide-batch-workloads.s10-deployment",
  "schema_version": 1,
  "repository_sha": "$repo_sha",
  "oxide_batch_version": "0.6.0",
  "oxide_batch_features": ["postgres"],
  "workload_binary": "bin/postgres-postgres",
  "workload_sha256": "$workload_sha256",
  "controller_binary": "bin/postgres-postgres-endurancectl",
  "controller_sha256": "$controller_sha256",
  "installed_at_unix_seconds_utc": $installed_at_epoch
}
EOF
chmod 0644 "$release_dir/deployment.json.tmp"
mv -f "$release_dir/deployment.json.tmp" "$release_dir/deployment.json"

ln -sfn "$release_dir" "$deployment_base/current"
ln -sfn "$deployment_base/current/service" "$service_link"

printf 'installed_release=%s\n' "$release_dir"
printf 'current=%s\n' "$deployment_base/current"
printf 'service=%s\n' "$service_link"
printf 'campaign_config=%s\n' "$campaign_config"
printf 'evidence_root=%s\n' "$evidence_root"
printf 'raw_log_root=%s\n' "$log_root"
