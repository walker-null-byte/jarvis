"""AgentManager: installed agent plugins, their settings, records, runs and imports.

Folders:
    jarvis/agents_builtin/<id>/     built-in plugins (read-only)
    <mind>/agents/<id>/             installed plugins
    <mind>/agents/.staging/<token>/ imports waiting for review
    <mind>/agent_data/<id>/         the user's data: settings.json, records/, runs.jsonl, trust.json
Secrets live in the keystore as "agent:<id>:<KEY>" and are never exported.
"""

import datetime
import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import keystore
from ..events import Event
from ..runtime import AgentProfile, llm_overrides
from . import packaging
from .manifest import ID_RE, ManifestError, format_issue, load_manifest
from .plugin_tools import (RECORD_TOOL_NAMES, build_record_tools, load_plugin_tools, skill_tool,
                           table_summary)
from .records import FieldError, RecordTable, atomic_write_json, coerce
from .templating import TemplateError, render_prompt, render_template, to_csv, to_json, to_markdown, today

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "agents_builtin"
STAGING_MAX_AGE = 24 * 3600

# Tools hidden by the manifest's [permissions] (names of current and planned Jarvis tools).
COMMAND_TOOLS = {"run_command", "command_output", "command_kill"}
WRITE_TOOLS = {"write_file", "edit_file"}
FILE_TOOLS = WRITE_TOOLS | {"read_file", "list_dir", "grep", "glob"}
NETWORK_TOOLS = {"web_search", "fetch_page"}


