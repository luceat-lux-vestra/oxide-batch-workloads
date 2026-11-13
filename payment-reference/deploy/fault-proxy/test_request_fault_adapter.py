import unittest

from request_fault_adapter import FaultPlanValidationError, RequestFaultPlanAdapter


def plan(*steps):
    return {
        "fault_plan_version": 1,
        "scenario_version": 1,
        "seed": 42,
        "logical_business_time": "2026-11-14T00:00:00Z",
        "scenario_case": "request-adapter-test",
        "expected_provider_truth": "AUTHORIZED",
        "expected_canonical_outcome": "PAYMENT_UNKNOWN/UNKNOWN",
        "steps": list(steps),
    }


def request_step(step_id, sequence, offset_ms, point, path_suffix, action):
    return {
        "step_id": step_id,
        "sequence": sequence,
        "offset_ms": offset_ms,
        "point": point,
        "request_match": {
            "method": "POST",
            "path_suffix": path_suffix,
        },
        "action": action,
    }


class RequestFaultPlanAdapterTest(unittest.TestCase):
    def test_http_failure_waits_for_matching_request_and_is_consumed_once(self):
        adapter = RequestFaultPlanAdapter()
        adapter.load(
            plan(
                request_step(
                    "provider-503",
                    1,
                    100,
                    "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                    "/payments",
                    {"kind": "HTTP_FAILURE", "status": 503},
                )
            )
        )

        self.assertIsNone(adapter.consume_before("POST", "/payments"))
        adapter.advance(100)
        self.assertIsNone(adapter.consume_before("POST", "/payments/kb-1/authorize"))

        decision = adapter.consume_before("POST", "/payments")
        self.assertEqual("RESPOND_BEFORE_UPSTREAM", decision["effect"])
        self.assertEqual(503, decision["status"])
        self.assertIsNone(adapter.consume_before("POST", "/payments"))

    def test_after_commit_drop_is_consumed_only_in_after_commit_phase(self):
        adapter = RequestFaultPlanAdapter()
        adapter.load(
            plan(
                request_step(
                    "commit-drop",
                    1,
                    0,
                    "REQUEST_AFTER_PROVIDER_COMMIT",
                    "/authorize",
                    {"kind": "COMMIT_THEN_DROP_RESPONSE"},
                )
            )
        )
        adapter.advance(0)

        self.assertIsNone(adapter.consume_before("POST", "/payments/kb-1/authorize"))
        decision = adapter.consume_after_commit("POST", "/payments/kb-1/authorize")
        self.assertEqual("DROP_AFTER_UPSTREAM_COMMIT", decision["effect"])
        self.assertIsNone(adapter.consume_after_commit("POST", "/payments/kb-1/authorize"))

    def test_non_request_steps_are_retained_as_deferred_not_executed(self):
        adapter = RequestFaultPlanAdapter()
        snapshot = adapter.load(
            plan(
                {
                    "step_id": "stale-read",
                    "sequence": 1,
                    "offset_ms": 50,
                    "point": "STATUS_INQUIRY",
                    "action": {"kind": "STATUS_VISIBILITY", "visibility": "STALE"},
                }
            )
        )

        self.assertEqual([], snapshot["scheduled_request_steps"])
        self.assertEqual("stale-read", snapshot["deferred_steps"][0]["step_id"])

    def test_permanent_business_failure_has_explicit_non_retryable_4xx_status(self):
        adapter = RequestFaultPlanAdapter()
        adapter.load(
            plan(
                request_step(
                    "decline",
                    1,
                    0,
                    "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                    "/authorize",
                    {
                        "kind": "PERMANENT_BUSINESS_FAILURE",
                        "status": 422,
                        "code": "payment_declined",
                    },
                )
            )
        )
        adapter.advance(0)

        decision = adapter.consume_before("POST", "/payments/kb-1/authorize")
        self.assertEqual(422, decision["status"])
        self.assertEqual("payment_declined", decision["error_code"])

        with self.assertRaises(FaultPlanValidationError):
            adapter.load(
                plan(
                    request_step(
                        "bad-decline",
                        1,
                        0,
                        "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                        "/authorize",
                        {
                            "kind": "PERMANENT_BUSINESS_FAILURE",
                            "status": 429,
                            "code": "wrong-classification",
                        },
                    )
                )
            )

    def test_request_method_must_match_ascii_contract(self):
        adapter = RequestFaultPlanAdapter()
        invalid = request_step(
            "unicode-method",
            1,
            0,
            "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
            "/payments",
            {"kind": "HTTP_FAILURE", "status": 503},
        )
        invalid["request_match"]["method"] = "ＰＯＳＴ"
        with self.assertRaises(FaultPlanValidationError):
            adapter.load(plan(invalid))

    def test_backward_logical_time_is_rejected(self):
        adapter = RequestFaultPlanAdapter()
        adapter.load(
            plan(
                request_step(
                    "provider-503",
                    1,
                    100,
                    "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                    "/payments",
                    {"kind": "HTTP_FAILURE", "status": 503},
                )
            )
        )
        adapter.advance(100)
        with self.assertRaises(FaultPlanValidationError):
            adapter.advance(99)


if __name__ == "__main__":
    unittest.main()
