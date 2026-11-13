import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import proxy


def plan(*steps):
    return {
        "fault_plan_version": 1,
        "scenario_version": 1,
        "seed": 42,
        "logical_business_time": "2026-11-14T00:00:00Z",
        "scenario_case": "proxy-status-async",
        "expected_provider_truth": "AUTHORIZED",
        "expected_canonical_outcome": "PAYMENT_UNKNOWN/UNKNOWN",
        "steps": list(steps),
    }


def selected_step(step_id, sequence, offset_ms, point, path_suffix, action, method="POST"):
    return {
        "step_id": step_id,
        "sequence": sequence,
        "offset_ms": offset_ms,
        "point": point,
        "request_match": {"method": method, "path_suffix": path_suffix},
        "action": action,
    }


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path != "/payments/provider-test":
            self._json(404, {"error": {"code": "payment_not_found"}})
            return
        self._json(200, self.server.payment)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self.server.request_count += 1
        if self.path == "/payments/provider-test/authorize":
            self.server.payment = dict(self.server.payment)
            self.server.payment["status"] = "authorized"
            self.server.payment["authorized_amount"] = self.server.payment["amount"]
            self.server.payment["version"] += 1
            self._json(200, self.server.payment)
            return
        self._json(404, {"error": {"code": "not_found"}})

    def _json(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ProxyStatusAsyncIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.request_count = 0
        self.upstream.payment = {
            "id": "provider-test",
            "amount": 1000,
            "currency": "USD",
            "status": "pending",
            "authorized_amount": 0,
            "captured_amount": 0,
            "refunded_amount": 0,
            "version": 1,
        }
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()

        proxy.UPSTREAM_HOST = "127.0.0.1"
        proxy.UPSTREAM_PORT = self.upstream.server_address[1]
        proxy.STATE = proxy.FaultState()

        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.proxy_port = self.proxy.server_address[1]

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def request_json(self, method, path, payload=None):
        body = b""
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=2)
        connection.request(method, path, body=body or None, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data.decode("utf-8"))

    def test_async_success_with_stale_then_immediate_visibility(self):
        configured = plan(
            selected_step(
                "defer-authorize",
                1,
                0,
                "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                "/authorize",
                {"kind": "DEFER_ASYNC", "operation_id": "async-auth"},
            ),
            selected_step(
                "stale-status",
                2,
                50,
                "STATUS_INQUIRY",
                "/payments/provider-test",
                {"kind": "STATUS_VISIBILITY", "visibility": "STALE"},
                method="GET",
            ),
            {
                "step_id": "authorize-success",
                "sequence": 3,
                "offset_ms": 100,
                "point": "PROVIDER_TRANSITION",
                "action": {
                    "kind": "PROVIDER_TRANSITION",
                    "operation_id": "async-auth",
                    "outcome": "SUCCEEDED",
                },
            },
            selected_step(
                "status-converges",
                4,
                200,
                "STATUS_INQUIRY",
                "/payments/provider-test",
                {"kind": "STATUS_VISIBILITY", "visibility": "IMMEDIATE"},
                method="GET",
            ),
        )
        self.assertEqual(200, self.request_json("POST", "/__fault/load-plan", configured)[0])
        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 0})[0])

        status, pending = self.request_json("POST", "/payments/provider-test/authorize")
        self.assertEqual(200, status)
        self.assertEqual("pending", pending["status"])
        self.assertEqual(0, self.upstream.request_count)

        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 50})[0])
        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 100})[0])
        self.assertEqual(1, self.upstream.request_count)
        self.assertEqual("authorized", self.upstream.payment["status"])

        status, stale = self.request_json("GET", "/payments/provider-test")
        self.assertEqual(200, status)
        self.assertEqual("pending", stale["status"])

        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 200})[0])
        status, current = self.request_json("GET", "/payments/provider-test")
        self.assertEqual(200, status)
        self.assertEqual("authorized", current["status"])

    def test_async_failure_becomes_effective_provider_truth_without_upstream_mutation(self):
        configured = plan(
            selected_step(
                "defer-authorize",
                1,
                0,
                "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                "/authorize",
                {"kind": "DEFER_ASYNC", "operation_id": "async-auth"},
            ),
            {
                "step_id": "authorize-failed",
                "sequence": 2,
                "offset_ms": 100,
                "point": "PROVIDER_TRANSITION",
                "action": {
                    "kind": "PROVIDER_TRANSITION",
                    "operation_id": "async-auth",
                    "outcome": "FAILED",
                },
            },
        )
        self.assertEqual(200, self.request_json("POST", "/__fault/load-plan", configured)[0])
        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 0})[0])
        self.assertEqual(200, self.request_json("POST", "/payments/provider-test/authorize")[0])

        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 100})[0])
        self.assertEqual(0, self.upstream.request_count)
        self.assertEqual("pending", self.upstream.payment["status"])

        status, failed = self.request_json("GET", "/payments/provider-test")
        self.assertEqual(200, status)
        self.assertEqual("failed", failed["status"])

        stats = proxy.STATE.snapshot()
        self.assertEqual("FAILED", stats["transition_history"][0]["outcome"])
        self.assertFalse(stats["transition_history"][0]["provider_side_effect"])

    def test_not_found_visibility_is_bounded_by_immediate_step(self):
        configured = plan(
            selected_step(
                "hidden",
                1,
                10,
                "STATUS_INQUIRY",
                "/payments/provider-test",
                {"kind": "STATUS_VISIBILITY", "visibility": "NOT_FOUND"},
                method="GET",
            ),
            selected_step(
                "visible",
                2,
                20,
                "STATUS_INQUIRY",
                "/payments/provider-test",
                {"kind": "STATUS_VISIBILITY", "visibility": "IMMEDIATE"},
                method="GET",
            ),
        )
        self.assertEqual(200, self.request_json("POST", "/__fault/load-plan", configured)[0])
        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 10})[0])
        self.assertEqual(404, self.request_json("GET", "/payments/provider-test")[0])

        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 20})[0])
        status, payment = self.request_json("GET", "/payments/provider-test")
        self.assertEqual(200, status)
        self.assertEqual("pending", payment["status"])

    def test_before_side_effect_http_failure_never_reaches_upstream(self):
        configured = plan(
            selected_step(
                "provider-503",
                1,
                0,
                "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                "/authorize",
                {"kind": "HTTP_FAILURE", "status": 503},
            )
        )
        self.assertEqual(200, self.request_json("POST", "/__fault/load-plan", configured)[0])
        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 0})[0])

        status, body = self.request_json("POST", "/payments/provider-test/authorize")
        self.assertEqual(503, status)
        self.assertEqual("http_503", body["error"]["code"])
        self.assertEqual(0, self.upstream.request_count)

    def test_after_commit_drop_reaches_upstream_once_then_loses_response(self):
        configured = plan(
            selected_step(
                "commit-drop",
                1,
                0,
                "REQUEST_AFTER_PROVIDER_COMMIT",
                "/authorize",
                {"kind": "COMMIT_THEN_DROP_RESPONSE"},
            )
        )
        self.assertEqual(200, self.request_json("POST", "/__fault/load-plan", configured)[0])
        self.assertEqual(200, self.request_json("POST", "/__fault/advance", {"offset_ms": 0})[0])

        connection = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=2)
        connection.request(
            "POST",
            "/payments/provider-test/authorize",
            body=b"",
            headers={"Content-Length": "0"},
        )
        with self.assertRaises(
            (http.client.RemoteDisconnected, ConnectionResetError, http.client.BadStatusLine)
        ):
            connection.getresponse()
        connection.close()

        self.assertEqual(1, self.upstream.request_count)
        self.assertEqual("authorized", self.upstream.payment["status"])


if __name__ == "__main__":
    unittest.main()
