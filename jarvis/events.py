"""Small in-process event bus used by the agent and its frontends.

Events are deliberately plain data.  The Textual UI, a future web server, and
diagnostic integrations can subscribe without reaching into the agent loop.
"""

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Callable
import uuid


@dataclass(frozen=True)
class Event:
    kind: str
    session_id: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))
    turn_id: str = ""
    data: dict = field(default_factory=dict)


class EventBus:
    """Thread-safe in-process pub/sub for runtime events."""

    def __init__(self):
        self._subscribers = []
        self._lock = Lock()

    def subscribe(self, callback: Callable[[Event], None]):
        with self._lock:
            self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def emit(self, event: Event):
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                # A presentation/plugin failure must never break the agent.
                continue


def new_id(prefix="id"):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
