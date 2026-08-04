"""Append-only, redacted JSONL audit trail for runtime actions."""

import datetime
import json
import threading
from pathlib import Path

from .events import new_id
from .redaction import redact_value


class AuditLog:
    def __init__(self, mind):
        self.root = Path(mind) / "audit"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, kind, session_id="", turn_id="", action="", status="", data=None):
        record = {
            "id": new_id("audit"),
            "timestamp": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "kind": kind,
            "session_id": session_id,
            "turn_id": turn_id,
            "action": action,
            "status": status,
            "data": redact_value(data or {}),
        }
        path = self.root / f"{datetime.date.today():%Y-%m-%d}.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record
