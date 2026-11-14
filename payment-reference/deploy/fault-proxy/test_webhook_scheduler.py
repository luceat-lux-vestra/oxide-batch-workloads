import unittest

from webhook_scheduler import WebhookDeliveryScheduler, WebhookScheduleError


def step(step_id, sequence, offset_ms, event_id, attempt, delivery):
    return {
        "step_id": step_id,
        "sequence": sequence,
        "offset_ms": offset_ms,
        "point": "WEBHOOK_DELIVERY",
        "action": {
            "kind": "WEBHOOK",
            "event_id": event_id,
            "attempt": attempt,
            "delivery": delivery,
        },
    }


class WebhookDeliverySchedulerTest(unittest.TestCase):
    def test_delayed_delivery_emits_only_when_dispatched(self):
        scheduler = WebhookDeliveryScheduler()
        scheduler.register_event("event-1", {"type": "payment.authorized"})

        record = scheduler.dispatch(
            step("deliver", 1, 100, "event-1", 1, "DELIVER"),
            100,
        )

        self.assertEqual("DELIVERED", record["outcome"])
        self.assertEqual(100, record["scheduled_offset_ms"])
        self.assertEqual(
            "payment.authorized",
            record["envelope"]["payload"]["type"],
        )

    def test_duplicate_delivery_uses_distinct_attempts(self):
        scheduler = WebhookDeliveryScheduler()
        scheduler.register_event("event-1", {"type": "payment.authorized"})

        scheduler.dispatch(step("first", 1, 100, "event-1", 1, "DELIVER"), 100)
        scheduler.dispatch(step("duplicate", 2, 200, "event-1", 2, "DELIVER"), 200)

        snapshot = scheduler.snapshot()
        self.assertEqual([1, 2], [item["attempt"] for item in snapshot["delivered_envelopes"]])

    def test_out_of_order_events_follow_plan_dispatch_order(self):
        scheduler = WebhookDeliveryScheduler()
        scheduler.register_event("event-created", {"type": "payment.created"})
        scheduler.register_event("event-authorized", {"type": "payment.authorized"})

        scheduler.dispatch(
            step("authorized-first", 2, 100, "event-authorized", 1, "DELIVER"),
            100,
        )
        scheduler.dispatch(
            step("created-late", 1, 200, "event-created", 1, "DELIVER"),
            200,
        )

        snapshot = scheduler.snapshot()
        self.assertEqual(
            ["event-authorized", "event-created"],
            [item["event_id"] for item in snapshot["delivered_envelopes"]],
        )

    def test_dropped_attempt_can_be_retried_later(self):
        scheduler = WebhookDeliveryScheduler()
        scheduler.register_event("event-1", {"type": "payment.authorized"})

        first = scheduler.dispatch(step("drop", 1, 100, "event-1", 1, "DROP"), 100)
        second = scheduler.dispatch(step("retry", 2, 300, "event-1", 2, "DELIVER"), 300)

        self.assertEqual("DROPPED", first["outcome"])
        self.assertEqual("DELIVERED", second["outcome"])
        snapshot = scheduler.snapshot()
        self.assertEqual(2, len(snapshot["attempts"]))
        self.assertEqual(1, len(snapshot["delivered_envelopes"]))

    def test_event_registration_is_idempotent_but_conflicts_are_rejected(self):
        scheduler = WebhookDeliveryScheduler()
        event = {"type": "payment.authorized"}
        scheduler.register_event("event-1", event)
        scheduler.register_event("event-1", event)

        with self.assertRaises(WebhookScheduleError):
            scheduler.register_event("event-1", {"type": "payment.failed"})


if __name__ == "__main__":
    unittest.main()
