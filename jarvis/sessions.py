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


NOT_PERSISTED = ("provider_state",)  # reasoning and stats are kept so reloads look the same


class SessionStore:
    def __init__(self, mind):
        self.root = Path(mind) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        # Sessions with no messages yet live only in memory, so opening the
        # UI (or a quick --doctor run) doesn't litter mind/sessions/ with empty
        # files. The first append() writes them to disk.
        self._pending = {}

    def create(self, title="New conversation", character="jarvis"):
        session_id = uuid.uuid4().hex[:12]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self._pending[session_id] = {
            "id": session_id,
            "title": title,
            "created": now,
            "updated": now,
            "message_count": 0,
            "character": character,
        }
        return session_id

    def append(self, session_id, message):
        path = self._messages_path(session_id)
        if session_id in self._pending:
            # First message: persist the session for real.
            self._write_meta(session_id, self._pending.pop(session_id))
            path.touch(exist_ok=True)
        if not self._meta_path(session_id).exists():
            # Re-create metadata for this specific id if missing (e.g. resumed after deletion)
            self._write_meta(session_id, {
                "id": session_id,
                "title": "Conversation",
                "created": datetime.datetime.now().isoformat(timespec="seconds"),
                "updated": datetime.datetime.now().isoformat(timespec="milliseconds"),
                "message_count": 0,
            })
            path.touch(exist_ok=True)
        # Opaque provider state (signed blocks, ids) is in-memory only; never persist it.
        message = {k: v for k, v in message.items() if k not in NOT_PERSISTED}
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

    def truncate_tail(self, session_id, n):
        """Drop the last n persisted messages (atomic rewrite). Returns how many
        were dropped. No-op for pending (unsaved) or unknown sessions."""
        path = self._messages_path(session_id)
        if n <= 0 or session_id in self._pending or not path.exists():
            return 0
        lines = path.read_text(encoding="utf-8").splitlines()

        def is_record(line):
            try:
                return isinstance(json.loads(line).get("message"), dict)
            except (json.JSONDecodeError, AttributeError):
                return False

        dropped, keep = 0, list(lines)
        while keep and dropped < n:
            if is_record(keep.pop()):
                dropped += 1
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text("".join(line + "\n" for line in keep), encoding="utf-8")
        temporary.replace(path)
        meta = self.get(session_id)
        if meta:
            meta["message_count"] = sum(1 for line in keep if is_record(line))
            meta["updated"] = datetime.datetime.now().isoformat(timespec="milliseconds")
            self._write_meta(session_id, meta)
        return dropped

    def get(self, session_id):
        if session_id in self._pending:
            return dict(self._pending[session_id])
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
        if session_id in self._pending:
            return True
        return self._meta_path(session_id).exists() and self._messages_path(session_id).exists()

    def delete(self, session_id):
        """Delete a session and its messages. Returns True on success."""
        if self._pending.pop(session_id, None) is not None:
            return True
        if not self.exists(session_id):
            return False
        try:
            self._meta_path(session_id).unlink()
            self._messages_path(session_id).unlink()
            return True
        except OSError:
            return False

    def rename(self, session_id, title):
        """Rename a session title."""
        meta = self.get(session_id)
        if not meta:
            return None
        title = (title or "").strip()[:70] or "Conversation"
        meta["title"] = title
        meta["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        self._write_meta(session_id, meta)
        return meta

    def get_character(self, session_id):
        return (self.get(session_id) or {}).get("character") or ""

    def set_character(self, session_id, character):
        meta = self.get(session_id)
        if not meta:
            return None
        meta["character"] = character
        self._write_meta(session_id, meta)
        return meta

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
        if session_id in self._pending:  # not on disk yet (see __init__)
            self._pending[session_id] = data
            return
        path = self._meta_path(session_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)
