"""Saved API keys: $XDG_CONFIG_HOME/jarvis/keys.json (dir 0700, file 0600).

Names are env-var style (GEMINI_API_KEY); plugin secrets are namespaced
"agent:<id>:<KEY>". Values are never logged or returned by `names()`/`hint()`.
Set JARVIS_KEYS_FILE to use another file (tests always do).
"""

import json
import os
import threading
from pathlib import Path

_lock = threading.RLock()


def keys_path():
    override = os.environ.get("JARVIS_KEYS_FILE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jarvis" / "keys.json"


def _load():
    try:
        data = json.loads(keys_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _save(data):
    path = keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def get(name):
    """The saved value for *name*, or None."""
    return _load().get(name) or None


def set(name, value):  # noqa: A001 - spec name
    """Save *value* under *name* (an empty value deletes it)."""
    value = (value or "").strip()
    if not value:
        delete(name)
        return
    with _lock:
        data = _load()
        data[name] = value
        _save(data)


def delete(name):
    """Remove *name*; returns True if it existed."""
    with _lock:
        data = _load()
        if name not in data:
            return False
        del data[name]
        _save(data)
        return True


def names():
    """Saved names (never values), sorted."""
    return sorted(_load())


def hint(name):
    """'••••abcd' (last 4 characters) or '' when nothing is saved."""
    value = get(name)
    return "••••" + value[-4:] if value else ""


_cache = {"stamp": None, "values": []}


def saved_values():
    """All saved values (6+ chars); only for redaction. Cached by file mtime."""
    try:
        stat = keys_path().stat()
        stamp = (str(keys_path()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []
    if _cache["stamp"] != stamp:
        _cache["values"] = [v for v in _load().values() if len(v) >= 6]
        _cache["stamp"] = stamp
    return _cache["values"]
