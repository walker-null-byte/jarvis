"""Durable, lightweight session storage.

Each session has a small metadata file and a JSONL message stream.  JSONL keeps
the format append-friendly and easy to inspect or export while avoiding a
database dependency for the prototype.
"""

import datetime
import json
import uuid
from pathlib import Path

from .redaction import redact_value


class SessionStore:
    def __init__(self, mind):
        self.root = Path(mind) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, title="New conversation"):
        session_id = uuid.uuid4().hex[:12]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self._write_meta(session_id, {
            "id": session_id,
            "title": title,
            "created": now,
            "updated": now,
            "message_count": 0,
        })
        self._messages_path(session_id).touch()
        return session_id

    def append(self, session_id, message):
        path = self._messages_path(session_id)
        if not path.parent.exists():
            self.create(session_id)
        safe_message = redact_value(message)
        record = {
            "timestamp": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "message": safe_message,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        meta = self.get(session_id) or {"id": session_id, "title": "Conversation"}
        meta["updated"] = record["timestamp"]
        meta["message_count"] = int(meta.get("message_count", 0)) + 1
        if safe_message.get("role") == "user" and meta.get("title") in ("", "New conversation"):
            title = (safe_message.get("content") or "Conversation").strip().replace("\n", " ")
            meta["title"] = title[:70] or "Conversation"
        self._write_meta(session_id, meta)

    def load(self, session_id):
        path = self._messages_path(session_id)
        if not path.exists():
            return []
        messages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                if isinstance(record.get("message"), dict):
                    messages.append(record["message"])
            except json.JSONDecodeError:
                continue
        return messages

    def get(self, session_id):
        path = self._meta_path(session_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def list(self):
        sessions = []
        for path in self.root.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if item.get("id"):
                sessions.append(item)
        sessions.sort(key=lambda item: item.get("updated", ""), reverse=True)
        return sessions

    def search(self, query="", limit=20):
        """Return recent sessions whose metadata or messages match ``query``."""
        query = (query or "").strip().casefold()
        matches = []
        for item in self.list():
            if not query:
                matches.append(item)
            elif self._matches(item["id"], query):
                matches.append(item)
            if limit and len(matches) >= limit:
                break
        return matches

    def export(self, session_id, destination=None, format="markdown"):
        """Serialize a session as Markdown or JSON, optionally writing it out."""
        metadata = self.get(session_id)
        if not metadata:
            raise ValueError(f"Session not found: {session_id}")
        messages = self.load(session_id)
        format = format.casefold()
        if format == "json":
            content = json.dumps(
                {"session": metadata, "messages": messages},
                ensure_ascii=False,
                indent=2,
                default=str,
            ) + "\n"
        elif format in ("markdown", "md"):
            content = self._as_markdown(metadata, messages)
        else:
            raise ValueError("Session export format must be 'markdown' or 'json'")

        if destination is not None:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return str(path)
        return content

    def exists(self, session_id):
        return self._meta_path(session_id).exists() and self._messages_path(session_id).exists()

    def _meta_path(self, session_id):
        return self.root / f"{session_id}.json"

    def _messages_path(self, session_id):
        return self.root / f"{session_id}.jsonl"

    def _matches(self, session_id, query):
        metadata = self.get(session_id) or {}
        haystack = json.dumps(metadata, ensure_ascii=False, default=str).casefold()
        if query in haystack:
            return True
        return query in json.dumps(self.load(session_id), ensure_ascii=False, default=str).casefold()

    @staticmethod
    def _as_markdown(metadata, messages):
        lines = [
            f"# {metadata.get('title') or 'Conversation'}",
            "",
            f"- Session: `{metadata.get('id', '')}`",
            f"- Created: {metadata.get('created', '')}",
            f"- Updated: {metadata.get('updated', '')}",
            "",
        ]
        labels = {"user": "You", "assistant": "Jarvis", "tool": "Tool", "system": "System"}
        for message in messages:
            role = message.get("role", "message")
            content = message.get("content") or ""
            lines.extend([f"## {labels.get(role, role.title())}", "", str(content), ""])
        return "\n".join(lines)

    def _write_meta(self, session_id, data):
        path = self._meta_path(session_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)
