"""agent.toml: parse, validate and normalise a plugin manifest (schema = 1).

`load_manifest(folder)` returns `(manifest, warnings)` or raises `ManifestError`
whose `.issues` say exactly what is wrong: file, key and a plain explanation.
Unknown keys are warnings, never failures, so newer plugins still load.
"""

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

SCHEMA = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}([-+][0-9A-Za-z.-]+)?$")

SETTING_TYPES = ("text", "textarea", "number", "boolean", "select", "multiselect", "url", "secret")
INPUT_TYPES = ("text", "textarea", "number", "boolean", "select", "multiselect", "url")
FIELD_TYPES = ("text", "textarea", "number", "boolean", "select", "url", "email", "date")
EXPORT_FORMATS = ("csv", "json", "markdown", "template")
FILE_MODES = ("data", "workspace", "none")
RESERVED_FIELDS = ("id", "created", "updated")

TOP_KEYS = {"schema", "id", "name", "version", "description", "author", "icon", "min_jarvis",
            "homepage", "license", "tags", "agent", "permissions", "settings", "records",
            "actions", "exports"}
AGENT_KEYS = {"prompt", "greeting", "provider", "model", "core_tools", "max_tool_rounds",
              "suggestions", "replace_persona"}
PERMISSION_KEYS = {"network", "commands", "files"}
SETTING_KEYS = {"key", "label", "type", "required", "help", "default", "options", "placeholder"}
RECORD_KEYS = {"name", "label", "unique", "title_field", "fields", "status_field"}
FIELD_KEYS = {"name", "label", "type", "required", "options", "default", "min", "max",
              "editable", "help", "hidden"}
ACTION_KEYS = {"id", "label", "description", "inputs", "prompt", "icon", "records"}
INPUT_KEYS = {"key", "label", "type", "required", "default", "options", "help", "placeholder", "min", "max"}
EXPORT_KEYS = {"id", "label", "format", "records", "template", "filename", "description"}


class ManifestError(ValueError):
    """A manifest that cannot be used. `issues` is a list of {file, key, message}."""

    def __init__(self, issues):
        self.issues = issues
        super().__init__("\n".join(format_issue(i) for i in issues))


def format_issue(issue):
    where = issue["file"] + (f" → {issue['key']}" if issue.get("key") else "")
    return f"{where}: {issue['message']}"


