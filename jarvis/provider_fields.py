"""Non-secret per-provider settings, e.g. Cloudflare's Account ID.

A preset lists them under "fields"; each fills a "{key}" in its base URL or a
request header. They are saved in provider_fields.json next to the saved keys,
by env-var name so presets can share one. Not kept in the keystore: its values
are redacted from all output, which would mangle ordinary words like "default".

Value order: per-request override, saved value, env var, the field's default.
"""

import json
import os
import re
import threading

from . import keystore
from .providers import PRESETS

# Values go into URL paths and headers, so only plain id characters are allowed.
VALUE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_lock = threading.RLock()


def _path():
    return keystore.keys_path().with_name("provider_fields.json")


def _load():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def fields_of(provider):
    return PRESETS.get(provider, {}).get("fields", [])


def saved(field):
    return str(_load().get(field["env"]) or "")


def value(field, overrides=None):
    return (str((overrides or {}).get(field["key"]) or "").strip() or saved(field)
            or os.environ.get(field["env"], "").strip() or field.get("default", ""))


def values(provider, overrides=None):
    return {f["key"]: value(f, overrides) for f in fields_of(provider)}


def missing(provider, overrides=None):
    """Required fields of *provider* that have no value."""
    return [f for f in fields_of(provider) if f.get("required") and not value(f, overrides)]


def clean(provider, updates):
    """*updates* as {key: stripped value}; ValueError for an unknown field or a bad value."""
    known = {f["key"]: f for f in fields_of(provider)}
    out = {}
    for key, raw in (updates or {}).items():
        field = known.get(key)
        if not field:
            names = ", ".join(known) or "none"
            raise ValueError(f"{provider} has no setting {key!r} (available: {names})")
        text = str(raw or "").strip()
        if text and not VALUE_RE.fullmatch(text):
            raise ValueError(f"{field['label']} may only contain letters, digits, '-' and '_'")
        out[key] = text
    return out


def save(provider, updates):
    """Persist field values; an empty value removes the saved one."""
    updates = clean(provider, updates)
    if not updates:
        return
    envs = {f["key"]: f["env"] for f in fields_of(provider)}
    with _lock:
        data = _load()
        for key, text in updates.items():
            if text:
                data[envs[key]] = text
            else:
                data.pop(envs[key], None)
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
