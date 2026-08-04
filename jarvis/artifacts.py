"""Persistent visual/text artifacts produced during a Jarvis session."""

import datetime
import json
from pathlib import Path

from .events import new_id
from .surfaces import SurfaceManager


ARTIFACT_TYPES = {"markdown", "text", "image", "table", "chart", "json"}


class ArtifactStore:
    def __init__(self, mind):
        self.root = Path(mind) / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.surfaces = SurfaceManager(self.root)

    def create(self, artifact_type, title, content="", path="", data=None,
               display_mode="inline", session_id=""):
        if artifact_type not in ARTIFACT_TYPES:
            return {"error": f"Unsupported artifact type: {artifact_type}"}
        artifact_id = new_id("artifact")
        now = datetime.datetime.now().isoformat(timespec="seconds")
        item = {
            "id": artifact_id,
            "type": artifact_type,
            "title": (title or "Jarvis artifact")[:160],
            "content": content[:20000] if isinstance(content, str) else "",
            "path": path[:2000] if isinstance(path, str) else "",
            "data": data,
            "display_mode": display_mode if display_mode in ("inline", "window") else "inline",
            "session_id": session_id,
            "created": now,
            "updated": now,
            "status": "open",
        }
        self._write(item)
        result = {"artifact_id": artifact_id, "artifact": item}
        if item["display_mode"] == "window":
            result.update(self.surfaces.open(artifact_id))
        return result

    def update(self, artifact_id, content=None, path=None, data=None, title=None):
        item = self.get(artifact_id)
        if not item:
            return {"error": f"Artifact not found: {artifact_id}"}
        if content is not None:
            item["content"] = content[:20000]
        if path is not None:
            item["path"] = path[:2000]
        if data is not None:
            item["data"] = data
        if title is not None:
            item["title"] = title[:160]
        item["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        self._write(item)
        return {"artifact_id": artifact_id, "artifact": item}

    def close(self, artifact_id):
        item = self.get(artifact_id)
        if not item:
            return {"error": f"Artifact not found: {artifact_id}"}
        item["status"] = "closed"
        item["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        self._write(item)
        result = self.surfaces.close(artifact_id)
        return {"artifact_id": artifact_id, "status": "closed", "artifact": item, **result}

    def get(self, artifact_id):
        path = self.root / f"{artifact_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def list(self, session_id=""):
        items = []
        for path in self.root.glob("artifact_*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not session_id or item.get("session_id") == session_id:
                items.append(item)
        items.sort(key=lambda item: item.get("updated", ""), reverse=True)
        return {"artifacts": items}

    def _write(self, item):
        path = self.root / f"{item['id']}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(item, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        temporary.replace(path)

    def open_window(self, artifact_id):
        return self.surfaces.open(artifact_id)


def build_artifact_tools(mind):
    store = ArtifactStore(mind)

    def show_artifact(artifact_type: str, title: str, content: str = "", path: str = "",
                      data: str = "", display_mode: str = "inline", session_id: str = ""):
        """Create an image, chart, table, Markdown, text, or JSON artifact for the UI.

        `data` may contain a JSON string for chart/table/json artifacts. The UI
        displays artifacts inline and can later open them in a detached window.
        """
        parsed = None
        if data:
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = data
        return store.create(artifact_type, title, content, path, parsed, display_mode, session_id)

    def update_artifact(artifact_id: str, content: str = "", path: str = "", data: str = "", title: str = ""):
        """Update an existing artifact."""
        parsed = None
        if data:
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = data
        return store.update(
            artifact_id,
            content=content if content else None,
            path=path if path else None,
            data=parsed,
            title=title if title else None,
        )

    def list_artifacts(session_id: str = ""):
        """List artifacts, optionally limited to a session."""
        return store.list(session_id)

    def close_artifact(artifact_id: str):
        """Mark an artifact closed in the UI."""
        return store.close(artifact_id)

    def open_artifact_window(artifact_id: str):
        """Open an existing artifact in a detached native Python window."""
        return store.open_window(artifact_id)

    return [show_artifact, update_artifact, list_artifacts, close_artifact, open_artifact_window]