def parse_version(text):
    """'1.2.3' -> (1, 2, 3); pre-release suffixes are ignored. None if unparseable."""
    match = re.match(r"^\s*(\d+(?:\.\d+){0,3})", str(text or ""))
    if not match:
        return None
    parts = [int(p) for p in match.group(1).split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def jarvis_version():
    """This Jarvis's version: pyproject.toml in a source checkout, else package metadata."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, ValueError):
        pass
    try:
        from importlib.metadata import version
        return version("jarvis")
    except Exception:
        from .. import __version__
        return __version__


class _Checker:
    """Collects errors and warnings while walking the manifest."""

    def __init__(self, filename="agent.toml"):
        self.file = filename
        self.errors = []
        self.warnings = []

    def error(self, key, message):
        self.errors.append({"file": self.file, "key": key, "message": message})

    def warn(self, key, message):
        self.warnings.append({"file": self.file, "key": key, "message": message})

    def unknown(self, table, allowed, where):
        for key in table:
            if key not in allowed:
                self.warn(f"{where}{key}" if where else key, "unknown key (ignored)")

    def text(self, table, key, where, required=False, default=""):
        value = table.get(key, None)
        if value is None:
            if required:
                self.error(where + key, "is required")
            return default
        if not isinstance(value, str):
            self.error(where + key, f"must be a string, got {type(value).__name__}")
            return default
        if required and not value.strip():
            self.error(where + key, "must not be empty")
        return value.strip() if "\n" not in value else value

    def boolean(self, table, key, where, default=False):
        value = table.get(key, default)
        if not isinstance(value, bool):
            self.error(where + key, "must be true or false")
            return default
        return value

    def number(self, table, key, where, default=None, integer=False):
        value = table.get(key, default)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
            self.error(where + key, f"must be {'a whole number' if integer else 'a number'}")
            return default
        return value

    def strings(self, table, key, where):
        value = table.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.error(where + key, "must be a list of strings")
            return []
        return [v.strip() for v in value if v.strip()]

    def table_list(self, data, key):
        value = data.get(key, [])
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            self.error(key, f"must be a list of tables, written as [[{key}]]")
            return []
        return value


def _options(check, item, where, kind):
    options = item.get("options")
    if kind in ("select", "multiselect"):
        if not isinstance(options, list) or not options or not all(isinstance(o, str) and o.strip() for o in options):
            check.error(where + "options", f"a {kind} needs a non-empty list of option strings")
            return []
        return [o.strip() for o in options]
    if options is not None:
        check.warn(where + "options", f"ignored: only select fields have options (type is {kind})")
    return []


def _check_default(check, item, where, kind, options):
    if "default" not in item:
        return None
    value = item["default"]
    if kind == "secret":
        check.error(where + "default", "a secret setting cannot have a default value")
        return None
    if kind == "boolean" and not isinstance(value, bool):
        check.error(where + "default", "must be true or false for a boolean")
    elif kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        check.error(where + "default", "must be a number")
    elif kind == "select" and value not in options:
        check.error(where + "default", f"'{value}' is not one of the options: {', '.join(options)}")
    elif kind == "multiselect" and (not isinstance(value, list) or any(v not in options for v in value)):
        check.error(where + "default", "must be a list of the options")
    elif kind not in ("boolean", "number", "multiselect") and not isinstance(value, str):
        check.error(where + "default", "must be a string")
    return value


def _input_like(check, item, where, types, key_name="key", label_default=None):
    """A [[settings]] entry or an action input (same shape)."""
    check.unknown(item, SETTING_KEYS | INPUT_KEYS, where)
    key = check.text(item, key_name, where, required=True)
    if key and not KEY_RE.match(key):
        check.error(where + key_name, f"'{key}' must be letters, digits and _ (starting with a letter)")
    kind = check.text(item, "type", where, default="text") or "text"
    if kind not in types:
        check.error(where + "type", f"'{kind}' is not a valid type (use one of: {', '.join(types)})")
        kind = "text"
    options = _options(check, item, where, kind)
    return {
        "key": key,
        "label": check.text(item, "label", where) or label_default or key,
        "type": kind,
        "required": check.boolean(item, "required", where),
        "help": check.text(item, "help", where),
        "placeholder": check.text(item, "placeholder", where),
        "options": options,
        "default": _check_default(check, item, where, kind, options),
        "min": check.number(item, "min", where),
        "max": check.number(item, "max", where),
    }


def _field(check, item, where):
    check.unknown(item, FIELD_KEYS, where)
    name = check.text(item, "name", where, required=True)
    if name and not NAME_RE.match(name):
        check.error(where + "name", f"'{name}' must be lowercase letters, digits and _ (starting with a letter)")
    if name in RESERVED_FIELDS:
        check.error(where + "name", f"'{name}' is reserved (every record already has id, created, updated)")
    kind = check.text(item, "type", where, default="text") or "text"
    if kind not in FIELD_TYPES:
        check.error(where + "type", f"'{kind}' is not a field type (use one of: {', '.join(FIELD_TYPES)})")
        kind = "text"
    options = _options(check, item, where, kind)
    return {
        "name": name,
        "label": check.text(item, "label", where) or (name or "").replace("_", " ").capitalize(),
        "type": kind,
        "required": check.boolean(item, "required", where),
        "options": options,
        "default": _check_default(check, item, where, kind, options),
        "min": check.number(item, "min", where),
        "max": check.number(item, "max", where),
        "editable": check.boolean(item, "editable", where),
        "hidden": check.boolean(item, "hidden", where),
        "help": check.text(item, "help", where),
    }


def _records(check, data):
    tables = []
    seen = set()
    for n, item in enumerate(check.table_list(data, "records")):
        where = f"records[{n}]."
        check.unknown(item, RECORD_KEYS, where)
        name = check.text(item, "name", where, required=True)
        if name and not NAME_RE.match(name):
            check.error(where + "name", f"'{name}' must be lowercase letters, digits and _")
        if name in seen:
            check.error(where + "name", f"duplicate table name '{name}'")
        seen.add(name)
        raw_fields = item.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields or not all(isinstance(f, dict) for f in raw_fields):
            check.error(where + "fields", "needs a non-empty list of fields like { name = \"title\", type = \"text\" }")
            raw_fields = []
        fields = [_field(check, f, f"{where}fields[{i}].") for i, f in enumerate(raw_fields)]
        names = [f["name"] for f in fields]
        for dup in {x for x in names if names.count(x) > 1 and x}:
            check.error(where + "fields", f"field '{dup}' is defined twice")
        unique = check.strings(item, "unique", where)
        for u in unique:
            if u not in names:
                check.error(where + "unique", f"'{u}' is not one of this table's fields ({', '.join(names)})")
        title = check.text(item, "title_field", where) or (names[0] if names else "")
        if title and title not in names:
            check.error(where + "title_field", f"'{title}' is not one of this table's fields")
        status = check.text(item, "status_field", where)
        if not status:
            status = "status" if any(f["name"] == "status" and f["type"] == "select" for f in fields) else ""
        elif not any(f["name"] == status and f["type"] == "select" for f in fields):
            check.error(where + "status_field", f"'{status}' must name a select field")
        tables.append({"name": name, "label": check.text(item, "label", where) or (name or "").capitalize(),
                       "unique": unique, "title_field": title, "status_field": status, "fields": fields})
    return tables


PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


def _actions(check, data, settings, tables):
    actions = []
    seen = set()
    setting_keys = {s["key"] for s in settings}
    table_names = {t["name"] for t in tables}
    for n, item in enumerate(check.table_list(data, "actions")):
        where = f"actions[{n}]."
        check.unknown(item, ACTION_KEYS, where)
        action_id = check.text(item, "id", where, required=True)
        if action_id and not ID_RE.match(action_id):
            check.error(where + "id", f"'{action_id}' must be lowercase letters, digits and -")
        if action_id in seen:
            check.error(where + "id", f"duplicate action id '{action_id}'")
        seen.add(action_id)
        raw_inputs = item.get("inputs", [])
        if not isinstance(raw_inputs, list) or not all(isinstance(i, dict) for i in raw_inputs):
            check.error(where + "inputs", "must be a list of inputs like { key = \"topic\", type = \"text\" }")
            raw_inputs = []
        inputs = [_input_like(check, i, f"{where}inputs[{k}].", INPUT_TYPES) for k, i in enumerate(raw_inputs)]
        prompt = check.text(item, "prompt", where, required=True)
        input_keys = {i["key"] for i in inputs}
        for name in PLACEHOLDER_RE.findall(prompt or ""):
            head, _, rest = name.partition(".")
            if "." not in name and name not in input_keys and name != "input":
                check.warn(where + "prompt", f"{{{name}}} is not an input of this action (left as written)")
            elif head == "settings" and rest not in setting_keys:
                check.warn(where + "prompt", f"{{{name}}}: there is no setting '{rest}'")
            elif head == "records" and rest.split(".")[0] not in table_names:
                check.warn(where + "prompt", f"{{{name}}}: there is no records table '{rest.split('.')[0]}'")
        records = check.text(item, "records", where)
        if records and records not in table_names:
            check.error(where + "records", f"'{records}' is not a records table")
        actions.append({"id": action_id, "label": check.text(item, "label", where) or action_id,
                        "description": check.text(item, "description", where), "icon": check.text(item, "icon", where),
                        "inputs": inputs, "prompt": prompt, "records": records})
    return actions


def _exports(check, data, tables, folder):
    exports = []
    seen = set()
    table_names = {t["name"] for t in tables}
    for n, item in enumerate(check.table_list(data, "exports")):
        where = f"exports[{n}]."
        check.unknown(item, EXPORT_KEYS, where)
        export_id = check.text(item, "id", where, required=True)
        if export_id and not ID_RE.match(export_id):
            check.error(where + "id", f"'{export_id}' must be lowercase letters, digits and -")
        if export_id in seen:
            check.error(where + "id", f"duplicate export id '{export_id}'")
        seen.add(export_id)
        fmt = check.text(item, "format", where, default="csv")
        if fmt not in EXPORT_FORMATS:
            check.error(where + "format", f"'{fmt}' is not a format (use one of: {', '.join(EXPORT_FORMATS)})")
        records = check.text(item, "records", where)
        if records and records not in table_names:
            check.error(where + "records", f"'{records}' is not a records table ({', '.join(sorted(table_names)) or 'none defined'})")
        if fmt in ("csv", "markdown") and not records:
            if len(table_names) == 1:
                records = next(iter(table_names))
            else:
                check.error(where + "records", f"a {fmt} export needs records = \"<table>\"")
        template = check.text(item, "template", where)
        if fmt == "template":
            if not template:
                check.error(where + "template", "a template export needs template = \"exports/<file>.md\"")
            else:
                path = _inside(folder, template)
                if path is None:
                    check.error(where + "template", f"'{template}' must be a relative path inside the plugin")
                elif not path.is_file():
                    check.error(where + "template", f"file '{template}' does not exist")
                else:
                    from .templating import TemplateError, parse_template
                    try:
                        parse_template(path.read_text(encoding="utf-8"))
                    except (TemplateError, UnicodeDecodeError) as error:
                        check.errors.append({"file": template, "key": "", "message": str(error)})
        exports.append({"id": export_id, "label": check.text(item, "label", where) or export_id, "format": fmt,
                        "records": records, "template": template, "filename": check.text(item, "filename", where),
                        "description": check.text(item, "description", where)})
    return exports


def _inside(folder, relative):
    """folder/relative if it stays inside folder, else None."""
    if not relative or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        return None
    path = (Path(folder) / relative).resolve()
    root = Path(folder).resolve()
    return path if path == root or root in path.parents else None


def validate(data, folder, check_version=True):
    """Validate parsed agent.toml data from `folder`. Returns (manifest, warnings)."""
    check = _Checker()
    check.unknown(data, TOP_KEYS, "")
    schema = data.get("schema")
    if schema != SCHEMA:
        check.error("schema", f"must be {SCHEMA} (got {schema!r}); this Jarvis understands schema {SCHEMA}")
    agent_id = check.text(data, "id", "", required=True)
    if agent_id and not ID_RE.match(agent_id):
        check.error("id", f"'{agent_id}' must be 1-48 lowercase letters, digits and - (like \"lead-finder\")")
    version = check.text(data, "version", "", default="0.1.0") or "0.1.0"
    if not VERSION_RE.match(version):
        check.error("version", f"'{version}' is not a version like 1.0.0")
    min_jarvis = check.text(data, "min_jarvis", "")
    if min_jarvis:
        needed, have = parse_version(min_jarvis), parse_version(jarvis_version())
        if needed is None:
            check.error("min_jarvis", f"'{min_jarvis}' is not a version like 1.1.2")
        elif check_version and have and needed > have:
            check.error("min_jarvis", f"this agent needs Jarvis {min_jarvis} or newer; you have {jarvis_version()}")

    agent = data.get("agent", {})
    if not isinstance(agent, dict):
        check.error("agent", "must be a table: [agent]")
        agent = {}
    check.unknown(agent, AGENT_KEYS, "agent.")
    prompt_file = check.text(agent, "prompt", "agent.", default="prompt.md") or "prompt.md"
    prompt_path = _inside(folder, prompt_file)
    if prompt_path is None:
        check.error("agent.prompt", f"'{prompt_file}' must be a relative path inside the plugin")
    elif not prompt_path.is_file():
        check.errors.append({"file": prompt_file, "key": "", "message": "is missing (every agent needs its role prompt)"})
    max_rounds = check.number(agent, "max_tool_rounds", "agent.", integer=True)
    if max_rounds is not None and not 1 <= max_rounds <= 200:
        check.error("agent.max_tool_rounds", "must be between 1 and 200")
        max_rounds = None

    permissions = data.get("permissions", {})
    if not isinstance(permissions, dict):
        check.error("permissions", "must be a table: [permissions]")
        permissions = {}
    check.unknown(permissions, PERMISSION_KEYS, "permissions.")
    files = check.text(permissions, "files", "permissions.", default="data") or "data"
    if files not in FILE_MODES:
        check.error("permissions.files", f"'{files}' must be one of: {', '.join(FILE_MODES)}")

    settings = [_input_like(check, s, f"settings[{n}].", SETTING_TYPES)
                for n, s in enumerate(check.table_list(data, "settings"))]
    keys = [s["key"] for s in settings]
    for dup in {k for k in keys if keys.count(k) > 1 and k}:
        check.error("settings", f"setting '{dup}' is defined twice")
    tables = _records(check, data)
    actions = _actions(check, data, settings, tables)
    exports = _exports(check, data, tables, folder)

    icon = check.text(data, "icon", "", default="agents") or "agents"
    if check.errors:
        raise ManifestError(check.errors)
    manifest = {
        "schema": SCHEMA, "id": agent_id, "name": check.text(data, "name", "", required=True) or agent_id,
        "version": version, "description": check.text(data, "description", ""),
        "author": check.text(data, "author", ""), "icon": icon, "min_jarvis": min_jarvis,
        "homepage": check.text(data, "homepage", ""), "license": check.text(data, "license", ""),
        "tags": check.strings(data, "tags", ""),
        "agent": {
            "prompt": prompt_file, "greeting": check.text(agent, "greeting", "agent."),
            "provider": check.text(agent, "provider", "agent."), "model": check.text(agent, "model", "agent."),
            "core_tools": check.strings(agent, "core_tools", "agent."), "max_tool_rounds": max_rounds,
            "suggestions": check.strings(agent, "suggestions", "agent."),
            "replace_persona": check.boolean(agent, "replace_persona", "agent.", default=True),
        },
        "permissions": {"network": check.boolean(permissions, "network", "permissions.", default=True),
                        "commands": check.boolean(permissions, "commands", "permissions.", default=False),
                        "files": files},
        "settings": settings, "records": tables, "actions": actions, "exports": exports,
    }
    if check.errors:  # name/tags checks above can still fail
        raise ManifestError(check.errors)
    return manifest, check.warnings


def load_manifest(folder, check_version=True):
    """Read and validate folder/agent.toml. Returns (manifest, warnings)."""
    folder = Path(folder)
    path = folder / "agent.toml"
    if not path.is_file():
        raise ManifestError([{"file": "agent.toml", "key": "", "message": "is missing: every agent folder needs one"}])
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        raise ManifestError([{"file": "agent.toml", "key": "", "message": "is not UTF-8 text"}]) from None
    except tomllib.TOMLDecodeError as error:
        raise ManifestError([{"file": "agent.toml", "key": "", "message": f"is not valid TOML: {error}"}]) from None
    return validate(data, folder, check_version)
