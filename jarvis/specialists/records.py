"""Records: the structured tables an agent fills (one JSON file per table).

    <data_dir>/records/<table>.json = {"schema": 1, "next_id": 4, "records": [...]}

Every write is atomic (temp file + rename) under a per-file lock. Values are
validated and coerced against the table's field schema; `add` de-duplicates on
the table's unique key(s) and merges instead of creating a second copy.
Record ids are short numbers as strings ("1", "2", …): easy for small models.
"""

import datetime
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAX_TEXT = 20000
_locks = {}
_locks_guard = threading.Lock()
EMAIL_RE = re.compile(r"^[^@\s<>()\[\],;:\"]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
# "[Name]", "[Specific Detail]", "{first_name}": an unfilled template, never real data
PLACEHOLDER_RE = re.compile(
    r"[\[{]\s*(?:your |their |his |her |first |last |specific |company |studio |project )?"
    r"(?:name|first_name|detail|details|company|project|game|studio|link|role|title|topic)\s*[\]}]", re.I)


def _lock_for(path):
    with _locks_guard:
        return _locks.setdefault(str(path), threading.RLock())


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class FieldError(ValueError):
    pass


def _option(value, options):
    """Match a select value loosely: case, spaces and dashes don't matter."""
    norm = lambda s: re.sub(r"[\s\-]+", "_", str(s).strip().lower())  # noqa: E731
    wanted = norm(value)
    for option in options:
        if norm(option) == wanted:
            return option
    raise FieldError(f"'{value}' is not one of: {', '.join(options)}")


def coerce(field, value):
    """Validate and convert one value for `field`. Returns None for empty; raises FieldError."""
    kind = field["type"]
    if value is None or (isinstance(value, str) and not value.strip()) or value == []:
        return None
    if kind in ("text", "textarea"):
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value)
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        text = str(value).strip()
        placeholder = PLACEHOLDER_RE.search(text)
        if placeholder:
            raise FieldError(f"contains the placeholder {placeholder.group(0)}: write the real text "
                             "(look the record up first if you need its details)")
        return text[:MAX_TEXT]
    if kind == "number":
        if isinstance(value, bool):
            raise FieldError("must be a number")
        if isinstance(value, str):
            match = NUMBER_RE.search(value)
            if not match:
                raise FieldError(f"'{value}' is not a number")
            value = float(match.group(0).replace(",", "."))
        if not isinstance(value, (int, float)):
            raise FieldError("must be a number")
        if field.get("min") is not None and value < field["min"]:
            raise FieldError(f"must be at least {field['min']} (got {value:g})")
        if field.get("max") is not None and value > field["max"]:
            raise FieldError(f"must be at most {field['max']} (got {value:g})")
        return int(value) if float(value).is_integer() else round(float(value), 4)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "1", "y", "on"):
            return True
        if text in ("false", "no", "0", "n", "off"):
            return False
        raise FieldError(f"'{value}' is not true/false")
    if kind == "select":
        return _option(value, field["options"])
    if kind == "url":
        text = str(value).strip()
        if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
            if re.match(r"^[\w-]+(\.[\w-]+)+(/|$)", text):
                text = "https://" + text  # instagram.com/name -> https://instagram.com/name
            else:
                raise FieldError(f"'{text[:80]}' is not a web address")
        parts = urlsplit(text)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise FieldError(f"'{text[:80]}' is not an http(s) web address")
        return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))
    if kind == "email":
        text = str(value).strip()
        text = text[7:] if text.lower().startswith("mailto:") else text
        if not EMAIL_RE.match(text):
            raise FieldError(f"'{text[:80]}' is not an email address")
        return text.lower()
    if kind == "date":
        text = str(value).strip()[:10]
        try:
            return datetime.date.fromisoformat(text).isoformat()
        except ValueError:
            raise FieldError(f"'{value}' is not a date like 2026-01-31") from None
    return value


def dedupe_key(field, value):
    """Normalised form used to spot duplicates (URLs ignore www, scheme, slash, query)."""
    if value is None:
        return ""
    if field["type"] == "url":
        parts = urlsplit(str(value).lower())
        host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
        return host + parts.path.rstrip("/")
    return str(value).strip().lower()


