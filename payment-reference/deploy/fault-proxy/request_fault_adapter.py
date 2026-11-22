#!/usr/bin/env python3
import copy
import re
import threading

FAULT_PLAN_VERSION = 1
REQUEST_BEFORE = "REQUEST_BEFORE_PROVIDER_SIDE_EFFECT"
REQUEST_AFTER = "REQUEST_AFTER_PROVIDER_COMMIT"
STATUS_INQUIRY = "STATUS_INQUIRY"
PROVIDER_TRANSITION = "PROVIDER_TRANSITION"
WEBHOOK_DELIVERY = "WEBHOOK_DELIVERY"
PROVIDER_PAYMENT_PLACEHOLDER = "{provider_payment_id}"
PROVIDER_PAYMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

_SELECTOR_POINTS = {REQUEST_BEFORE, REQUEST_AFTER, STATUS_INQUIRY}
_REQUEST_POINTS = {REQUEST_BEFORE, REQUEST_AFTER}
_TIMELINE_POINTS = {STATUS_INQUIRY, PROVIDER_TRANSITION, WEBHOOK_DELIVERY}

_ALLOWED_TRANSPORT = {"CONNECT", "REQUEST", "TIMEOUT", "CONNECTION_DROP"}
_ALLOWED_TRANSIENT_HTTP = {429, 502, 503, 504}
_ALLOWED_STATUS_VISIBILITY = {"IMMEDIATE", "NOT_FOUND", "STALE"}
_ALLOWED_ASYNC_OUTCOME = {"SUCCEEDED", "FAILED"}
_ALLOWED_WEBHOOK_DELIVERY = {"DELIVER", "DROP"}


class FaultPlanValidationError(ValueError):
    pass


