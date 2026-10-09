#!/usr/bin/env bash
set -euo pipefail

# GitHub-hosted Ubuntu runners provide curl, tar, sha256sum, find, and install.
readonly DEFAULT_VERSION="0.20.2"
# Select only architecture-matched official pinned release binaries. Keep the
# x86_64 default for unaffected x64 scheduled/maintenance audit runners.
case "$(uname -m)" in
  x86_64)
    readonly DEFAULT_ARCHIVE="cargo-deny-0.20.2-x86_64-unknown-linux-musl.tar.gz"
    readonly DEFAULT_SHA256="9f12ed4c49936e09b48bf862b595cde2fe64fcbd9d74dfacac6131ca824c8d5f"
    ;;
  aarch64)
    readonly DEFAULT_ARCHIVE="cargo-deny-0.20.2-aarch64-unknown-linux-musl.tar.gz"
    readonly DEFAULT_SHA256="995c82be0defc7a025cae49a2aa2644ce8245c9a3318fc4103907c6a285e8c7d"
    ;;
  *)
    echo "::error::Unsupported cargo-deny runner architecture: $(uname -m)" >&2
    exit 2
    ;;
esac
readonly DEFAULT_URL="https://github.com/EmbarkStudios/cargo-deny/releases/download/0.20.2/${DEFAULT_ARCHIVE}"

version="$DEFAULT_VERSION"
archive_name="$DEFAULT_ARCHIVE"
url="$DEFAULT_URL"
sha256="$DEFAULT_SHA256"
runner_temp="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
install_root=""
path_file="${GITHUB_PATH:-}"

usage() {
  cat <<'EOF'
Usage: install-cargo-deny.sh [options]

Options are intended for deterministic installer tests. Production CI uses
the pinned defaults with no arguments.

  --url URL
  --sha256 HEX
  --version VERSION
  --archive-name NAME
  --install-root DIR
  --path-file FILE
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --url)
      url="$2"
      shift 2
      ;;
    --sha256)
      sha256="$2"
      shift 2
      ;;
    --version)
      version="$2"
      shift 2
      ;;
    --archive-name)
      archive_name="$2"
      shift 2
      ;;
    --install-root)
      install_root="$2"
      shift 2
      ;;
    --path-file)
      path_file="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "::error::unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$sha256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "::error::cargo-deny SHA-256 must be 64 lowercase hex characters" >&2
  exit 3
fi

if [ -z "$install_root" ]; then
  install_root="$runner_temp/cargo-deny-$version"
fi
if [ -z "$path_file" ]; then
  echo "::error::GITHUB_PATH is unset and --path-file was not provided" >&2
  exit 4
fi

work_dir="$(mktemp -d "$runner_temp/cargo-deny-install.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

archive="$work_dir/$archive_name"
extract_dir="$work_dir/extract"
bin_dir="$install_root/bin"

curl --fail --location --retry 3 --retry-delay 2   "$url"   --output "$archive"

# Do not extract or execute any downloaded bytes until the pinned digest passes.
echo "$sha256  $archive" | sha256sum --check --strict

mkdir -p "$extract_dir"
tar -xzf "$archive" -C "$extract_dir"

mapfile -t binaries < <(find "$extract_dir" -type f -name cargo-deny -print)
if [ "${#binaries[@]}" -ne 1 ]; then
  echo "::error::expected exactly one cargo-deny binary, found ${#binaries[@]}" >&2
  exit 5
fi

rm -rf "$install_root"
mkdir -p "$bin_dir"
install -m 0755 "${binaries[0]}" "$bin_dir/cargo-deny"

actual_version="$("$bin_dir/cargo-deny" --version)"
expected_version="cargo-deny $version"
if [ "$actual_version" != "$expected_version" ]; then
  echo "::error::cargo-deny version mismatch: expected '$expected_version', got '$actual_version'" >&2
  exit 6
fi

echo "$bin_dir" >> "$path_file"
echo "verified cargo-deny installed: $actual_version"
