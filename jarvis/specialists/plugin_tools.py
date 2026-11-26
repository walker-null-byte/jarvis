"""Tools an agent gets on top of Jarvis's own: records tools and its plugin's tools/*.py.

Plugin tool files are plain Python, loaded only after the user trusts the plugin:

    def register(ctx):
        @ctx.tool
        def find_contact(name: str, hint: str = "") -> dict:
            "Search the web for a person's public email/phone/website."
            ...

Each plugin gets its own module namespace (`jarvis_plugins.<id>`), so two agents
can both have a tools/search.py. Files starting with "_" are helpers that tool
files may import (`from . import _helpers`); they are not registered.
"""

import importlib.util
import re
import sys
import threading
import traceback
import types
from collections.abc import Mapping
from pathlib import Path

from .records import FieldError

RECORD_TOOL_NAMES = ("records_add", "records_update", "records_query", "records_delete")
_http = {"client": None}
_http_lock = threading.Lock()


def shared_http():
    """One httpx.Client for all plugins: timeouts and a polite User-Agent."""
    with _http_lock:
        if _http["client"] is None:
            import httpx
            _http["client"] = httpx.Client(
                timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis-agent/1.1; personal assistant)"})
        return _http["client"]


class _NoNetwork:
    def __getattr__(self, name):
        raise PermissionError("this agent's manifest says network = false")


class SettingsView(Mapping):
    """Read-only, always-current view of an agent's settings (secrets resolved)."""

    def __init__(self, read):
        self._read = read

    def __getitem__(self, key):
        return self._read()[key]

    def __iter__(self):
        return iter(self._read())

    def __len__(self):
        return len(self._read())

    def get(self, key, default=None):
        value = self._read().get(key)
        return default if value in (None, "") else value


class PluginContext:
    """The `ctx` passed to a plugin's register(ctx)."""

    def __init__(self, manager, plugin):
        self._manager = manager
        self.id = plugin.id
        self.name = plugin.manifest["name"]
        self.data_dir = manager.data_dir(plugin.id)
        self.settings = SettingsView(lambda: manager.settings_values(plugin.id, secrets=True))
        self.http = shared_http() if plugin.manifest["permissions"]["network"] else _NoNetwork()
        self.tools = {}

    def tool(self, fn=None, *, name=None, description=None):
        """Decorator: @ctx.tool or @ctx.tool(name="x"). The agent sees the short name."""
        def wrap(func):
            tool_name = name or func.__name__
            if tool_name in RECORD_TOOL_NAMES:
                raise ValueError(f"'{tool_name}' is reserved for the records tools")
            if description:
                func.__doc__ = description
            self.tools[tool_name] = func
            return func
        return wrap(fn) if fn is not None else wrap

    def records(self, name):
        """A RecordTable: .add(items) .update(id, fields) .query(where, limit) .get(id) .delete(id)."""
        return self._manager.table(self.id, name)

    def call(self, tool_name, **arguments):
        """Run one of Jarvis's own tools (e.g. web_search, fetch_page) and return its result."""
        return self._manager.call_jarvis_tool(tool_name, arguments)

    def log(self, message):
        self._manager.log(self.id, str(message))


def load_plugin_tools(manager, plugin):
    """Import a trusted plugin's tools/*.py. Returns (tools {name: fn}, errors [{file, error}])."""
    folder = Path(plugin.path) / "tools"
    if not folder.is_dir():
        return {}, []
    return import_tool_files(PluginContext(manager, plugin), folder, plugin.id)


