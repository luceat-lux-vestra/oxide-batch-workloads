#!/usr/bin/env python3
import copy
import threading


class WebhookScheduleError(ValueError):
    pass


class WebhookDeliveryScheduler:
    """Deterministic logical-time scheduler for webhook delivery attempts.

    This component does not perform network I/O. It turns registered webhook
    events plus WEBHOOK_DELIVERY plan steps into auditable delivery/drop
    envelopes. A later integration checkpoint may hand delivered envelopes to
    a concrete Merchant receiver.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._events = {}
            self._attempt_keys = set()
            self._audit = []
            self._delivered = []

    def register_event(self, event_id, payload, metadata=None):
        event_id = _non_blank(event_id, "event_id")
        if not isinstance(payload, dict):
            raise WebhookScheduleError("payload must be a JSON object")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise WebhookScheduleError("metadata must be a JSON object")

        candidate = {
            "event_id": event_id,
            "payload": copy.deepcopy(payload),
            "metadata": copy.deepcopy(metadata),
        }
        with self._lock:
            existing = self._events.get(event_id)
            if existing is not None:
                if existing != candidate:
                    raise WebhookScheduleError(
                        f"conflicting webhook event registration: {event_id}"
                    )
                return copy.deepcopy(existing)

            self._events[event_id] = candidate
            return copy.deepcopy(candidate)

    def dispatch(self, step, dispatched_at_ms):
        dispatched_at_ms = _non_negative_int(dispatched_at_ms, "dispatched_at_ms")
        if not isinstance(step, dict):
            raise WebhookScheduleError("webhook step must be an object")
        if step.get("point") != "WEBHOOK_DELIVERY":
            raise WebhookScheduleError("webhook scheduler requires WEBHOOK_DELIVERY step")

        action = step.get("action")
        if not isinstance(action, dict) or action.get("kind") != "WEBHOOK":
            raise WebhookScheduleError("webhook step requires WEBHOOK action")

        event_id = _non_blank(action.get("event_id"), "action.event_id")
        attempt = _positive_int(action.get("attempt"), "action.attempt")
        delivery = _non_blank(action.get("delivery"), "action.delivery")
        if delivery not in {"DELIVER", "DROP"}:
            raise WebhookScheduleError(f"unsupported webhook delivery: {delivery}")

        step_id = _non_blank(step.get("step_id"), "step_id")
        scheduled_offset_ms = _non_negative_int(step.get("offset_ms"), "offset_ms")
        sequence = _non_negative_int(step.get("sequence"), "sequence")

        with self._lock:
            event = self._events.get(event_id)
            if event is None:
                raise WebhookScheduleError(
                    f"webhook event is not registered: {event_id}"
                )

            key = (event_id, attempt)
            if key in self._attempt_keys:
                raise WebhookScheduleError(
                    f"webhook attempt already dispatched: event_id={event_id} attempt={attempt}"
                )
            self._attempt_keys.add(key)

            record = {
                "step_id": step_id,
                "sequence": sequence,
                "scheduled_offset_ms": scheduled_offset_ms,
                "dispatched_at_ms": dispatched_at_ms,
                "event_id": event_id,
                "attempt": attempt,
                "delivery": delivery,
                "outcome": "DELIVERED" if delivery == "DELIVER" else "DROPPED",
                "event": copy.deepcopy(event),
            }
            self._audit.append(record)
            if delivery == "DELIVER":
                envelope = {
                    "event_id": event_id,
                    "attempt": attempt,
                    "scheduled_offset_ms": scheduled_offset_ms,
                    "dispatched_at_ms": dispatched_at_ms,
                    "payload": copy.deepcopy(event["payload"]),
                    "metadata": copy.deepcopy(event["metadata"]),
                }
                self._delivered.append(envelope)
                record["envelope"] = copy.deepcopy(envelope)

            return copy.deepcopy(record)

    def snapshot(self):
        with self._lock:
            return {
                "registered_events": [
                    copy.deepcopy(self._events[event_id])
                    for event_id in sorted(self._events)
                ],
                "attempts": copy.deepcopy(self._audit),
                "delivered_envelopes": copy.deepcopy(self._delivered),
            }


def _non_blank(value, field):
    if not isinstance(value, str) or not value.strip():
        raise WebhookScheduleError(f"{field} must be a non-blank string")
    return value


def _non_negative_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WebhookScheduleError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value, field):
    value = _non_negative_int(value, field)
    if value == 0:
        raise WebhookScheduleError(f"{field} must be greater than zero")
    return value
