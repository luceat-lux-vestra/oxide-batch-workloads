import unittest

from request_fault_adapter import FaultPlanValidationError, RequestFaultPlanAdapter


def plan(*steps):
    return {
        "fault_plan_version": 1,
        "scenario_version": 1,
        "seed": 42,
        "logical_business_time": "2026-11-14T00:00:00Z",
        "scenario_case": "adapter-test",
        "expected_provider_truth": "AUTHORIZED",
        "expected_canonical_outcome": "PAYMENT_UNKNOWN/UNKNOWN",
        "steps": list(steps),
    }


def request_step(step_id, sequence, offset_ms, point, path_suffix, action, method="POST"):
    return {
        "step_id": step_id,
        "sequence": sequence,
        "offset_ms": offset_ms,
        "point": point,
        "request_match": {"method": method, "path_suffix": path_suffix},
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

    def test_async_defer_and_timeline_transition_share_operation_identity(self):
        adapter = RequestFaultPlanAdapter()
        adapter.load(
            plan(
                request_step(
                    "defer",
                    1,
                    0,
                    "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT",
                    "/authorize",
                    {"kind": "DEFER_ASYNC", "operation_id": "async-1"},
                ),
                {
                    "step_id": "transition",
                    "sequence": 2,
                    "offset_ms": 100,
                    "point": "PROVIDER_TRANSITION",
                    "action": {
                        "kind": "PROVIDER_TRANSITION",
                        "operation_id": "async-1",
                        "outcome": "SUCCEEDED",
                    },
                },
            )
        )

        adapter.advance(0)
        decision = adapter.consume_before("POST", "/payments/kb-1/authorize")
        self.assertEqual("DEFER_ASYNC", decision["effect"])
        self.assertEqual("async-1", decision["operation_id"])

        advanced = adapter.advance(100)
        self.assertEqual("transition", advanced["due_events"][0]["step_id"])

    def test_status_visibility_is_timeline_event_with_get_selector(self):
        adapter = RequestFaultPlanAdapter()
        adapter.load(
            plan(
                request_step(
                    "stale",
                    1,
                    50,
                    "STATUS_INQUIRY",
                    "/payments/kb-1",
                    {"kind": "STATUS_VISIBILITY", "visibility": "STALE"},
                    method="GET",
                )
            )
        )

        advanced = adapter.advance(50)
        event = advanced["due_events"][0]
        self.assertEqual("STATUS_INQUIRY", event["point"])
        self.assertEqual("GET", event["request_match"]["method"])

    def test_transition_without_matching_defer_is_rejected(self):
        adapter = RequestFaultPlanAdapter()
        with self.assertRaises(FaultPlanValidationError):
            adapter.load(
                plan(
                    {
                        "step_id": "orphan-transition",
                        "sequence": 1,
                        "offset_ms": 10,
                        "point": "PROVIDER_TRANSITION",
                        "action": {
                            "kind": "PROVIDER_TRANSITION",
                            "operation_id": "missing",
                            "outcome": "FAILED",
                        },
                    }
                )
            )

    def test_webhook_is_scheduled_as_timeline_event(self):
        adapter = RequestFaultPlanAdapter()
        adapter.load(
            plan(
                {
                    "step_id": "webhook",
                    "sequence": 1,
                    "offset_ms": 10,
                    "point": "WEBHOOK_DELIVERY",
                    "action": {
                        "kind": "WEBHOOK",
                        "delivery": "DELIVER",
                        "event_id": "event-1",
                        "attempt": 1,
                    },
                }
            )
        )
        self.assertEqual([], adapter.advance(9)["due_events"])
        due = adapter.advance(10)["due_events"]
        self.assertEqual("webhook", due[0]["step_id"])

    def test_duplicate_webhook_attempt_is_rejected(self):
        adapter = RequestFaultPlanAdapter()
        with self.assertRaises(FaultPlanValidationError):
            adapter.load(
                plan(
                    {
                        "step_id": "first",
                        "sequence": 1,
                        "offset_ms": 10,
                        "point": "WEBHOOK_DELIVERY",
                        "action": {
                            "kind": "WEBHOOK",
                            "delivery": "DROP",
                            "event_id": "event-1",
                            "attempt": 1,
                        },
                    },
                    {
                        "step_id": "duplicate",
                        "sequence": 2,
                        "offset_ms": 20,
                        "point": "WEBHOOK_DELIVERY",
                        "action": {
                            "kind": "WEBHOOK",
                            "delivery": "DELIVER",
                            "event_id": "event-1",
                            "attempt": 1,
                        },
                    },
                )
            )

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