class RequestFaultPlanAdapter:
    """Validates and schedules the bounded v1 fault-plan surface owned by the proxy."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._identity = None
            self._provider_payment_id = None
            self._now_ms = 0
            self._scheduled_request = []
            self._active_request = []
            self._scheduled_timeline = []
            self._consumed_request = []
            self._dispatched_timeline = []
            self._deferred = []

    def load(self, payload):
        normalized = _normalize_plan(payload)
        with self._lock:
            self._identity = normalized["identity"]
            self._now_ms = 0
            self._scheduled_request = normalized["request_steps"]
            self._active_request = []
            self._scheduled_timeline = normalized["timeline_steps"]
            self._consumed_request = []
            self._dispatched_timeline = []
            self._deferred = normalized["deferred_steps"]
            return self._snapshot_unlocked()

    def advance(self, offset_ms):
        offset_ms = _non_negative_int(offset_ms, "offset_ms")
        with self._lock:
            if offset_ms < self._now_ms:
                raise FaultPlanValidationError(
                    f"logical time cannot move backward: current={self._now_ms} requested={offset_ms}"
                )

            while (
                self._scheduled_request
                and self._scheduled_request[0]["offset_ms"] <= offset_ms
            ):
                self._active_request.append(self._scheduled_request.pop(0))

            due_events = []
            while (
                self._scheduled_timeline
                and self._scheduled_timeline[0]["offset_ms"] <= offset_ms
            ):
                step = self._resolve_step_unlocked(
                    self._scheduled_timeline.pop(0),
                    require_bound=True,
                )
                due_events.append(copy.deepcopy(step))
                self._dispatched_timeline.append(
                    {
                        "step_id": step["step_id"],
                        "point": step["point"],
                        "sequence": step["sequence"],
                        "offset_ms": step["offset_ms"],
                        "dispatched_at_ms": offset_ms,
                    }
                )

            self._now_ms = offset_ms
            return {
                "snapshot": self._snapshot_unlocked(),
                "due_events": due_events,
            }

    def consume_before(self, method, path):
        with self._lock:
            step = self._consume_matching_unlocked(REQUEST_BEFORE, method, path)
            if step is None:
                return None

            action = step["action"]
            kind = action["kind"]
            if kind == "TRANSPORT_FAILURE":
                return {
                    "effect": "DROP_BEFORE_UPSTREAM",
                    "transport_class": action["class"],
                    "step": step,
                }
            if kind == "HTTP_FAILURE":
                status = action["status"]
                return {
                    "effect": "RESPOND_BEFORE_UPSTREAM",
                    "status": status,
                    "error_code": f"http_{status}",
                    "message": f"synthetic HTTP {status}",
                    "step": step,
                }
            if kind == "PERMANENT_BUSINESS_FAILURE":
                return {
                    "effect": "RESPOND_BEFORE_UPSTREAM",
                    "status": action["status"],
                    "error_code": action["code"],
                    "message": "synthetic permanent business failure",
                    "step": step,
                }
            if kind == "DEFER_ASYNC":
                return {
                    "effect": "DEFER_ASYNC",
                    "operation_id": action["operation_id"],
                    "step": step,
                }
            raise AssertionError(f"validated unsupported before-side-effect action: {kind}")

    def consume_after_commit(self, method, path):
        with self._lock:
            step = self._consume_matching_unlocked(REQUEST_AFTER, method, path)
            if step is None:
                return None
            if step["action"]["kind"] != "COMMIT_THEN_DROP_RESPONSE":
                raise AssertionError("validated after-commit step has unexpected action")
            return {
                "effect": "DROP_AFTER_UPSTREAM_COMMIT",
                "step": step,
            }

    def snapshot(self):
        with self._lock:
            return self._snapshot_unlocked()

    def _consume_matching_unlocked(self, point, method, path):
        method = method.upper()
        for index, step in enumerate(self._active_request):
            match = step["request_match"]
            if (
                step["point"] == point
                and method == match["method"]
                and self._request_matches_unlocked(match, path)
            ):
                consumed = self._resolve_step_unlocked(
                    self._active_request.pop(index),
                    require_bound=True,
                )
                self._consumed_request.append(
                    {
                        "step_id": consumed["step_id"],
                        "point": consumed["point"],
                        "sequence": consumed["sequence"],
                        "offset_ms": consumed["offset_ms"],
                        "consumed_at_ms": self._now_ms,
                        "request_match": copy.deepcopy(consumed["request_match"]),
                        "action": copy.deepcopy(consumed["action"]),
                    }
                )
                return copy.deepcopy(consumed)
        return None

    def _request_matches_unlocked(self, match, path):
        selector = "path_suffix" if "path_suffix" in match else "path_prefix"
        expected = match[selector]
        if PROVIDER_PAYMENT_PLACEHOLDER not in expected:
            return path.endswith(expected) if selector == "path_suffix" else path.startswith(expected)

        if self._provider_payment_id is None:
            candidate = _provider_id_from_selector(expected, path, selector)
            if candidate is None:
                return False
            self._provider_payment_id = candidate

        resolved = expected.replace(
            PROVIDER_PAYMENT_PLACEHOLDER,
            self._provider_payment_id,
        )
        return path.endswith(resolved) if selector == "path_suffix" else path.startswith(resolved)

    def _resolve_step_unlocked(self, step, require_bound):
        resolved = copy.deepcopy(step)
        match = resolved.get("request_match")
        if match is None:
            return resolved
        for selector in ("path_suffix", "path_prefix"):
            value = match.get(selector)
            if not isinstance(value, str) or PROVIDER_PAYMENT_PLACEHOLDER not in value:
                continue
            if self._provider_payment_id is None:
                if require_bound:
                    raise FaultPlanValidationError(
                        "fault timeline requires the provider payment identity to be bound first"
                    )
                continue
            match[selector] = value.replace(
                PROVIDER_PAYMENT_PLACEHOLDER,
                self._provider_payment_id,
            )
        return resolved

    def _snapshot_unlocked(self):
        return {
            "identity": copy.deepcopy(self._identity),
            "provider_payment_id": self._provider_payment_id,
            "now_ms": self._now_ms,
            "scheduled_request_steps": copy.deepcopy(self._scheduled_request),
            "active_request_steps": copy.deepcopy(self._active_request),
            "consumed_request_steps": copy.deepcopy(self._consumed_request),
            "scheduled_timeline_steps": copy.deepcopy(self._scheduled_timeline),
            "dispatched_timeline_steps": copy.deepcopy(self._dispatched_timeline),
            "deferred_steps": copy.deepcopy(self._deferred),
        }

def _normalize_plan(payload):
    if not isinstance(payload, dict):
        raise FaultPlanValidationError("fault plan must be a JSON object")

    required = {
        "fault_plan_version",
        "scenario_version",
        "seed",
        "logical_business_time",
        "scenario_case",
        "expected_provider_truth",
        "expected_canonical_outcome",
        "steps",
    }
    _exact_keys(payload, required, "fault plan")

    version = _non_negative_int(payload["fault_plan_version"], "fault_plan_version")
    if version != FAULT_PLAN_VERSION:
        raise FaultPlanValidationError(f"unsupported fault_plan_version: {version}")

    scenario_version = _non_negative_int(payload["scenario_version"], "scenario_version")
    if scenario_version == 0:
        raise FaultPlanValidationError("scenario_version must be greater than zero")

    identity = {
        "fault_plan_version": version,
        "scenario_version": scenario_version,
        "seed": _non_negative_int(payload["seed"], "seed"),
        "logical_business_time": _non_blank(payload["logical_business_time"], "logical_business_time"),
        "scenario_case": _non_blank(payload["scenario_case"], "scenario_case"),
        "expected_provider_truth": _non_blank(
            payload["expected_provider_truth"], "expected_provider_truth"
        ),
        "expected_canonical_outcome": _non_blank(
            payload["expected_canonical_outcome"], "expected_canonical_outcome"
        ),
    }

    steps = payload["steps"]
    if not isinstance(steps, list) or not steps:
        raise FaultPlanValidationError("steps must be a non-empty array")

    step_ids = set()
    sequences = set()
    async_defer_ids = set()
    transition_ids = set()
    webhook_attempts = set()
    request_steps = []
    timeline_steps = []
    deferred_steps = []

    for raw_step in steps:
        step = _normalize_step(raw_step)
        if step["step_id"] in step_ids:
            raise FaultPlanValidationError(f"duplicate step_id: {step['step_id']}")
        if step["sequence"] in sequences:
            raise FaultPlanValidationError(f"duplicate step sequence: {step['sequence']}")
        step_ids.add(step["step_id"])
        sequences.add(step["sequence"])

        action = step["action"]
        if action["kind"] == "DEFER_ASYNC":
            operation_id = action["operation_id"]
            if operation_id in async_defer_ids:
                raise FaultPlanValidationError(
                    f"duplicate deferred async operation_id: {operation_id}"
                )
            async_defer_ids.add(operation_id)
        if step["point"] == PROVIDER_TRANSITION:
            operation_id = action["operation_id"]
            if operation_id in transition_ids:
                raise FaultPlanValidationError(
                    f"duplicate provider transition operation_id: {operation_id}"
                )
            transition_ids.add(operation_id)

        if step["point"] == WEBHOOK_DELIVERY:
            webhook_key = (action["event_id"], action["attempt"])
            if webhook_key in webhook_attempts:
                raise FaultPlanValidationError(
                    "duplicate webhook attempt: "
                    f"event_id={action['event_id']} attempt={action['attempt']}"
                )
            webhook_attempts.add(webhook_key)

        if step["point"] in _REQUEST_POINTS:
            request_steps.append(step)
        elif step["point"] in _TIMELINE_POINTS:
            timeline_steps.append(step)
        else:
            deferred_steps.append(copy.deepcopy(step))

    unknown_transitions = transition_ids - async_defer_ids
    if unknown_transitions:
        raise FaultPlanValidationError(
            f"provider transition has no matching DEFER_ASYNC operation: {sorted(unknown_transitions)}"
        )

    request_steps.sort(key=lambda step: (step["offset_ms"], step["sequence"]))
    timeline_steps.sort(key=lambda step: (step["offset_ms"], step["sequence"]))
    deferred_steps.sort(key=lambda step: (step["offset_ms"], step["sequence"]))

    return {
        "identity": identity,
        "request_steps": request_steps,
        "timeline_steps": timeline_steps,
        "deferred_steps": deferred_steps,
    }

def _normalize_step(raw):
    if not isinstance(raw, dict):
        raise FaultPlanValidationError("fault step must be an object")

    base_keys = {"step_id", "sequence", "offset_ms", "point", "action"}
    point = _non_blank(raw.get("point"), "point")
    selector_point = point in _SELECTOR_POINTS
    allowed = base_keys | ({"request_match"} if selector_point else set())
    _exact_keys(raw, allowed, f"fault step {raw.get('step_id', '<unknown>')}")

    request_match = (
        _normalize_request_match(
            raw.get("request_match"),
            allow_prefix=point == STATUS_INQUIRY,
        )
        if selector_point
        else None
    )

    return {
        "step_id": _non_blank(raw["step_id"], "step_id"),
        "sequence": _non_negative_int(raw["sequence"], "sequence"),
        "offset_ms": _non_negative_int(raw["offset_ms"], "offset_ms"),
        "point": point,
        "request_match": request_match,
        "action": _normalize_action(point, raw["action"]),
    }

def _normalize_request_match(raw, allow_prefix=False):
    if not isinstance(raw, dict):
        raise FaultPlanValidationError("request fault step requires request_match")
    # Mutation matching stays suffix-only; STATUS_INQUIRY may use a prefix.
    selectors = {"path_suffix", "path_prefix"} & set(raw)
    if len(selectors) != 1:
        raise FaultPlanValidationError(
            "request_match requires exactly one path_suffix or path_prefix"
        )
    if not allow_prefix and "path_prefix" in selectors:
        raise FaultPlanValidationError(
            "request_match.path_prefix is only allowed for STATUS_INQUIRY"
        )
    _exact_keys(raw, {"method"} | selectors, "request_match")
    method = _non_blank(raw["method"], "request_match.method")
    if not method.isascii() or not method.isalpha() or method != method.upper():
        raise FaultPlanValidationError(
            f"request_match.method must be uppercase ASCII letters: {method!r}"
        )

    path_suffix = raw.get("path_suffix")
    path_prefix = raw.get("path_prefix") if allow_prefix else None
    if (path_suffix is None) == (path_prefix is None):
        label = "path_suffix or path_prefix" if allow_prefix else "path_suffix"
        raise FaultPlanValidationError(
            f"request_match requires exactly one {label}"
        )

    if path_suffix is not None:
        path_suffix = _non_blank(path_suffix, "request_match.path_suffix")
        if not path_suffix.startswith("/"):
            raise FaultPlanValidationError("request_match.path_suffix must begin with /")
        _validate_provider_placeholder(path_suffix, "request_match.path_suffix")
        return {"method": method, "path_suffix": path_suffix}

    path_prefix = _non_blank(path_prefix, "request_match.path_prefix")
    if not path_prefix.startswith("/"):
        raise FaultPlanValidationError("request_match.path_prefix must begin with /")
    _validate_provider_placeholder(path_prefix, "request_match.path_prefix")
    return {"method": method, "path_prefix": path_prefix}


def _validate_provider_placeholder(value, field):
    count = value.count(PROVIDER_PAYMENT_PLACEHOLDER)
    if count > 1:
        raise FaultPlanValidationError(
            f"{field} may contain at most one provider payment placeholder"
        )
    if count == 1:
        index = value.index(PROVIDER_PAYMENT_PLACEHOLDER)
        before = value[index - 1] if index > 0 else ""
        after_index = index + len(PROVIDER_PAYMENT_PLACEHOLDER)
        after = value[after_index:after_index + 1]
        if before != "/" or after not in {"", "/"}:
            raise FaultPlanValidationError(
                f"{field} provider payment placeholder must occupy a path segment"
            )


def _provider_id_from_selector(selector_value, path, selector_kind):
    escaped = re.escape(selector_value)
    token = re.escape(PROVIDER_PAYMENT_PLACEHOLDER)
    pattern = escaped.replace(
        token,
        rf"(?P<provider>{PROVIDER_PAYMENT_ID.pattern[:-2]})",
    )
    match = (
        re.search(pattern + r"\Z", path)
        if selector_kind == "path_suffix"
        else re.match(r"\A" + pattern, path)
    )
    if match is None:
        return None
    candidate = match.group("provider")
    return candidate if PROVIDER_PAYMENT_ID.fullmatch(candidate) is not None else None


def _normalize_action(point, raw):
    if not isinstance(raw, dict):
        raise FaultPlanValidationError("fault action must be an object")
    kind = _non_blank(raw.get("kind"), "action.kind")

    if point == REQUEST_BEFORE:
        if kind == "TRANSPORT_FAILURE":
            _exact_keys(raw, {"kind", "class"}, "TRANSPORT_FAILURE action")
            failure_class = _non_blank(raw["class"], "action.class")
            if failure_class not in _ALLOWED_TRANSPORT:
                raise FaultPlanValidationError(
                    f"unsupported transport failure class: {failure_class}"
                )
            return {"kind": kind, "class": failure_class}

        if kind == "HTTP_FAILURE":
            _exact_keys(raw, {"kind", "status"}, "HTTP_FAILURE action")
            status = _non_negative_int(raw["status"], "action.status")
            if status not in _ALLOWED_TRANSIENT_HTTP:
                raise FaultPlanValidationError(
                    f"unsupported injected HTTP status: {status}"
                )
            return {"kind": kind, "status": status}

        if kind == "PERMANENT_BUSINESS_FAILURE":
            _exact_keys(raw, {"kind", "status", "code"}, "PERMANENT_BUSINESS_FAILURE action")
            status = _non_negative_int(raw["status"], "action.status")
            if status < 400 or status > 499 or status == 429:
                raise FaultPlanValidationError(
                    f"permanent business failure status must be 4xx except 429: {status}"
                )
            return {
                "kind": kind,
                "status": status,
                "code": _non_blank(raw["code"], "action.code"),
            }

        if kind == "DEFER_ASYNC":
            _exact_keys(raw, {"kind", "operation_id"}, "DEFER_ASYNC action")
            return {
                "kind": kind,
                "operation_id": _non_blank(raw["operation_id"], "action.operation_id"),
            }

        raise FaultPlanValidationError(
            f"action {kind} is incompatible with {REQUEST_BEFORE}"
        )

    if point == REQUEST_AFTER:
        _exact_keys(raw, {"kind"}, "REQUEST_AFTER_PROVIDER_COMMIT action")
        if kind != "COMMIT_THEN_DROP_RESPONSE":
            raise FaultPlanValidationError(
                f"action {kind} is incompatible with {REQUEST_AFTER}"
            )
        return {"kind": kind}

    if point == STATUS_INQUIRY:
        _exact_keys(raw, {"kind", "visibility"}, "STATUS_INQUIRY action")
        if kind != "STATUS_VISIBILITY":
            raise FaultPlanValidationError(
                f"action {kind} is incompatible with {STATUS_INQUIRY}"
            )
        visibility = _non_blank(raw["visibility"], "action.visibility")
        if visibility not in _ALLOWED_STATUS_VISIBILITY:
            raise FaultPlanValidationError(
                f"unsupported status visibility: {visibility}"
            )
        return {"kind": kind, "visibility": visibility}

    if point == PROVIDER_TRANSITION:
        _exact_keys(raw, {"kind", "operation_id", "outcome"}, "PROVIDER_TRANSITION action")
        if kind != "PROVIDER_TRANSITION":
            raise FaultPlanValidationError(
                f"action {kind} is incompatible with {PROVIDER_TRANSITION}"
            )
        outcome = _non_blank(raw["outcome"], "action.outcome")
        if outcome not in _ALLOWED_ASYNC_OUTCOME:
            raise FaultPlanValidationError(f"unsupported provider outcome: {outcome}")
        return {
            "kind": kind,
            "operation_id": _non_blank(raw["operation_id"], "action.operation_id"),
            "outcome": outcome,
        }

    if point == WEBHOOK_DELIVERY:
        _exact_keys(raw, {"kind", "delivery", "event_id", "attempt"}, "WEBHOOK action")
        if kind != "WEBHOOK":
            raise FaultPlanValidationError(
                f"action {kind} is incompatible with {WEBHOOK_DELIVERY}"
            )
        delivery = _non_blank(raw["delivery"], "action.delivery")
        if delivery not in _ALLOWED_WEBHOOK_DELIVERY:
            raise FaultPlanValidationError(
                f"unsupported webhook delivery: {delivery}"
            )
        attempt = _non_negative_int(raw["attempt"], "action.attempt")
        if attempt == 0:
            raise FaultPlanValidationError("webhook attempt must be greater than zero")
        return {
            "kind": kind,
            "delivery": delivery,
            "event_id": _non_blank(raw["event_id"], "action.event_id"),
            "attempt": attempt,
        }

    raise FaultPlanValidationError(f"unsupported fault point: {point}")

def _exact_keys(payload, expected, label):
    actual = set(payload)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise FaultPlanValidationError(f"{label} missing fields: {sorted(missing)}")
    if extra:
        raise FaultPlanValidationError(f"{label} has unexpected fields: {sorted(extra)}")


def _non_blank(value, field):
    if not isinstance(value, str) or not value.strip():
        raise FaultPlanValidationError(f"{field} must be a non-blank string")
    return value


def _non_negative_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FaultPlanValidationError(f"{field} must be a non-negative integer")
    return value
