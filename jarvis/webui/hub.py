"""Fan-out of agent events to SSE clients.

The agent emits events from worker threads. Each connected browser has an
asyncio queue; events are handed over with call_soon_threadsafe and the oldest
are dropped when a client falls behind, so the agent is never blocked.
"""

import asyncio
import json
import threading

MAX_QUEUE = 2000


def event_message(event):
    """Plain-dict form of an Event, as sent to the browser."""
    return {
        "kind": event.kind,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "timestamp": event.timestamp,
        "data": event.data,
    }


def sse_format(message):
    return "data: " + json.dumps(message, ensure_ascii=False, default=str) + "\n\n"


class Client:
    """One connected browser tab."""

    def __init__(self, loop):
        self.loop = loop
        self.queue = asyncio.Queue()

    def push_threadsafe(self, message):
        try:
            self.loop.call_soon_threadsafe(self._push, message)
        except RuntimeError:
            pass  # event loop already closed

    def _push(self, message):
        if self.queue.qsize() >= MAX_QUEUE:
            self.queue.get_nowait()  # drop the oldest
        self.queue.put_nowait(message)


class EventHub:
    def __init__(self, bus):
        self._clients = set()
        self._lock = threading.Lock()
        self._bus = bus
        bus.subscribe(self._on_event)

    def connect(self):
        client = Client(asyncio.get_running_loop())
        with self._lock:
            self._clients.add(client)
        return client

    def disconnect(self, client):
        with self._lock:
            self._clients.discard(client)

    def close(self):
        self._bus.unsubscribe(self._on_event)

    def _on_event(self, event):
        message = event_message(event)
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.push_threadsafe(message)
