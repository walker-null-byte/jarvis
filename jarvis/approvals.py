"""Approval gate shared by tools and interactive frontends."""

import threading
from .events import Event, new_id
from .redaction import redact_value


class ApprovalManager:
    def __init__(self, event_bus, timeout=120):
        self.events = event_bus
        self.timeout = timeout
        self._pending = {}
        self._lock = threading.Lock()
        self._fallback = None
        self._cancel_event = None

    def set_fallback(self, callback):
        """Set a synchronous fallback, primarily for the CLI."""
        self._fallback = callback

    def set_cancel_event(self, cancel_event):
        self._cancel_event = cancel_event

    def request(self, action, payload, session_id="", turn_id=""):
        if self._fallback:
            return bool(self._fallback(action, redact_value(payload)))

        request_id = new_id("approval")
        waiter = threading.Event()
        with self._lock:
            self._pending[request_id] = {"waiter": waiter, "approved": False}
        self.events.emit(Event(
            "approval_requested",
            session_id,
            turn_id=turn_id,
            data={
                "request_id": request_id,
                "action": action,
                "payload": redact_value(payload),
            },
        ))
        waited = 0.0
        while waited < self.timeout and not waiter.wait(0.1):
            waited += 0.1
            if self._cancel_event and self._cancel_event.is_set():
                break
        with self._lock:
            request = self._pending.pop(request_id, None)
        approved = bool(request and request["approved"])
        self.events.emit(Event(
            "approval_finished", session_id, turn_id=turn_id,
            data={"request_id": request_id, "approved": approved},
        ))
        return approved

    def resolve(self, request_id, approved):
        with self._lock:
            request = self._pending.get(request_id)
            if not request:
                return False
            request["approved"] = bool(approved)
            request["waiter"].set()
            return True
