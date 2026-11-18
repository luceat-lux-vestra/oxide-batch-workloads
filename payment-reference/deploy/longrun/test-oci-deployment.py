#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
REPO = DEPLOY.parent.parent


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest_mod = load_module("image_manifest", "image-manifest.py")

SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "a" * 64


def item(service: str, env_name: str) -> dict:
    repository = {
        "killbill-db": "oxide-payment-killbill-db",
        "postgres": "oxide-payment-postgres",
        "payment-sandbox": "oxide-payment-sandbox",
        "fault-proxy": "oxide-payment-fault-proxy",
        "killbill": "oxide-payment-killbill",
        "gateway": "oxide-payment-gateway",
        "merchant": "oxide-payment-merchant",
    }[service]
    value = {
        "schema": "oxide-batch-workloads.payment-reference-published-image",
        "schema_version": 1,
        "service": service,
        "env_name": env_name,
        "source_sha": SHA,
        "platform": "linux/arm64",
        "tag_ref": f"ghcr.io/luceat-lux-vestra/{repository}:sha-{SHA}-run-1-a1",
        "digest_ref": f"ghcr.io/luceat-lux-vestra/{repository}@sha256:{DIGEST}",
        "source_image": None,
        "source_digest_ref": None,
    }
    if service == "postgres":
        value["source_image"] = "postgres:18.6-bookworm"
        value["source_digest_ref"] = f"postgres@sha256:{'b' * 64}"
    return value


class OciDeploymentTests(unittest.TestCase):
    def test_oci_compose_is_structurally_no_build_and_requires_every_digest(self) -> None:
        text = (HERE / "compose.oci.yaml").read_text(encoding="utf-8")
        self.assertNotIn("\n    build:", text)
        for env_name in manifest_mod.EXPECTED.values():
            self.assertIn(f"${{{env_name}:?", text)
        self.assertEqual(text.count("PAYMENT_REFERENCE_IMAGE_POSTGRES:?"), 2)
        self.assertIn("127.0.0.1:${KILLBILL_PORT:-8080}:8080", text)
        self.assertIn("127.0.0.1:${GATEWAY_PORT:-18082}:8080", text)
        self.assertIn("127.0.0.1:${MERCHANT_PORT:-18083}:8080", text)

    def test_manifest_assembly_requires_exact_seven_service_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for service, env_name in manifest_mod.EXPECTED.items():
                (root / f"{service}.json").write_text(
                    json.dumps(item(service, env_name)),
                    encoding="utf-8",
                )
            value = manifest_mod.assemble(root, SHA)
            self.assertEqual(value["source_sha"], SHA)
            self.assertEqual(value["platform"], "linux/arm64")
            self.assertEqual(
                {entry["service"] for entry in value["images"]},
                set(manifest_mod.EXPECTED),
            )

            (root / "merchant.json").unlink()
            with self.assertRaises(manifest_mod.ManifestError):
                manifest_mod.assemble(root, SHA)

    def test_manifest_env_is_immutable_replay_safe(self) -> None:
        manifest = {
            "schema": manifest_mod.SCHEMA,
            "schema_version": 1,
            "source_sha": SHA,
            "platform": "linux/arm64",
            "images": [
                item(service, env_name)
                for service, env_name in sorted(manifest_mod.EXPECTED.items())
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "images.env"
            manifest_mod.write_env(path, manifest)
            before = path.read_bytes()
            manifest_mod.write_env(path, manifest)
            self.assertEqual(path.read_bytes(), before)
            changed = json.loads(json.dumps(manifest))
            changed["images"][0]["digest_ref"] = (
                changed["images"][0]["digest_ref"][:-64] + ("c" * 64)
            )
            with self.assertRaises(manifest_mod.ManifestError):
                manifest_mod.write_env(path, changed)

    def test_publish_workflow_is_main_only_and_covers_exact_image_set(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "payment-reference-images.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("push:", workflow)
        self.assertIn("branches: [main]", workflow)
        self.assertIn("workflow_dispatch: {}", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("runs-on: ubuntu-24.04-arm", workflow)
        self.assertIn("publish-image.sh", workflow)
        self.assertIn("image-manifest.py assemble", workflow)
        for service in manifest_mod.EXPECTED:
            self.assertIn(f"          - {service}\n", workflow)

    def test_deploy_and_smoke_paths_fail_closed_against_builds(self) -> None:
        deploy = (HERE / "deploy-oci.sh").read_text(encoding="utf-8")
        smoke = (DEPLOY / "smoke.sh").read_text(encoding="utf-8")
        self.assertEqual(
            deploy.count("check-admission.py --data-path"),
            2,
        )
        self.assertIn("config --format json", deploy)
        self.assertIn('if "build" in service:', deploy)
        self.assertIn("pull", deploy)
        self.assertIn("up -d --no-build --pull never", deploy)
        self.assertIn("PAYMENT_REFERENCE_COMPOSE_FILE:-compose.yaml", smoke)
        self.assertIn("PAYMENT_REFERENCE_COMPOSE_ENV_FILE:-", smoke)
        self.assertIn("up -d --no-build --pull never", smoke)

    def test_publish_script_uses_unique_run_attempt_tag_and_digest_output(self) -> None:
        script = (HERE / "publish-image.sh").read_text(encoding="utf-8")
        self.assertIn("sha-$source_sha-run-$GITHUB_RUN_ID-a$GITHUB_RUN_ATTEMPT", script)
        self.assertIn("docker build", script)
        self.assertIn('docker push "$tag_ref"', script)
        self.assertIn('docker pull --platform linux/arm64 "$tag_ref"', script)
        self.assertIn("digest_ref", script)
        self.assertIn("postgres:18.6-bookworm", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
