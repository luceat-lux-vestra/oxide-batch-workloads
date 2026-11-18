#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

SCHEMA = "oxide-batch-workloads.payment-reference-image-manifest"
SCHEMA_VERSION = 1
ITEM_SCHEMA = "oxide-batch-workloads.payment-reference-published-image"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
DIGEST_REF = re.compile(r"^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")

EXPECTED = {
    "killbill-db": "PAYMENT_REFERENCE_IMAGE_KILLBILL_DB",
    "postgres": "PAYMENT_REFERENCE_IMAGE_POSTGRES",
    "payment-sandbox": "PAYMENT_REFERENCE_IMAGE_PAYMENT_SANDBOX",
    "fault-proxy": "PAYMENT_REFERENCE_IMAGE_FAULT_PROXY",
    "killbill": "PAYMENT_REFERENCE_IMAGE_KILLBILL",
    "gateway": "PAYMENT_REFERENCE_IMAGE_GATEWAY",
    "merchant": "PAYMENT_REFERENCE_IMAGE_MERCHANT",
}


class ManifestError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"unable to read JSON {path}: {exc}") from exc


def validate_item(value: Any, source_sha: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("published image item must be an object")
    required = {
        "schema",
        "schema_version",
        "service",
        "env_name",
        "source_sha",
        "platform",
        "tag_ref",
        "digest_ref",
        "source_image",
        "source_digest_ref",
    }
    if set(value) != required:
        raise ManifestError(f"published image item keys disagree with v1: {sorted(value)}")
    if value["schema"] != ITEM_SCHEMA or value["schema_version"] != 1:
        raise ManifestError("published image item schema mismatch")
    service = value["service"]
    if service not in EXPECTED:
        raise ManifestError(f"unexpected service {service!r}")
    if value["env_name"] != EXPECTED[service]:
        raise ManifestError(f"unexpected env mapping for {service!r}")
    if value["source_sha"] != source_sha:
        raise ManifestError(f"source SHA mismatch for {service!r}")
    if value["platform"] != "linux/arm64":
        raise ManifestError(f"unexpected platform for {service!r}: {value['platform']!r}")
    digest_ref = value["digest_ref"]
    if not isinstance(digest_ref, str) or not DIGEST_REF.fullmatch(digest_ref):
        raise ManifestError(f"invalid digest reference for {service!r}: {digest_ref!r}")
    tag_ref = value["tag_ref"]
    if not isinstance(tag_ref, str) or "@sha256:" in tag_ref or ":sha-" not in tag_ref:
        raise ManifestError(f"invalid publication tag for {service!r}: {tag_ref!r}")
    if service == "postgres":
        if value["source_image"] != "postgres:18.6-bookworm":
            raise ManifestError("postgres mirror source tag changed")
        source_digest = value["source_digest_ref"]
        if not isinstance(source_digest, str) or "@sha256:" not in source_digest:
            raise ManifestError("postgres mirror source digest is required")
    else:
        if value["source_image"] is not None or value["source_digest_ref"] is not None:
            raise ManifestError(f"repository-owned image {service!r} has unexpected source image")
    return value


def assemble(input_dir: Path, source_sha: str) -> dict[str, Any]:
    if not HEX40.fullmatch(source_sha):
        raise ManifestError("source SHA must be lowercase 40-hex")
    files = sorted(input_dir.rglob("*.json"))
    if not files:
        raise ManifestError(f"no image JSON files found under {input_dir}")
    items: dict[str, dict[str, Any]] = {}
    for path in files:
        item = validate_item(read_json(path), source_sha)
        service = item["service"]
        if service in items:
            raise ManifestError(f"duplicate image item for {service!r}")
        items[service] = item
    if set(items) != set(EXPECTED):
        raise ManifestError(
            f"image service set mismatch: expected={sorted(EXPECTED)} observed={sorted(items)}"
        )
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source_sha": source_sha,
        "platform": "linux/arm64",
        "images": [items[name] for name in sorted(items)],
    }


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("image manifest must be an object")
    if set(value) != {"schema", "schema_version", "source_sha", "platform", "images"}:
        raise ManifestError("image manifest keys disagree with v1")
    if value["schema"] != SCHEMA or value["schema_version"] != SCHEMA_VERSION:
        raise ManifestError("image manifest schema mismatch")
    source_sha = value["source_sha"]
    if not isinstance(source_sha, str) or not HEX40.fullmatch(source_sha):
        raise ManifestError("image manifest source SHA is invalid")
    if value["platform"] != "linux/arm64":
        raise ManifestError("image manifest platform must be linux/arm64")
    images = value["images"]
    if not isinstance(images, list):
        raise ManifestError("image manifest images must be an array")
    observed: dict[str, dict[str, Any]] = {}
    for raw in images:
        item = validate_item(raw, source_sha)
        service = item["service"]
        if service in observed:
            raise ManifestError(f"duplicate manifest service {service!r}")
        observed[service] = item
    if set(observed) != set(EXPECTED):
        raise ManifestError(
            f"manifest service set mismatch: expected={sorted(EXPECTED)} observed={sorted(observed)}"
        )
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    path.write_text(raw, encoding="utf-8")


def write_env(path: Path, manifest: dict[str, Any]) -> None:
    value = validate_manifest(manifest)
    lines = []
    for item in sorted(value["images"], key=lambda item: item["env_name"]):
        lines.append(f"{item['env_name']}={item['digest_ref']}")
    raw = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble and validate #318 immutable image manifests")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("assemble")
    build.add_argument("--input-dir", type=Path, required=True)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--out", type=Path, required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--source-sha")

    env = sub.add_parser("write-env")
    env.add_argument("--manifest", type=Path, required=True)
    env.add_argument("--out", type=Path, required=True)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "assemble":
            value = assemble(args.input_dir, args.source_sha)
            write_json(args.out, value)
            print(args.out)
        elif args.command == "verify":
            value = validate_manifest(read_json(args.manifest))
            if args.source_sha is not None and value["source_sha"] != args.source_sha:
                raise ManifestError(
                    f"manifest source SHA {value['source_sha']} != expected {args.source_sha}"
                )
            print(json.dumps({"decision": "ALLOW", "source_sha": value["source_sha"]}, sort_keys=True))
        elif args.command == "write-env":
            value = validate_manifest(read_json(args.manifest))
            write_env(args.out, value)
            print(args.out)
        else:
            raise AssertionError(args.command)
    except (ManifestError, OSError) as exc:
        print(f"image manifest error: {exc}", file=sys.stderr)
        return 65
    return 0


if __name__ == "__main__":
    sys.exit(main())