class RecordTable:
    """One table of records for one agent."""

    def __init__(self, data_dir, spec, on_change=None):
        self.spec = spec
        self.name = spec["name"]
        self.fields = {f["name"]: f for f in spec["fields"]}
        self.path = Path(data_dir) / "records" / f"{self.name}.json"
        self.on_change = on_change
        self._lock = _lock_for(self.path)

    # ---------- storage ----------

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": 1, "next_id": 1, "records": []}
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{self.path.name} is unreadable ({error}); fix or delete it") from None
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise RuntimeError(f"{self.path.name} has an unexpected format")
        data.setdefault("next_id", 1 + max((int(r["id"]) for r in data["records"]
                                            if str(r.get("id", "")).isdigit()), default=0))
        return data

    def _save(self, data, change):
        atomic_write_json(self.path, data)
        if self.on_change:
            self.on_change(self.name, change)

    def all(self):
        with self._lock:
            return self._load()["records"]

    def count(self):
        return len(self.all())

    def get(self, record_id):
        record_id = str(record_id)
        return next((r for r in self.all() if r.get("id") == record_id), None)

    # ---------- validation ----------

    def clean(self, item, partial=False):
        """Coerce an item's values. Returns (values, warnings); raises FieldError for a bad required field."""
        if not isinstance(item, dict):
            raise FieldError("each record must be an object of field values")
        values, warnings = {}, []
        for key, value in item.items():
            if key in ("id", "created", "updated"):
                continue
            field = self.fields.get(key)
            if field is None:
                warnings.append(f"unknown field '{key}' ignored (fields: {', '.join(self.fields)})")
                continue
            try:
                values[key] = coerce(field, value)
            except FieldError as error:
                if field.get("required") or partial:  # an explicit update must not silently empty a field
                    raise FieldError(f"{key}: {error}") from None
                warnings.append(f"{key}: {error} (left empty)")
                values[key] = None
        if not partial:
            for name, field in self.fields.items():
                if values.get(name) is None and field.get("default") is not None:
                    values[name] = field["default"]
                if field.get("required") and values.get(name) is None:
                    raise FieldError(f"'{name}' is required")
        else:
            for name, field in self.fields.items():
                if name in values and values[name] is None and field.get("required"):
                    raise FieldError(f"'{name}' is required and cannot be emptied")
        return values, warnings

    def _key(self, values):
        unique = self.spec.get("unique") or []
        parts = tuple(dedupe_key(self.fields[u], values.get(u)) for u in unique)
        return parts if unique and all(parts) else None

    # ---------- operations ----------

    def add(self, items):
        """Add records, merging duplicates. Returns counts plus ids and per-item problems."""
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise FieldError("items must be a list of records")
        result = {"added": 0, "updated": 0, "skipped": 0, "ids": [], "problems": []}
        with self._lock:
            data = self._load()
            index = {self._key(r): r for r in data["records"] if self._key(r)}
            changed = False
            for n, item in enumerate(items):
                try:
                    values, warnings = self.clean(item)
                except FieldError as error:
                    result["skipped"] += 1
                    result["problems"].append({"item": n, "error": str(error)})
                    continue
                for warning in warnings:
                    result["problems"].append({"item": n, "warning": warning})
                key = self._key(values)
                existing = index.get(key) if key else None
                if existing is None:
                    record = {"id": str(data["next_id"]), **{k: v for k, v in values.items() if v is not None},
                              "created": _now(), "updated": _now()}
                    data["next_id"] += 1
                    data["records"].append(record)
                    if key:
                        index[key] = record
                    result["added"] += 1
                    result["ids"].append(record["id"])
                    changed = True
                elif self._merge(existing, values):
                    result["updated"] += 1
                    result["ids"].append(existing["id"])
                    changed = True
                else:
                    result["skipped"] += 1
                    result["ids"].append(existing["id"])
                    result["problems"].append({"item": n, "note": f"duplicate of record {existing['id']}, nothing new"})
            if changed:
                self._save(data, "add")
        return result

    def _merge(self, record, values):
        """Fill in new information. Editable fields (the user's, e.g. status) are only filled when empty."""
        changed = False
        unique = set(self.spec.get("unique") or [])
        for key, value in values.items():
            if value is None or record.get(key) == value or (key in unique and record.get(key)):
                continue
            field = self.fields[key]
            if field.get("editable") and record.get(key) not in (None, ""):
                continue
            record[key] = value
            changed = True
        if changed:
            record["updated"] = _now()
        return changed

    def update(self, record_id, fields):
        """Change some fields of one record. Returns the record; KeyError if missing."""
        values, warnings = self.clean(fields, partial=True)
        with self._lock:
            data = self._load()
            record = next((r for r in data["records"] if r.get("id") == str(record_id)), None)
            if record is None:
                raise KeyError(f"no record with id {record_id}")
            for key, value in values.items():
                if value is None:
                    record.pop(key, None)
                else:
                    record[key] = value
            record["updated"] = _now()
            self._save(data, "update")
        return {**record, **({"warnings": warnings} if warnings else {})}

    def delete(self, record_id):
        with self._lock:
            data = self._load()
            kept = [r for r in data["records"] if r.get("id") != str(record_id)]
            if len(kept) == len(data["records"]):
                return False
            data["records"] = kept
            self._save(data, "delete")
        return True

    def replace_all(self, records):
        """Restore a table (import with data). Records are re-validated loosely."""
        with self._lock:
            clean = []
            for record in records:
                if isinstance(record, dict) and record.get("id"):
                    clean.append({k: v for k, v in record.items() if k in self.fields or k in ("id", "created", "updated")})
            next_id = 1 + max((int(r["id"]) for r in clean if str(r["id"]).isdigit()), default=0)
            self._save({"schema": 1, "next_id": next_id, "records": clean}, "import")

    def query(self, where=None, search="", limit=50, offset=0, fields=None):
        """Records matching `where` ({field: value}; "" or null = empty; a list = any of)."""
        records = self.all()
        where = where or {}
        out = []
        for record in records:
            if not _where_ok(record, where, self.fields):
                continue
            if search and search.lower() not in json.dumps(record, ensure_ascii=False).lower():
                continue
            out.append(record)
        total = len(out)
        out = out[offset:offset + limit] if limit else out[offset:]
        if fields:
            keep = set(fields) | {"id"}
            out = [{k: v for k, v in r.items() if k in keep} for r in out]
        return {"total": total, "records": out}


def _normal(field, value):
    try:
        value = coerce(field, value)
    except FieldError:
        pass
    return "" if value is None else dedupe_key(field, value) if field["type"] != "number" else str(value)


def _where_ok(record, where, fields):
    for key, wanted in where.items():
        value = record.get(key)
        if isinstance(wanted, str) and wanted.strip().strip("\"'").lower() in ("", "null", "none", "empty"):
            wanted = None  # small models write "\"\"" or "null" for "empty"
        if wanted is None or wanted == "" or wanted == []:
            if value not in (None, ""):
                return False
            continue
        field = fields.get(key, {"type": "text"})
        options = wanted if isinstance(wanted, list) else [wanted]
        if _normal(field, value) not in [_normal(field, o) for o in options]:
            return False
    return True