class AgentError(ValueError):
    """A request about agents that cannot be done. `status` is the HTTP status to use."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


@dataclass
class Plugin:
    id: str
    path: Path
    builtin: bool
    manifest: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    py_files: list = field(default_factory=list)
    trusted: bool = False

    @property
    def needs_trust(self):
        return bool(self.py_files) and not self.trusted


def code_hash(folder):
    """sha256 of every .py file (path + content): trust is tied to exactly this code."""
    digest = hashlib.sha256()
    for path in sorted(Path(folder).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _py_files(folder):
    return sorted(p.relative_to(folder).as_posix() for p in Path(folder).rglob("*.py")
                  if "__pycache__" not in p.parts)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _toml_string(text):
    return json.dumps(str(text), ensure_ascii=False)  # a JSON string is a valid TOML basic string


def rewrite_identity(toml_text, new_id, new_name):
    """Change the top-level id and name lines of an agent.toml text."""
    head, sep, rest = toml_text.partition("\n[")
    for key, value in (("id", new_id), ("name", new_name)):
        pattern = re.compile(rf'^(\s*{key}\s*=\s*)("(?:[^"\\]|\\.)*"|\'[^\']*\')', re.M)
        if pattern.search(head):
            head = pattern.sub(lambda m, v=value: m.group(1) + _toml_string(v), head, count=1)
        else:
            head = head.rstrip("\n") + f"\n{key} = {_toml_string(value)}\n"
    return head + sep + rest


class AgentManager:
    def __init__(self, state, builtin_dir=None):
        self.state = state
        self.config = state.jarvis.config
        self.events = state.jarvis.events
        self.mind = Path(state.jarvis.mind)
        self.builtin_dir = Path(builtin_dir or BUILTIN_DIR)
        self.agents_dir = self.mind / "agents"
        self.data_root = self.mind / "agent_data"
        self.staging_dir = self.agents_dir / ".staging"
        self._lock = threading.RLock()
        self._cache = {}        # id -> (stamp, Plugin)
        self._broken = {}       # folder name -> {path, issues}
        self._tool_cache = {}   # id -> (code hash, tools, errors)
        from ..runtime import register_profile_factory
        register_profile_factory("agent", lambda owner, meta: self.profile(owner, meta))
        try:  # spawn_agent(agent="<plugin id>") delegates to installed agents
            from ..subagents import set_agent_manager
            set_agent_manager(self)
        except ImportError:
            pass

    # ---------- discovery ----------

    def _folders(self):
        """(folder, builtin) for every plugin folder; installed ones override nothing (ids are unique)."""
        out = []
        for root, builtin in ((self.builtin_dir, True), (self.agents_dir, False)):
            if root.is_dir():
                out += [(p, builtin) for p in sorted(root.iterdir())
                        if p.is_dir() and not p.name.startswith(".") and (p / "agent.toml").is_file()]
        return out

    def _load(self, folder, builtin):
        stamp = tuple((p.stat().st_mtime_ns if p.exists() else 0)
                      for p in (folder / "agent.toml", folder / "tools", folder))
        cached = self._cache.get(str(folder))
        if cached and cached[0] == stamp:
            plugin = cached[1]
        else:
            manifest, warnings = load_manifest(folder, check_version=not builtin)
            plugin = Plugin(manifest["id"], folder, builtin, manifest, warnings, _py_files(folder))
            self._cache[str(folder)] = (stamp, plugin)
        plugin.trusted = builtin or not plugin.py_files or self._trusted_hash(plugin.id) == code_hash(folder)
        return plugin

    def plugins(self):
        """All loadable plugins by id (broken folders are kept in self._broken)."""
        with self._lock:
            found, broken = {}, {}
            for folder, builtin in self._folders():
                try:
                    plugin = self._load(folder, builtin)
                except ManifestError as error:
                    broken[folder.name] = {"folder": folder.name, "builtin": builtin, "issues": error.issues,
                                           "message": str(error)}
                    continue
                if plugin.id in found:
                    broken[folder.name] = {"folder": folder.name, "builtin": builtin, "issues": [], "message":
                                           f"agent.toml: id '{plugin.id}' is already used by another agent"}
                    continue
                if not builtin and folder.name != plugin.id:
                    broken[folder.name] = {"folder": folder.name, "builtin": False, "issues": [], "message":
                                           f"folder name '{folder.name}' must match id '{plugin.id}'"}
                    continue
                found[plugin.id] = plugin
            self._broken = broken
            return found

    def get(self, agent_id):
        plugin = self.plugins().get(agent_id)
        if plugin is None:
            raise AgentError(f"No agent called '{agent_id}'", 404)
        return plugin

    def list(self):
        plugins = self.plugins()
        items = [self.summary(p) for p in sorted(plugins.values(), key=lambda p: (not p.builtin, p.manifest["name"].lower()))]
        return items

    def broken(self):
        self.plugins()
        return list(self._broken.values())

    def summary(self, plugin):
        m = plugin.manifest
        counts = {t["name"]: self.table(plugin.id, t["name"], plugin).count() for t in m["records"]}
        return {
            "id": plugin.id, "name": m["name"], "version": m["version"], "description": m["description"],
            "author": m["author"], "icon": m["icon"], "has_icon_file": (plugin.path / "icon.svg").is_file(),
            "builtin": plugin.builtin, "trusted": plugin.trusted, "needs_trust": plugin.needs_trust,
            "records": [{"name": t["name"], "label": t["label"], "count": counts[t["name"]]} for t in m["records"]],
            "actions": len(m["actions"]), "missing_settings": self.missing_settings(plugin),
            "last_run": self.last_run(plugin.id),
        }

    def detail(self, agent_id):
        """Everything the workspace needs (no secret values)."""
        plugin = self.get(agent_id)
        m = plugin.manifest
        tools, errors = self.plugin_tools(plugin)
        readme = plugin.path / "README.md"
        return {
            **self.summary(plugin), "manifest": m,
            "warnings": [format_issue(w) for w in plugin.warnings],
            "readme": readme.read_text(encoding="utf-8") if readme.is_file() else "",
            "py_files": plugin.py_files, "tools": sorted(tools), "tool_errors": errors,
            "skills": sorted(p.stem for p in (plugin.path / "skills").glob("*.md")) if (plugin.path / "skills").is_dir() else [],
            "location": str(plugin.path),
        }

    # ---------- data ----------

    def data_dir(self, agent_id):
        return self.data_root / agent_id

    def table(self, agent_id, name, plugin=None):
        plugin = plugin or self.get(agent_id)
        spec = next((t for t in plugin.manifest["records"] if t["name"] == name), None)
        if spec is None:
            raise AgentError(f"{plugin.manifest['name']} has no records table '{name}'", 404)
        return RecordTable(self.data_dir(agent_id), spec, on_change=lambda table, change: self._records_changed(agent_id, table, change))

    def _records_changed(self, agent_id, table, change):
        from .. import toolctx
        ctx = toolctx.current()
        session_id = ctx.session_id if ctx else f"agent:{agent_id}"
        self.events.emit(Event("agent_records_changed", session_id, data={
            "agent_id": agent_id, "table": table, "change": change}))

    def log(self, agent_id, message):
        path = self.data_dir(agent_id) / "plugin.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_now()} {message}\n")

    def last_run(self, agent_id):
        path = self.data_dir(agent_id) / "runs.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            return json.loads(lines[-1]) if lines else None
        except (OSError, ValueError, IndexError):
            return None

    def _log_run(self, agent_id, entry):
        path = self.data_dir(agent_id) / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---------- settings ----------

    def _settings_file(self, agent_id):
        return self.data_dir(agent_id) / "settings.json"

    def _stored(self, agent_id):
        try:
            data = json.loads(self._settings_file(agent_id).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def secret_name(agent_id, key):
        return f"agent:{agent_id}:{key}"

    def settings_values(self, agent_id, secrets=False, plugin=None):
        """Current values with defaults; secrets resolved only when asked (plugin tools)."""
        plugin = plugin or self.get(agent_id)
        stored = self._stored(agent_id).get("values", {})
        values = {}
        for s in plugin.manifest["settings"]:
            if s["type"] == "secret":
                if secrets:
                    values[s["key"]] = keystore.get(self.secret_name(agent_id, s["key"])) or ""
                continue
            value = stored.get(s["key"], s["default"])
            values[s["key"]] = value if value is not None else ""
        return values

    def missing_settings(self, plugin):
        values = self.settings_values(plugin.id, plugin=plugin)
        missing = []
        for s in plugin.manifest["settings"]:
            if not s["required"]:
                continue
            present = (keystore.get(self.secret_name(plugin.id, s["key"])) if s["type"] == "secret"
                       else values.get(s["key"]) not in (None, "", []))
            if not present:
                missing.append(s["key"])
        return missing

    def settings_info(self, agent_id):
        plugin = self.get(agent_id)
        secrets = {}
        for s in plugin.manifest["settings"]:
            if s["type"] == "secret":
                name = self.secret_name(agent_id, s["key"])
                secrets[s["key"]] = {"saved": bool(keystore.get(name)), "hint": keystore.hint(name)}
        llm = self._stored(agent_id).get("llm") or {}
        return {"fields": plugin.manifest["settings"], "values": self.settings_values(agent_id),
                "secrets": secrets, "missing": self.missing_settings(plugin),
                "llm": {"provider": llm.get("provider", ""), "model": llm.get("model", "")},
                "manifest_llm": {"provider": plugin.manifest["agent"]["provider"],
                                 "model": plugin.manifest["agent"]["model"]}}

    def save_settings(self, agent_id, values=None, secrets=None, llm=None):
        """Validate and store settings. secrets: {KEY: value} saves, {KEY: None or ""} forgets."""
        plugin = self.get(agent_id)
        fields = {s["key"]: s for s in plugin.manifest["settings"]}
        clean, errors = {}, {}
        for key, value in (values or {}).items():
            spec = fields.get(key)
            if spec is None or spec["type"] == "secret":
                errors[key] = "not a setting of this agent" if spec is None else "send secrets separately"
                continue
            try:
                clean[key] = _coerce_input(spec, value)
            except FieldError as error:
                errors[key] = str(error)
        for key in (secrets or {}):
            if key not in fields or fields[key]["type"] != "secret":
                errors[key] = "not a secret setting of this agent"
        if errors:
            raise AgentError("Some settings are not valid", 400, fields=errors)
        stored = self._stored(agent_id)
        stored["values"] = {**stored.get("values", {}), **{k: v for k, v in clean.items()}}
        if llm is not None:
            provider, model = str(llm.get("provider") or "").strip(), str(llm.get("model") or "").strip()
            stored["llm"] = {"provider": provider, "model": model} if (provider or model) else {}
        atomic_write_json(self._settings_file(agent_id), stored)
        for key, value in (secrets or {}).items():
            name = self.secret_name(agent_id, key)
            if value:
                keystore.set(name, str(value))
            else:
                keystore.delete(name)
        self.refresh_sessions(agent_id)
        return self.settings_info(agent_id)

    # ---------- tools & profile ----------

    def plugin_tools(self, plugin):
        """Trusted plugin tools (cached per code hash) and load errors."""
        if not plugin.py_files:
            return {}, []
        if not plugin.trusted:
            return {}, []
        digest = code_hash(plugin.path)
        with self._lock:
            cached = self._tool_cache.get(plugin.id)
            if cached and cached[0] == digest:
                return cached[1], cached[2]
            tools, errors = load_plugin_tools(self, plugin)
            self._tool_cache[plugin.id] = (digest, tools, errors)
            return tools, errors

    def call_jarvis_tool(self, name, arguments):
        """For plugin code: run a Jarvis tool (same policy/approvals as the agent's own calls)."""
        registry = self.state.jarvis.registry
        if name not in registry.tool_names():
            raise RuntimeError(f"Jarvis has no tool named '{name}'")
        return registry.call(name, arguments)

    def hidden_tools(self, plugin):
        perms = plugin.manifest["permissions"]
        hidden = set()
        if not perms["commands"]:
            hidden |= COMMAND_TOOLS
        if perms["files"] == "none":
            hidden |= FILE_TOOLS
        elif perms["files"] == "data":
            hidden |= WRITE_TOOLS
        if not perms["network"]:
            hidden |= NETWORK_TOOLS
        return hidden

    def llm_for(self, plugin):
        """The provider/model override for this agent, or None to follow the main chat."""
        user = self._stored(plugin.id).get("llm") or {}
        provider = user.get("provider") or plugin.manifest["agent"]["provider"]
        model = user.get("model") or ("" if user.get("provider") else plugin.manifest["agent"]["model"])
        if provider:
            return llm_overrides(provider, model, self.config)
        return {"model": model} if model else None

    def system_prompt(self, plugin, tool_names=()):
        m = plugin.manifest
        prompt = (plugin.path / m["agent"]["prompt"]).read_text(encoding="utf-8").strip()
        lines = [f"# You are {m['name']}", "", prompt, "", "# Settings (set by the user in the Settings panel)"]
        values = self.settings_values(plugin.id, plugin=plugin)
        missing = self.missing_settings(plugin)
        for s in m["settings"]:
            if s["type"] == "secret":
                saved = bool(keystore.get(self.secret_name(plugin.id, s["key"])))
                lines.append(f"- {s['key']}: {'saved (your tools use it automatically)' if saved else 'not set'}")
                continue
            value = values.get(s["key"])
            shown = ", ".join(map(str, value)) if isinstance(value, list) else str(value if value is not None else "")
            lines.append(f"- {s['key']} ({s['label']}): {shown.strip() or '(not set)'}")
        if not m["settings"]:
            lines.append("- (this agent has no settings)")
        if missing:
            lines.append(f"Required but not set yet: {', '.join(missing)}. Ask the user, or tell them to open Settings.")
        if m["records"]:
            lines += ["", "# Your workspace records",
                      "The user sees these tables live in your workspace. Tables:"]
            lines += [f"- {table_summary(t)}" for t in m["records"]]
            lines += [
                "Rules for records:",
                "- Save results with records_add as soon as you have them (a few at a time), so progress is never lost.",
                "- Check what already exists with records_query before adding, and use records_update to change a record.",
                "- Never invent values. Leave a field out when you don't know it.",
                "- Fields marked user-editable belong to the user: only change them when asked or when your instructions say so.",
            ]
        own = [n for n in tool_names if n not in RECORD_TOOL_NAMES]
        if own:
            lines += ["", f"# Your own tools\n{', '.join(sorted(own))} (call them directly)."]
        lines += ["", f"Today is {today()}."]
        return "\n".join(lines)

    def profile(self, agent_id, session_meta=None):
        """The AgentProfile for this agent's chats (also the Runtimes profile factory)."""
        try:
            plugin = self.get(agent_id)
        except AgentError:
            return AgentProfile(kind="agent", owner=agent_id, title="", system_prompt=(
                f"(The agent '{agent_id}' is no longer installed. Tell the user.)"))
        m = plugin.manifest
        extra = build_record_tools(self, plugin)
        plugin_tools, _ = self.plugin_tools(plugin)
        extra.update(plugin_tools)
        skills = skill_tool(plugin)
        if skills:
            extra["agent_skill"] = skills
        return AgentProfile(
            kind="agent", owner=plugin.id, title="",  # chats are titled by their first message
            system_prompt=self.system_prompt(plugin, list(extra)),
            replace_persona=m["agent"]["replace_persona"],
            llm=self.llm_for(plugin), core_tools=list(m["agent"]["core_tools"]),
            extra_tools=extra, hidden_tools=self.hidden_tools(plugin),
            max_tool_rounds=m["agent"]["max_tool_rounds"])

    def refresh_sessions(self, agent_id):
        """Drop idle runtime instances of this agent so the next turn rebuilds its profile."""
        runtimes = self.state.runtimes
        for session_id in runtimes.ids():
            jarvis = runtimes.get(session_id)
            profile = getattr(jarvis, "profile", None)
            if profile and profile.kind == "agent" and profile.owner == agent_id and not runtimes.busy(session_id):
                runtimes.close(session_id)

    # ---------- actions & chat ----------

    def _action(self, plugin, action_id):
        action = next((a for a in plugin.manifest["actions"] if a["id"] == action_id), None)
        if action is None:
            raise AgentError(f"{plugin.manifest['name']} has no action '{action_id}'", 404)
        return action

    def validate_inputs(self, plugin, action, inputs):
        inputs = inputs or {}
        if not isinstance(inputs, dict):
            raise AgentError("inputs must be an object", 400)
        clean, errors = {}, {}
        for spec in action["inputs"]:
            key = spec["key"]
            value = inputs.get(key)
            try:
                value = _coerce_input(spec, value)
            except FieldError as error:
                errors[key] = str(error)
                continue
            if value in (None, "", []):
                if spec["required"]:
                    errors[key] = "is required"
                    continue
                value = spec["default"] if spec["default"] is not None else ""
            clean[key] = value
        if errors:
            labels = {s["key"]: s["label"] for s in action["inputs"]}
            text = "; ".join(f"{labels.get(k, k)} {v}" for k, v in errors.items())
            raise AgentError(f"Check the inputs: {text}", 400, fields=errors)
        return clean

    def render_action(self, agent_id, action_id, inputs):
        """The validated, templated prompt for an action (what the agent is asked to do)."""
        plugin = self.get(agent_id)
        action = self._action(plugin, action_id)
        clean = self.validate_inputs(plugin, action, inputs)
        shown = {k: (v if v not in ("", None, []) else "(not given)") for k, v in clean.items()}
        counts = {t["name"]: self.table(agent_id, t["name"]).count() for t in plugin.manifest["records"]}
        return render_prompt(action["prompt"], shown, self.settings_values(agent_id), counts).strip()

    def action_message(self, plugin, action, inputs, prompt):
        """The chat message for an action run: a short header, then the full instructions."""
        labels = {s["key"]: re.sub(r"\s*\([^)]*\)\s*$", "", s["label"]).rstrip("?: ") for s in action["inputs"]}
        parts = []
        for key, value in inputs.items():
            if value in ("", None, []):
                continue
            text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            parts.append(f"{labels.get(key, key)}: {text}")
        header = f"⚡ {action['label']}" + (f"\n{' · '.join(parts)}" if parts else "")
        return f"{header}\n\n{prompt}"

    def _session(self, plugin, session_id, runtimes, title=""):
        """An existing chat of this agent, or a new one."""
        if session_id:
            jarvis = runtimes.get(session_id)
            if jarvis is None:
                raise AgentError("That chat no longer exists", 404)
            profile = getattr(jarvis, "profile", None)
            if not profile or profile.kind != "agent" or profile.owner != plugin.id:
                raise AgentError("That chat belongs to something else", 400)
            return session_id
        profile = self.profile(plugin.id)
        if title:
            profile.title = title[:70]
        return runtimes.create(profile).session_id

    def run_action(self, agent_id, action_id, inputs, session_id=None, runtimes=None):
        """Post an action into an agent chat (a new one unless session_id is given). Returns the session id."""
        plugin = self.get(agent_id)
        action = self._action(plugin, action_id)
        missing = self.missing_settings(plugin)
        if missing:
            labels = {s["key"]: s["label"] for s in plugin.manifest["settings"]}
            raise AgentError("Fill in the agent's settings first: " + ", ".join(labels.get(k, k) for k in missing),
                             400, missing_settings=missing)
        clean = self.validate_inputs(plugin, action, inputs)
        prompt = self.render_action(agent_id, action_id, clean)
        runtimes = runtimes or self.state.runtimes
        first = next((str(v) for v in clean.values() if isinstance(v, str) and v), "")
        session_id = self._session(plugin, session_id, runtimes, f"{action['label']}{': ' + first if first else ''}")
        if not runtimes.start_turn(session_id, self.action_message(plugin, action, clean, prompt)):
            raise AgentError("This chat is still working. Wait for it to finish or press Stop.", 409)
        self._log_run(agent_id, {"time": _now(), "action": action_id, "label": action["label"],
                                 "inputs": clean, "session_id": session_id})
        return session_id

    def chat(self, agent_id, text, session_id=None, runtimes=None):
        """Send a chat message to the agent (a new chat unless session_id is given)."""
        plugin = self.get(agent_id)
        text = (text or "").strip()
        if not text:
            raise AgentError("Empty message")
        runtimes = runtimes or self.state.runtimes
        session_id = self._session(plugin, session_id, runtimes)
        if not runtimes.start_turn(session_id, text):
            raise AgentError("This chat is still working. Wait for it to finish or press Stop.", 409)
        return session_id

    def new_session(self, agent_id, runtimes=None):
        """An empty chat for this agent (written to disk with its first message)."""
        plugin = self.get(agent_id)
        return self._session(plugin, None, runtimes or self.state.runtimes)

    # ---------- exports ----------

    def export(self, agent_id, export_id):
        """(filename, media_type, bytes) for one of the manifest's exports."""
        plugin = self.get(agent_id)
        spec = next((e for e in plugin.manifest["exports"] if e["id"] == export_id), None)
        if spec is None:
            raise AgentError(f"{plugin.manifest['name']} has no export '{export_id}'", 404)
        tables = {t["name"]: t for t in plugin.manifest["records"]}
        rows = {name: self.table(agent_id, name).all() for name in tables}
        fmt = spec["format"]
        stem = spec["filename"] or f"{agent_id}-{export_id}-{today()}"
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem.replace("{date}", today())).strip("-") or export_id
        if fmt == "csv":
            body, ext, media = to_csv(tables[spec["records"]], rows[spec["records"]]), "csv", "text/csv"
        elif fmt == "markdown":
            body, ext, media = to_markdown(tables[spec["records"]], rows[spec["records"]]), "md", "text/markdown"
        elif fmt == "json":
            if spec["records"]:
                body = to_json(rows[spec["records"]])
            else:
                body = to_json({"agent": agent_id, "exported": _now(), "tables": rows})
            ext, media = "json", "application/json"
        else:
            source = (plugin.path / spec["template"]).read_text(encoding="utf-8")
            context = {"settings": self.settings_values(agent_id), "date": today(),
                       "agent": {"id": agent_id, "name": plugin.manifest["name"], "version": plugin.manifest["version"]},
                       "count": {n: len(r) for n, r in rows.items()}}
            try:
                body = render_template(source, context, rows)
            except TemplateError as error:
                raise AgentError(f"{spec['template']}: {error}") from None
            ext = Path(spec["template"]).suffix.lstrip(".") or "md"
            media = "text/markdown" if ext == "md" else "text/plain"
        if not stem.endswith(f".{ext}"):
            stem = f"{stem}.{ext}"
        from ..redaction import redact_text
        return stem, media, redact_text(body).encode("utf-8")

    # ---------- trust ----------

    def _trusted_hash(self, agent_id):
        try:
            return json.loads((self.data_dir(agent_id) / "trust.json").read_text(encoding="utf-8")).get("hash")
        except (OSError, ValueError, AttributeError):
            return None

    def trust(self, agent_id, trusted=True):
        """Allow (or revoke) loading this plugin's Python tools, for exactly its current code."""
        plugin = self.get(agent_id)
        path = self.data_dir(agent_id) / "trust.json"
        if trusted:
            atomic_write_json(path, {"hash": code_hash(plugin.path), "trusted_at": _now(), "files": plugin.py_files})
        elif path.exists():
            path.unlink()
        self._tool_cache.pop(agent_id, None)
        self.refresh_sessions(agent_id)
        return self.detail(agent_id)

    # ---------- import / install ----------

    def _clean_staging(self):
        if not self.staging_dir.is_dir():
            return
        for folder in self.staging_dir.iterdir():
            try:
                if time.time() - folder.stat().st_mtime > STAGING_MAX_AGE:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass

    def stage(self, files, skipped=(), source=""):
        """Put imported files in staging and return the review card (raises ManifestError)."""
        self._clean_staging()
        token = uuid.uuid4().hex[:16]
        folder = self.staging_dir / token
        plugin_files = {k: v for k, v in files.items() if not k.startswith("data/")}
        data_files = {k[len("data/"):]: v for k, v in files.items()
                      if re.match(r"^data/records/[a-z][a-z0-9_]*\.json$", k)}
        if "icon.svg" in plugin_files:
            packaging.check_svg(plugin_files["icon.svg"])
        packaging.write_files(plugin_files, folder / "plugin")
        if data_files:
            packaging.write_files(data_files, folder / "data")
        try:
            manifest, warnings = load_manifest(folder / "plugin")
        except ManifestError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        atomic_write_json(folder / "meta.json", {"source": source, "skipped": list(skipped), "created": _now()})
        return self.review(token, manifest, warnings, skipped, source)

    def review(self, token, manifest, warnings, skipped, source):
        folder = self.staging_dir / token
        existing = self.plugins().get(manifest["id"])
        data = {}
        records_dir = folder / "data" / "records"
        if records_dir.is_dir():
            for path in sorted(records_dir.glob("*.json")):
                try:
                    data[path.stem] = len(json.loads(path.read_text(encoding="utf-8")).get("records", []))
                except (OSError, ValueError, AttributeError):
                    pass
        plugin_dir = folder / "plugin"
        return {
            "staging": token, "source": source, "id": manifest["id"], "name": manifest["name"],
            "version": manifest["version"], "author": manifest["author"], "description": manifest["description"],
            "icon": manifest["icon"], "permissions": manifest["permissions"],
            "py_files": _py_files(plugin_dir),
            "skills": sorted(p.stem for p in (plugin_dir / "skills").glob("*.md")) if (plugin_dir / "skills").is_dir() else [],
            "actions": [a["label"] for a in manifest["actions"]],
            "records": [t["label"] for t in manifest["records"]],
            "exports": [e["label"] for e in manifest["exports"]],
            "settings": [s["label"] for s in manifest["settings"]],
            "warnings": [format_issue(w) for w in warnings], "skipped": list(skipped), "data": data,
            "existing": ({"builtin": existing.builtin, "version": existing.manifest["version"],
                          "name": existing.manifest["name"]} if existing else None),
        }

    def import_zip(self, data, source="upload"):
        files, skipped = packaging.read_zip(data)
        return self.stage(files, skipped, source)

    def import_url(self, url):
        data, subdir = packaging.download(url)
        files, skipped = packaging.read_zip(data, subdir)
        return self.stage(files, skipped, url)

    def import_folder(self, path):
        files, skipped = packaging.read_folder(path)
        return self.stage(files, skipped, str(path))

    def _free_id(self, base):
        plugins = self.plugins()
        base = base[:44]
        for n in range(2, 100):
            candidate = f"{base}-{n}"
            if candidate not in plugins and not (self.agents_dir / candidate).exists():
                return candidate
        raise AgentError("Too many copies of this agent")

    def install(self, token, mode="new", trust=False):
        """Install a staged import. mode: new | upgrade (keep data) | copy (new id)."""
        if not re.match(r"^[0-9a-f]{16}$", token or ""):
            raise AgentError("Unknown import; start the import again")
        folder = self.staging_dir / token
        if not (folder / "plugin" / "agent.toml").is_file():
            raise AgentError("This import has expired; start it again", 410)
        manifest, _ = load_manifest(folder / "plugin")
        agent_id = manifest["id"]
        with self._lock:
            existing = self.plugins().get(agent_id)
            if mode == "new" and (existing or (self.agents_dir / agent_id).exists()):
                raise AgentError(f"An agent with id '{agent_id}' already exists: choose Upgrade or Install as copy", 409)
            if mode == "upgrade":
                if existing is None:
                    mode = "new"
                elif existing.builtin:
                    raise AgentError("Built-in agents can't be upgraded; install it as a copy", 409)
            if mode == "copy":
                agent_id = self._free_id(manifest["id"])
                toml_path = folder / "plugin" / "agent.toml"
                toml_path.write_text(rewrite_identity(toml_path.read_text(encoding="utf-8"), agent_id,
                                                      f"{manifest['name']} (copy)"), encoding="utf-8")
                load_manifest(folder / "plugin")
            elif mode not in ("new", "upgrade"):
                raise AgentError(f"Unknown install mode '{mode}'")
            target = self.agents_dir / agent_id
            self.agents_dir.mkdir(parents=True, exist_ok=True)
            old = None
            if target.exists():
                old = self.staging_dir / f"{token}-old"
                target.replace(old)
            (folder / "plugin").replace(target)
            if old:
                shutil.rmtree(old, ignore_errors=True)
            self._restore_data(agent_id, folder / "data" / "records")
            shutil.rmtree(folder, ignore_errors=True)
            self._tool_cache.pop(agent_id, None)
            if trust:
                self.trust(agent_id, True)
            elif mode == "upgrade":
                self.refresh_sessions(agent_id)
        return self.detail(agent_id)

    def _restore_data(self, agent_id, records_dir):
        """Records from an 'export with data' zip, only into empty tables."""
        if not records_dir.is_dir():
            return
        plugin = self.get(agent_id)
        for spec in plugin.manifest["records"]:
            path = records_dir / f"{spec['name']}.json"
            table = self.table(agent_id, spec["name"])
            if path.is_file() and table.count() == 0:
                try:
                    records = json.loads(path.read_text(encoding="utf-8")).get("records", [])
                except (OSError, ValueError, AttributeError):
                    continue
                table.replace_all(records)

    def discard(self, token):
        if re.match(r"^[0-9a-f]{16}$", token or ""):
            shutil.rmtree(self.staging_dir / token, ignore_errors=True)

    # ---------- create, duplicate, remove, export ----------

    def create(self, name, description="", prompt="", icon="agents"):
        """'New agent' wizard: write a prompt-only plugin."""
        name = (name or "").strip()
        if not name:
            raise AgentError("Give the agent a name", 400, fields={"name": "is required"})
        if not (prompt or "").strip():
            raise AgentError("Describe the agent's role", 400, fields={"prompt": "is required"})
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "agent"
        agent_id = base if base not in self.plugins() and not (self.agents_dir / base).exists() else self._free_id(base)
        folder = self.agents_dir / agent_id
        folder.mkdir(parents=True)
        toml = "\n".join([
            "schema = 1", f"id = {_toml_string(agent_id)}", f"name = {_toml_string(name[:60])}",
            'version = "0.1.0"', f"description = {_toml_string((description or '').strip()[:300])}",
            'author = "You"', f"icon = {_toml_string(icon or 'agents')}", "",
            "[agent]", 'prompt = "prompt.md"', "", "[permissions]", "network = true", "commands = false",
            'files = "data"', ""])
        (folder / "agent.toml").write_text(toml, encoding="utf-8")
        (folder / "prompt.md").write_text(prompt.strip() + "\n", encoding="utf-8")
        (folder / "README.md").write_text(f"# {name}\n\n{(description or '').strip()}\n\n"
                                          "Created with the New agent wizard. Edit `prompt.md` to refine its role, "
                                          "or add settings, records and actions to `agent.toml` "
                                          "(see docs/agent-plugins.md).\n", encoding="utf-8")
        try:
            load_manifest(folder)
        except ManifestError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return self.detail(agent_id)

    def duplicate(self, agent_id):
        """Copy a plugin (built-in or installed) to a new id to customise; settings come along, records don't."""
        plugin = self.get(agent_id)
        new_id = self._free_id(agent_id)
        target = self.agents_dir / new_id
        shutil.copytree(plugin.path, target, ignore=shutil.ignore_patterns("__pycache__"))
        toml_path = target / "agent.toml"
        toml_path.write_text(rewrite_identity(toml_path.read_text(encoding="utf-8"), new_id,
                                              f"{plugin.manifest['name']} (copy)"), encoding="utf-8")
        stored = self._stored(agent_id)
        if stored:
            atomic_write_json(self._settings_file(new_id), stored)
        for s in plugin.manifest["settings"]:
            if s["type"] == "secret" and keystore.get(self.secret_name(agent_id, s["key"])):
                keystore.set(self.secret_name(new_id, s["key"]), keystore.get(self.secret_name(agent_id, s["key"])))
        if plugin.trusted and plugin.py_files:
            self.trust(new_id, True)
        return self.detail(new_id)

    def remove(self, agent_id, delete_data=True):
        plugin = self.get(agent_id)
        if plugin.builtin:
            raise AgentError("Built-in agents can't be removed", 400)
        for session_id in self.state.runtimes.ids():
            jarvis = self.state.runtimes.get(session_id)
            if getattr(getattr(jarvis, "profile", None), "owner", None) == agent_id:
                self.state.runtimes.close(session_id)
        shutil.rmtree(plugin.path)
        self._tool_cache.pop(agent_id, None)
        if delete_data:
            shutil.rmtree(self.data_dir(agent_id), ignore_errors=True)
            for s in plugin.manifest["settings"]:
                if s["type"] == "secret":
                    keystore.delete(self.secret_name(agent_id, s["key"]))
        return {"removed": agent_id}

    def export_zip(self, agent_id, with_data=False):
        """(filename, bytes): the plugin folder, plus data/records/* when asked. Never secrets."""
        plugin = self.get(agent_id)
        extra = {}
        if with_data:
            for spec in plugin.manifest["records"]:
                path = self.data_dir(agent_id) / "records" / f"{spec['name']}.json"
                if path.is_file():
                    from ..redaction import redact_text
                    extra[f"data/records/{spec['name']}.json"] = redact_text(path.read_text(encoding="utf-8"))
        suffix = "-with-data" if with_data else ""
        return f"{agent_id}-{plugin.manifest['version']}{suffix}.zip", packaging.zip_folder(plugin.path, agent_id, extra)

    def icon_path(self, agent_id):
        path = self.get(agent_id).path / "icon.svg"
        return path if path.is_file() else None


def _coerce_input(spec, value):
    """Validate a setting or action input value by its type."""
    kind = spec["type"]
    if value is None or value == "" or value == []:
        return value if value is not None else None
    if kind == "multiselect":
        items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        out = []
        for item in items:
            if not str(item).strip():
                continue
            out.append(coerce({"type": "select", "options": spec["options"]}, item))
        return out
    if kind in ("text", "textarea", "secret"):
        if isinstance(value, (dict, list)):
            raise FieldError("must be text")
        return str(value).strip()
    return coerce({**spec, "type": {"url": "url", "number": "number", "boolean": "boolean",
                                    "select": "select"}.get(kind, "text")}, value)


__all__ = ["AgentManager", "AgentError", "Plugin", "ID_RE"]
