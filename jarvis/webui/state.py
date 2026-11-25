"""Shared server state: the Jarvis instance, event hub and turn runner."""

import json
import re
import threading
import time

from fastapi import HTTPException, Request

from ..artifacts import ArtifactStore
from ..events import Event
from .history import render_history
from .hub import EventHub
from .security import request_token_ok

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def fail(status, message):
    raise HTTPException(status_code=status, detail=message)


def check_id(value, what="id"):
    """Reject anything that could be used as a path (ids become file names)."""
    if not ID_RE.match(value or "") or value in (".", ".."):
        fail(400, f"Invalid {what}")
    return value


class WebState:
    def __init__(self, jarvis, token):
        self.jarvis = jarvis
        self.token = token
        self.hub = EventHub(jarvis.events)
        self.artifacts = ArtifactStore(jarvis.mind)
        self.listener = None
        self._turn_lock = threading.Lock()
        self._running = False
        self._saw_error = False
        self._tts = None
        self._tts_key = None
        self._tts_lock = threading.Lock()
        self.synth_lock = threading.Lock()  # one synthesis at a time
        self.downloads = {}  # item_id -> last progress dict
        self.download_lock = threading.Lock()
        self.active_downloads = set()
        self.pending_approvals = {}  # request_id -> event data, for reconnecting browsers
        self.maintenance_lock = threading.Lock()  # dream cycle / codify
        # Web mode: approvals must become events, never a terminal prompt.
        jarvis.approvals.set_fallback(None)
        jarvis.events.subscribe(self._watch_errors)

    # ---------- events ----------

    def emit(self, kind, **data):
        self.jarvis.events.emit(Event(kind, self.jarvis.session_id, data=data))

    def _watch_errors(self, event):
        if event.kind == "error":
            self._saw_error = True
        elif event.kind == "approval_requested":
            self.pending_approvals[event.data["request_id"]] = {
                **event.data, "requested_at": time.time(), "turn_id": event.turn_id}
        elif event.kind == "approval_finished":
            self.pending_approvals.pop(event.data.get("request_id"), None)

    # ---------- turns ----------

    @property
    def busy(self):
        return self._running or bool(getattr(self.jarvis, "busy", False))

    def start_turn(self, text=None):
        """Run a turn in a worker thread (text=None retries the last request).

        Returns False when a turn is already running.
        """
        if text is None:
            return self.start_action(self.jarvis.retry_last_turn)
        return self.start_action(lambda: self.jarvis.ask(text))

    def start_action(self, action):
        """Run `action()` (a function that performs one turn) in a worker thread.

        Returns False when a turn is already running.
        """
        with self._turn_lock:
            if self.busy:
                return False
            self._running = True
        threading.Thread(target=self._run_turn, args=(action,), daemon=True).start()
        return True

    def _run_turn(self, action):
        self._saw_error = False
        try:
            action()
        except Exception as error:
            if not self._saw_error:
                self.emit("error", message=str(error), status=getattr(error, "status", None),
                          details=getattr(error, "details", "") or "")
        finally:
            self._running = False
            self.emit("busy_changed", busy=self.busy)

    # ---------- onboarding flag (<mind>/ui_state.json) ----------

    @property
    def ui_state_path(self):
        return self.jarvis.mind / "ui_state.json"

    def ui_state(self):
        try:
            data = json.loads(self.ui_state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_ui_state(self, **values):
        data = {**self.ui_state(), **values}
        path = self.ui_state_path
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    @property
    def onboarded(self):
        return bool(self.ui_state().get("onboarded"))

    # ---------- voice (settings, cached TTS engine, downloads) ----------

    def voice_info(self):
        """The GET /api/voice object (also embedded in the snapshot)."""
        cfg = self.jarvis.config.get("voice", {})
        info = {
            "auto_speak": bool(cfg.get("enabled")),
            "engine": cfg.get("engine", "piper"),
            "piper_voice": cfg.get("model", ""),
            "kokoro_voice": cfg.get("kokoro_voice", ""),
            "ready": False, "missing": None, "models": [],
            "downloads": dict(self.downloads),
        }
        try:
            info["ready"], info["missing"] = self.tts_engine().ready()
        except Exception:  # voice module missing or engine failed to build
            pass
        try:
            from ..models_manager import catalog
            info["models"] = catalog(self.jarvis.config)
        except Exception:
            pass
        return info

    def tts_engine(self):
        """The TTS engine for the current settings, built once per (engine, voice)."""
        from ..voice import make_engine
        cfg = self.jarvis.config.get("voice", {})
        key = (cfg.get("engine"), cfg.get("model"), cfg.get("kokoro_voice"))
        with self._tts_lock:
            if self._tts_key != key or self._tts is None:
                self._tts, self._tts_key = make_engine(self.jarvis.config), key
            return self._tts

    def invalidate_tts(self):
        with self._tts_lock:
            self._tts, self._tts_key = None, None

    # ---------- snapshot ----------

    def history_items(self):
        return render_history(self.jarvis.history, self.artifacts.get)

    def character_summary(self):
        jarvis = self.jarvis
        card_id = getattr(jarvis, "character", None)
        if not card_id:
            return None
        store = getattr(jarvis, "store_characters", None)
        if store is not None:
            for summary in store.list():
                if summary.get("id") == card_id:
                    return summary
        return {"id": card_id, "name": card_id}

    def session_meta(self):
        meta = self.jarvis.sessions.get(self.jarvis.session_id) or {}
        return {"id": self.jarvis.session_id, "title": meta.get("title", "New conversation"), **{
            k: v for k, v in meta.items() if k not in ("id", "title")}}

    def status(self):
        jarvis = self.jarvis
        cfg = jarvis.config
        llm = cfg["llm"]
        backend = getattr(jarvis.llm, "backend", "chat")
        result = {
            "provider": llm.get("provider", "custom"),
            "backend": backend,
            "model": llm.get("model", ""),
            "base_url": llm.get("base_url", ""),
            "voice": {"enabled": bool(cfg.get("voice", {}).get("enabled")),
                      "engine": cfg.get("voice", {}).get("engine", "")},  # cheap; full info: voice_info()
            "security_mode": cfg.get("security", {}).get("mode", ""),
            "tool_mode": cfg.get("agent", {}).get("tool_mode", "all"),
            "busy": self.busy,
            "session": self.session_meta(),
            "character": self.character_summary(),
            "chatgpt": None,
        }
        if backend == "chatgpt" or llm.get("provider") == "chatgpt":
            result["chatgpt"] = self.chatgpt_status()
        return result

    def chatgpt_status(self):
        try:
            from ..providers.chatgpt_auth import get_chatgpt_auth
            return get_chatgpt_auth(self.jarvis.config).status()
        except Exception as error:  # module missing or auth backend failure
            return {"signed_in": False, "error": str(error)}

    def snapshot(self):
        return {
            "session": self.session_meta(),
            "history": self.history_items(),
            "busy": self.busy,
            "character": self.character_summary(),
            "status": self.status(),
            "approvals": list(self.pending_approvals.values()),
            "onboarded": self.onboarded,
            "voice": self.voice_info(),
        }


def web(request: Request) -> WebState:
    """FastAPI dependency: authenticate the request and return the state."""
    state = request.app.state.web
    if not request_token_ok(request, state.token):
        fail(401, "Not authenticated")
    return state
