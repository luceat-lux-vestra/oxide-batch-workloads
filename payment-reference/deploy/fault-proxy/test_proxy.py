import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import proxy


def plan(step):
    return {
        "fault_plan_version": 1,
        "scenario_version": 1,
        "seed": 42,
        "logical_business_time": "2026-11-14T00:00:00Z",
        "scenario_case": "proxy-request-fault",
        "expected_provider_truth": "AUTHORIZED",
        "expected_canonical_outcome": "PAYMENT_UNKNOWN/UNKNOWN",
        "steps": [step],
    }


def request_step(step_id, point, path_suffix, action):
    return {
        "step_id": step_id,
        "sequence": 1,
        "offset_ms": 0,
        "point": point,
        "request_match": {
            "method": "POST",
            "path_suffix": path_suffix,
        },
        "action": action,
    }


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self.server.request_count += 1
        body = b'{"id":"provider-test","status":"authorized"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProxyRequestFaultIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.request_count = 0
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()

        proxy.UPSTREAM_HOST = "127.0.0.1"
        proxy.UPSTREAM_PORT = self.upstream.server_address[1]
        proxy.STATE = proxy.FaultState()

        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True
        )
        self.proxy_thread.start()
        self.proxy_port = self.proxy.server_address[1]

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def post_json(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=2)
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
        return response.status, json.loads(response_body.decode("utf-8"))

    def test_before_side_effect_http_failure_never_reaches_upstream(self):
        status, _ = self.post_json(
            "/__fault/load-plan",
            plan(
                request_step(
                    "provider-503",
                    "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                    "/payments",
                    {"kind": "HTTP_FAILURE", "status": 503},
                )
            ),
        )
        self.assertEqual(200, status)
        self.assertEqual(200, self.post_json("/__fault/advance", {"offset_ms": 0})[0])

        status, body = self.post_json("/payments", {"id": "provider-test"})
        self.assertEqual(503, status)
        self.assertEqual("http_503", body["error"]["code"])
        self.assertEqual(0, self.upstream.request_count)

        _, stats = self.post_json("/__fault/advance", {"offset_ms": 0})
        self.assertEqual(
            "provider-503",
            stats["consumed_request_steps"][0]["step_id"],
        )

    def test_after_commit_drop_reaches_upstream_once_then_loses_response(self):
        status, _ = self.post_json(
            "/__fault/load-plan",
            plan(
                request_step(
                    "commit-drop",
                    "REQUEST_AFTER_PROVIDER_COMMIT",
                    "/authorize",
                    {"kind": "COMMIT_THEN_DROP_RESPONSE"},
                )
            ),
        )
        self.assertEqual(200, status)
        self.assertEqual(200, self.post_json("/__fault/advance", {"offset_ms": 0})[0])

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
        snapshot = proxy.STATE.snapshot()["plan"]
        self.assertEqual(
            "commit-drop",
            snapshot["consumed_request_steps"][0]["step_id"],
        )


if __name__ == "__main__":
    unittest.main()
