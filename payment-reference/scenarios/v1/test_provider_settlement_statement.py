#!/usr/bin/env python3
"""Focused executable tests for provider-settlement-statement-v1."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRODUCER_PATH = HERE / "produce-provider-settlement-statement.py"
ROSTER_PATH = HERE / "settlement-roster.fixture.json"

spec = importlib.util.spec_from_file_location(
    "payment_reference_settlement_producer",
    PRODUCER_PATH,
)
if spec is None or spec.loader is None:
    raise RuntimeError(f"unable to load producer module: {PRODUCER_PATH}")
producer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = producer
spec.loader.exec_module(producer)


def provider_payments() -> dict[str, dict[str, object]]:
    return {
        "provider-payment-0001": {
            "id": "provider-payment-0001",
            "currency": "USD",
            "status": "captured",
            "captured_amount": 1_000,
            "refunded_amount": 0,
            "version": 2,
        },
        "provider-payment-0002": {
            "id": "provider-payment-0002",
            "currency": "USD",
            "status": "refunded",
            "captured_amount": 2_000,
            "refunded_amount": 500,
            "version": 4,
        },
        "provider-payment-0003": {
            "id": "provider-payment-0003",
            "currency": "USD",
            "status": "refunded",
            "captured_amount": 3_000,
            "refunded_amount": 3_000,
            "version": 6,
        },
    }


class ProviderHandler(BaseHTTPRequestHandler):
    payments: dict[str, dict[str, object]] = {}
    methods: list[str] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).methods.append(self.command)
        parsed = urllib.parse.urlparse(self.path)
        prefix = "/payments/"
        if not parsed.path.startswith(prefix):
            self.send_error(404)
            return
        payment_id = urllib.parse.unquote(parsed.path[len(prefix):])
        payment = type(self).payments.get(payment_id)
        if payment is None:
            self.send_error(404)
            return
        body = json.dumps(payment, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProviderServer:
    def __init__(self, payments: dict[str, dict[str, object]]) -> None:
        ProviderHandler.payments = copy.deepcopy(payments)
        ProviderHandler.methods = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    def __enter__(self) -> "ProviderServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


class ProviderSettlementStatementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = producer.validate_roster(
            json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
        )

    def produce(
        self,
        payments: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, object]:
        with ProviderServer(payments or provider_payments()) as server:
            statement = producer.produce_statement(
                self.roster,
                server.base_url,
                2.0,
            )
            self.assertTrue(ProviderHandler.methods)
            self.assertEqual(
                set(ProviderHandler.methods),
                {"GET"},
                "statement production must be read-only provider access",
            )
            return statement

    def test_deterministic_statement_totals_and_digest(self) -> None:
        first = self.produce()
        second = self.produce()
        self.assertEqual(first, second)
        self.assertEqual(first["line_count"], 3)
        self.assertEqual(first["captured_total_minor"], 6_000)
        self.assertEqual(first["refunded_total_minor"], 3_500)
        self.assertEqual(first["net_settlement_total_minor"], 2_500)
        self.assertEqual(
            [line["provider_payment_id"] for line in first["lines"]],
            sorted(self.roster["provider_payment_ids"]),
        )
        self.assertEqual(
            first["source_roster_digest"],
            producer.sha256_digest(self.roster),
        )
        unsigned = dict(first)
        observed_digest = unsigned.pop("statement_digest")
        self.assertEqual(observed_digest, producer.sha256_digest(unsigned))
        producer.validate_statement(first, self.roster)

    def test_statement_validation_binds_exact_roster_identity_and_membership(self) -> None:
        statement = self.produce()

        wrong_identity = copy.deepcopy(statement)
        wrong_identity["provider_account_id"] = "other-provider-account"
        wrong_identity["statement_id"] = producer.deterministic_id(
            "statement",
            producer.statement_identity(wrong_identity),
        )
        for line in wrong_identity["lines"]:
            line["line_id"] = producer.deterministic_id(
                "line",
                {
                    "statement_id": wrong_identity["statement_id"],
                    "provider_payment_id": line["provider_payment_id"],
                },
            )
        unsigned = dict(wrong_identity)
        unsigned.pop("statement_digest")
        wrong_identity["statement_digest"] = producer.sha256_digest(unsigned)
        with self.assertRaisesRegex(
            producer.SettlementStatementError,
            "provider_account_id does not match input roster",
        ):
            producer.validate_statement(wrong_identity, self.roster)

        wrong_membership = copy.deepcopy(statement)
        wrong_membership["lines"][0]["provider_payment_id"] = "provider-payment-0000"
        wrong_membership["lines"][0]["line_id"] = producer.deterministic_id(
            "line",
            {
                "statement_id": wrong_membership["statement_id"],
                "provider_payment_id": "provider-payment-0000",
            },
        )
        unsigned = dict(wrong_membership)
        unsigned.pop("statement_digest")
        wrong_membership["statement_digest"] = producer.sha256_digest(unsigned)
        with self.assertRaisesRegex(
            producer.SettlementStatementError,
            "membership does not match input roster",
        ):
            producer.validate_statement(wrong_membership, self.roster)

    def test_ineligible_provider_member_fails_whole_statement(self) -> None:
        payments = provider_payments()
        payments["provider-payment-0002"]["status"] = "authorized"
        with ProviderServer(payments) as server:
            with self.assertRaisesRegex(
                producer.SettlementStatementError,
                "not settlement-eligible",
            ):
                producer.produce_statement(
                    self.roster,
                    server.base_url,
                    2.0,
                )
            self.assertEqual(set(ProviderHandler.methods), {"GET"})

    def test_immutable_create_replay_and_same_identity_conflict(self) -> None:
        statement = self.produce()
        with tempfile.TemporaryDirectory() as directory:
            retained = Path(directory) / "provider-settlement-statement-v1.json"

            self.assertEqual(
                producer.retain_immutable_statement(
                    retained,
                    statement,
                    self.roster,
                ),
                "created",
            )
            before_stat = retained.stat()
            before_bytes = retained.read_bytes()

            self.assertEqual(
                producer.retain_immutable_statement(
                    retained,
                    statement,
                    self.roster,
                ),
                "replay",
            )
            after_replay_stat = retained.stat()
            self.assertEqual(before_stat.st_ino, after_replay_stat.st_ino)
            self.assertEqual(
                before_stat.st_mtime_ns,
                after_replay_stat.st_mtime_ns,
            )
            self.assertEqual(before_bytes, retained.read_bytes())

            conflict = copy.deepcopy(statement)
            conflict["lines"][0]["provider_version"] += 1
            unsigned = dict(conflict)
            unsigned.pop("statement_digest")
            conflict["statement_digest"] = producer.sha256_digest(unsigned)

            with self.assertRaisesRegex(
                producer.SettlementStatementError,
                "same identity has different immutable content",
            ):
                producer.retain_immutable_statement(
                    retained,
                    conflict,
                    self.roster,
                )
            self.assertEqual(before_bytes, retained.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