def import_tool_files(ctx, folder, plugin_id):
    """Import every non-helper .py in `folder` as jarvis_plugins.<id>.<file> and call register(ctx).

    Shared with the builder's test runner, which passes a stub ctx. Returns (tools, errors).
    """
    folder = Path(folder)
    package = f"jarvis_plugins.{plugin_id.replace('-', '_')}"
    if "jarvis_plugins" not in sys.modules:
        root = types.ModuleType("jarvis_plugins")
        root.__path__ = []
        sys.modules["jarvis_plugins"] = root
    for key in [k for k in sys.modules if k == package or k.startswith(package + ".")]:
        del sys.modules[key]  # reload: forget the previous version of this plugin
    holder = types.ModuleType(package)
    holder.__path__ = [str(folder)]
    sys.modules[package] = holder
    errors = []
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        rel = f"tools/{path.name}"
        try:
            spec = importlib.util.spec_from_file_location(f"{package}.{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                errors.append({"file": rel, "error": "has no register(ctx) function"})
                continue
            before = set(ctx.tools)
            register(ctx)
            if set(ctx.tools) == before:
                errors.append({"file": rel, "error": "register(ctx) did not add any @ctx.tool"})
        except Exception as error:  # plugin code: report, never crash Jarvis
            frames = traceback.extract_tb(error.__traceback__)
            line = next((f"line {f.lineno}" for f in reversed(frames) if f.filename == str(path)), "")
            errors.append({"file": rel, "error": f"{error.__class__.__name__}: {error}" + (f" ({line})" if line else "")})
    return dict(ctx.tools), errors


# ---------- records tools (schemas generated from the manifest) ----------

def _field_schema(field):
    schema = {"number": {"type": "number"}, "boolean": {"type": "boolean"}}.get(field["type"], {"type": "string"})
    if field["type"] == "select":
        schema["enum"] = list(field["options"])
    hints = [field.get("help") or ""]
    if field["type"] in ("url", "email", "date"):
        hints.append({"url": "full https:// URL", "email": "email address", "date": "YYYY-MM-DD"}[field["type"]])
    if field.get("min") is not None or field.get("max") is not None:
        hints.append(f"{field.get('min', '')}–{field.get('max', '')}")
    if field.get("required"):
        hints.append("required")
    text = "; ".join(h for h in hints if h)
    if text:
        schema["description"] = text
    return schema


def _item_schema(table, partial=False):
    properties = {f["name"]: _field_schema(f) for f in table["fields"]}
    schema = {"type": "object", "properties": properties}
    required = [f["name"] for f in table["fields"] if f.get("required")]
    if required and not partial:
        schema["required"] = required
    return schema


def table_summary(table):
    """'leads: name (text, required), platform (instagram|linkedin|other), … unique by profile_url'"""
    parts = []
    for f in table["fields"]:
        kind = "|".join(f["options"]) if f["type"] == "select" else f["type"]
        extra = ", required" if f.get("required") else ""
        if f.get("editable"):
            extra += ", user-editable"
        parts.append(f"{f['name']} ({kind}{extra})")
    unique = f"; one record per {' + '.join(table['unique'])}" if table.get("unique") else ""
    return f"`{table['name']}` ({table['label']}): " + ", ".join(parts) + unique


def build_record_tools(manager, plugin):
    """records_add / records_update / records_query / records_delete for this agent's tables."""
    tables = plugin.manifest["records"]
    if not tables:
        return {}
    names = [t["name"] for t in tables]
    by_name = {t["name"]: t for t in tables}
    single = names[0] if len(names) == 1 else None
    table_prop = {"type": "string", "enum": names, "description": "Which table"}
    if single:
        table_prop["default"] = single

    def pick(table):
        table = table or single
        if table not in by_name:
            raise FieldError(f"unknown table '{table}'; tables: {', '.join(names)}")
        return manager.table(plugin.id, table)

    def guard(fn):
        def run(**kwargs):
            try:
                return fn(**kwargs)
            except (FieldError, KeyError) as error:
                message = error.args[0] if error.args else str(error)
                return {"error": str(message)}
        run.__name__ = fn.__name__
        run.__doc__ = fn.__doc__
        return run

    def records_add(items, table=None):
        result = pick(table).add(items)
        total = pick(table).count()
        summary = f"added {result['added']}, updated {result['updated']}, skipped {result['skipped']}; table now has {total}"
        return {"summary": summary, **result}

    def records_update(id, fields, table=None):  # noqa: A002 - the model-facing name
        if not isinstance(fields, dict) or not fields:
            raise FieldError("fields must be an object of the values to change")
        return {"updated": pick(table).update(str(id), fields)}

    def records_query(where=None, search="", limit=50, fields=None, table=None):
        target = pick(table)
        where = where or {}
        if not isinstance(where, dict):
            raise FieldError("where must be an object like {\"status\": \"new\"}")
        unknown = [k for k in where if k not in target.fields and k != "id"]
        if unknown:
            raise FieldError(f"unknown field(s) in where: {', '.join(unknown)}; fields: {', '.join(target.fields)}")
        try:
            limit = max(1, min(int(limit or 50), 200))
        except (TypeError, ValueError):
            limit = 50
        search = str(search or "")
        result = target.query(where, search, limit, fields=fields or None)
        if search and not result["total"] and re.search(r"\w\s*[:=]|\b(AND|OR)\b", search):
            # Small models write query languages into `search`; it is plain text. Use `where` alone.
            result = target.query(where, "", limit, fields=fields or None)
            result["note"] = "search is plain text (not field:value); ignored it. Use where for field filters."
        return result

    def records_delete(id, table=None):  # noqa: A002
        return {"deleted": pick(table).delete(str(id)), "id": str(id)}

    any_item = {"anyOf": [_item_schema(t) for t in tables]} if not single else _item_schema(tables[0])
    any_partial = {"anyOf": [_item_schema(t, True) for t in tables]} if not single else _item_schema(tables[0], True)
    summaries = "\n".join(table_summary(t) for t in tables)
    specs = {
        "records_add": (records_add,
                        "Save records to your workspace table (the user sees them live). Duplicates (same "
                        f"unique key) are merged, not repeated. Returns added/updated/skipped counts.\n{summaries}",
                        {"items": {"type": "array", "items": any_item, "description": "Records to save"},
                         "table": table_prop}, ["items"]),
        "records_update": (records_update,
                           "Change fields of one record by id (e.g. status, notes, a drafted message).",
                           {"id": {"type": "string", "description": "Record id from records_add/records_query"},
                            "fields": {**any_partial, "description": "Only the fields to change; null empties a field"},
                            "table": table_prop}, ["id", "fields"]),
        "records_query": (records_query,
                          "List records. where = exact matches, e.g. {\"status\": \"replied\"}; "
                          "{\"email\": \"\"} finds records missing an email; a list means any of.",
                          {"where": {"type": "object", "description": "Field filters"},
                           "search": {"type": "string", "description": "Text to look for anywhere in a record"},
                           "limit": {"type": "integer", "description": "Max records (default 50)"},
                           "fields": {"type": "array", "items": {"type": "string"},
                                      "description": "Only return these fields (saves space)"},
                           "table": table_prop}, []),
        "records_delete": (records_delete, "Delete one record by id.",
                           {"id": {"type": "string"}, "table": table_prop}, ["id"]),
    }
    tools = {}
    for name, (fn, description, properties, required) in specs.items():
        run = guard(fn)
        run.__doc__ = description
        run.input_schema = {"type": "object", "properties": properties, **({"required": required} if required else {})}
        tools[name] = run
    return tools


def skill_tool(plugin):
    """agent_skill(name): read one of the plugin's skills/*.md (None when it has none)."""
    folder = Path(plugin.path) / "skills"
    names = sorted(p.stem for p in folder.glob("*.md")) if folder.is_dir() else []
    if not names:
        return None

    def agent_skill(name: str):
        stem = str(name).split("/")[-1].removesuffix(".md")
        if stem not in names:
            return {"error": f"no skill '{name}'; skills: {', '.join(names)}"}
        return (folder / f"{stem}.md").read_text(encoding="utf-8")

    agent_skill.__doc__ = f"Read one of your playbooks before following it. Available: {', '.join(names)}."
    agent_skill.input_schema = {"type": "object", "properties": {
        "name": {"type": "string", "enum": names, "description": "Skill name"}}, "required": ["name"]}
    return agent_skill
