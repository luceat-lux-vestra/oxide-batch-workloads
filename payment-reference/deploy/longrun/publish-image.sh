#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <service> <registry-namespace> <source-sha> <output-json>" >&2
  exit 64
}

[ "$#" -eq 4 ] || usage

service="$1"
registry_namespace="${2%/}"
source_sha="$3"
output_json="$4"

[[ "$source_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "source SHA must be lowercase 40-hex" >&2
  exit 64
}
[[ "$registry_namespace" =~ ^ghcr\.io/[a-z0-9._-]+$ ]] || {
  echo "registry namespace must be ghcr.io/<owner>" >&2
  exit 64
}
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] || {
  echo "GITHUB_RUN_ID is required" >&2
  exit 64
}
[[ "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ ]] || {
  echo "GITHUB_RUN_ATTEMPT is required" >&2
  exit 64
}

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
actual_sha="$(git rev-parse HEAD)"
[ "$actual_sha" = "$source_sha" ] || {
  echo "checked out SHA $actual_sha does not match requested source SHA $source_sha" >&2
  exit 65
}
[ -z "$(git status --porcelain)" ] || {
  echo "image publication requires a clean worktree" >&2
  exit 65
}

case "$service" in
  killbill-db)
    image_name="oxide-payment-killbill-db"
    env_name="PAYMENT_REFERENCE_IMAGE_KILLBILL_DB"
    dockerfile="payment-reference/deploy/killbill-db/Dockerfile"
    context="."
    ;;
  postgres)
    image_name="oxide-payment-postgres"
    env_name="PAYMENT_REFERENCE_IMAGE_POSTGRES"
    source_image="postgres:18.6-bookworm"
    ;;
  payment-sandbox)
    image_name="oxide-payment-sandbox"
    env_name="PAYMENT_REFERENCE_IMAGE_PAYMENT_SANDBOX"
    dockerfile="payment-reference/deploy/payment-sandbox/Dockerfile"
    context="payment-reference/deploy/payment-sandbox"
    ;;
  fault-proxy)
    image_name="oxide-payment-fault-proxy"
    env_name="PAYMENT_REFERENCE_IMAGE_FAULT_PROXY"
    dockerfile="payment-reference/deploy/fault-proxy/Dockerfile"
    context="."
    ;;
  killbill)
    image_name="oxide-payment-killbill"
    env_name="PAYMENT_REFERENCE_IMAGE_KILLBILL"
    dockerfile="payment-reference/deploy/killbill/Dockerfile"
    context="."
    ;;
  gateway)
    image_name="oxide-payment-gateway"
    env_name="PAYMENT_REFERENCE_IMAGE_GATEWAY"
    dockerfile="payment-reference/deploy/gateway/Dockerfile"
    context="."
    ;;
  merchant)
    image_name="oxide-payment-merchant"
    env_name="PAYMENT_REFERENCE_IMAGE_MERCHANT"
    dockerfile="payment-reference/deploy/merchant/Dockerfile"
    context="."
    ;;
  *)
    echo "unsupported service: $service" >&2
    exit 64
    ;;
esac

image_repo="$registry_namespace/$image_name"
tag="sha-$source_sha-run-$GITHUB_RUN_ID-a$GITHUB_RUN_ATTEMPT"
tag_ref="$image_repo:$tag"
source_digest_ref=""

if [ "$service" = "postgres" ]; then
  docker pull --platform linux/arm64 "$source_image"
  source_digest_ref="$(
    docker image inspect "$source_image" --format '{{json .RepoDigests}}' |
      jq -er 'map(select(contains("@sha256:"))) | first'
  )"
  docker tag "$source_image" "$tag_ref"
else
  docker build     --pull     --platform linux/arm64     --label "org.opencontainers.image.source=https://github.com/${GITHUB_REPOSITORY}"     --label "org.opencontainers.image.revision=$source_sha"     --label "org.opencontainers.image.title=$image_name"     --file "$dockerfile"     --tag "$tag_ref"     "$context"
fi

local_arch="$(docker image inspect "$tag_ref" --format '{{.Architecture}}')"
[ "$local_arch" = "arm64" ] || {
  echo "built image architecture is $local_arch, expected arm64" >&2
  exit 65
}

docker push "$tag_ref"
docker image rm "$tag_ref" >/dev/null 2>&1 || true
docker pull --platform linux/arm64 "$tag_ref"

digest_ref="$(
  docker image inspect "$tag_ref" --format '{{json .RepoDigests}}' |
    jq -er --arg prefix "$image_repo@sha256:" '
      map(select(startswith($prefix))) |
      if length == 1 then .[0] else error("expected exactly one pushed digest ref") end
    '
)"
[[ "$digest_ref" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "invalid pushed digest reference: $digest_ref" >&2
  exit 65
}

mkdir -p "$(dirname "$output_json")"
jq -n   --arg service "$service"   --arg env_name "$env_name"   --arg source_sha "$source_sha"   --arg platform "linux/arm64"   --arg tag_ref "$tag_ref"   --arg digest_ref "$digest_ref"   --arg source_image "${source_image:-}"   --arg source_digest_ref "$source_digest_ref"   '{
    schema: "oxide-batch-workloads.payment-reference-published-image",
    schema_version: 1,
    service: $service,
    env_name: $env_name,
    source_sha: $source_sha,
    platform: $platform,
    tag_ref: $tag_ref,
    digest_ref: $digest_ref,
    source_image: (if $source_image == "" then null else $source_image end),
    source_digest_ref: (if $source_digest_ref == "" then null else $source_digest_ref end)
  }' > "$output_json"

jq -e . "$output_json" >/dev/null
cat "$output_json"
