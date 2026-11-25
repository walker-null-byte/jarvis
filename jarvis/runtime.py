"""Many agents in one process: AgentProfile, per-provider LLM overrides, Runtimes.

`Jarvis(profile=...)` gives one agent its own config copy, LLM, tool set and
session kind. `Runtimes` keeps extra Jarvis instances (agent chats, discussion
participants, routine runs) keyed by session id, all on one shared EventBus.
The main chat stays `WebState.jarvis`.
"""

import copy
import threading
from dataclasses import dataclass, field

from .config import load_config, remembered_model
from .events import Event
from .providers import PRESETS


@dataclass
class AgentProfile:
    kind: str = "chat"             # chat | agent | discussion | routine | subagent
    owner: str = ""                # agent plugin id / discussion id / routine id
    title: str = ""                # session title for new sessions
    system_prompt: str = ""        # extra system-prompt section (role, workflow)
    replace_persona: bool = False  # True: system_prompt replaces the character persona
    llm: dict | None = None        # overrides merged over config["llm"]
    core_tools: list = field(default_factory=list)   # extra tool names exposed directly
    extra_tools: dict = field(default_factory=dict)  # name -> callable, visible only to this agent
    hidden_tools: set = field(default_factory=set)   # tool names this agent must not see/use
    max_tool_rounds: int | None = None
    persist: bool = True           # False: no session files (ephemeral subagents)
    allow_spawn: bool = True       # False for subagents (depth limit 1)


def llm_overrides(provider, model="", config=None):
    """The `config["llm"]` override dict for a provider preset.

    Model: the one given, else the one remembered for that provider, else the
    preset's default_model. Unknown providers (custom) only get provider/model.
    """
    preset = PRESETS.get(provider)
    if not preset:
        return {"provider": provider, **({"model": model} if model else {})}
    model = (model or remembered_model(config or load_config(), provider)
             or preset.get("default_model") or "")
    out = {"provider": provider, "base_url": preset["base_url"],
           "api_key_env": preset["api_key_env"] or None, "api_key": None}
    if model:
        out["model"] = model
    return out


def apply_profile_config(config, profile):
    """A deep copy of config with profile.llm merged in (the agent's own settings)."""
    config = copy.deepcopy(config)
    if profile.llm:
        config["llm"].update(profile.llm)
    return config


# ---------- profile factories: rebuild a profile when a session is reloaded ----------

_factories = {}


def register_profile_factory(kind, fn):
    """fn(owner, session_meta) -> AgentProfile, used by Runtimes.get for sessions of `kind`."""
    _factories[kind] = fn


def profile_for(meta):
    """The profile for a stored session (a plain profile when no factory is registered)."""
    kind, owner = meta.get("kind") or "chat", meta.get("owner") or ""
    factory = _factories.get(kind)
    if factory:
        return factory(owner, meta)
    return AgentProfile(kind=kind, owner=owner)


class Runtimes:
    """Extra Jarvis instances keyed by session_id, sharing one EventBus."""

    def __init__(self, config, events, jarvis_cls=None):
        self.config = config
        self.events = events
        self._jarvis_cls = jarvis_cls
        self._instances = {}
        self._running = set()
        self._lock = threading.RLock()
        self._store = None

    def _cls(self):
        if self._jarvis_cls:
            return self._jarvis_cls
        from .agent import Jarvis
        return Jarvis

    def _sessions(self):
        if self._store is None:
            from .config import mind_dir
            from .sessions import SessionStore
            self._store = SessionStore(mind_dir(self.config))
        return self._store

    def create(self, profile):
        """Start a new instance (and session) for `profile`; returns the Jarvis."""
        jarvis = self._cls()(self.config, None, self.events, profile)
        with self._lock:
            self._instances[jarvis.session_id] = jarvis
        return jarvis

    def get(self, session_id):
        """The instance for a session, loaded lazily from disk; None if unknown."""
        with self._lock:
            jarvis = self._instances.get(session_id)
            if jarvis:
                return jarvis
            meta = self._sessions().get(session_id)
            if not meta:
                return None
            jarvis = self._cls()(self.config, session_id, self.events, profile_for(meta))
            self._instances[session_id] = jarvis
            return jarvis

    def ids(self):
        with self._lock:
            return list(self._instances)

    def busy(self, session_id):
        with self._lock:
            jarvis = self._instances.get(session_id)
            return session_id in self._running or bool(jarvis and jarvis.busy)

    def start_turn(self, session_id, text=None):
        """Run a turn in a worker thread (text=None retries the last request).

        Returns False when that instance is already busy; raises KeyError for an
        unknown session.
        """
        jarvis = self.get(session_id)
        if jarvis is None:
            raise KeyError(session_id)
        with self._lock:
            if self.busy(session_id):
                return False
            self._running.add(session_id)
        action = jarvis.retry_last_turn if text is None else (lambda: jarvis.ask(text))
        threading.Thread(target=self._run, args=(jarvis, action), daemon=True).start()
        return True

    def _run(self, jarvis, action):
        saw_error = []
        watcher = self.events.subscribe(
            lambda e: saw_error.append(1) if e.kind == "error" and e.session_id == jarvis.session_id else None)
        try:
            action()
        except Exception as error:
            if not saw_error:
                self.events.emit(Event("error", jarvis.session_id, data={
                    "message": str(error), "status": getattr(error, "status", None),
                    "details": getattr(error, "details", "") or ""}))
        finally:
            self.events.unsubscribe(watcher)
            with self._lock:
                self._running.discard(jarvis.session_id)
            self.events.emit(Event("busy_changed", jarvis.session_id, data={"busy": False}))

    def cancel(self, session_id):
        with self._lock:
            jarvis = self._instances.get(session_id)
        if jarvis:
            jarvis.cancel()
        return bool(jarvis)

    def find_approval(self, request_id):
        """The instance holding a pending approval request, or None."""
        with self._lock:
            instances = list(self._instances.values())
        for jarvis in instances:
            with jarvis.approvals._lock:
                if request_id in jarvis.approvals._pending:
                    return jarvis
        return None

    def close(self, session_id=None):
        """Cancel and drop one instance (or all of them)."""
        with self._lock:
            ids = [session_id] if session_id else list(self._instances)
            gone = [self._instances.pop(i, None) for i in ids]
        for jarvis in filter(None, gone):
            jarvis.cancel()
            jarvis.close()
