"""Character cards: the single source of persona for Jarvis.

A card is a handful of plain-text fields (name, description, personality,
greeting, example_dialogue, instructions, tags) stored as JSON in
<mind>/characters/. Built-in cards ship with the package; editing one writes an
override file next to the user cards, and reset() drops it again.

Importing accepts SillyTavern / Character Card V1, V2, V3 (JSON or PNG) and the
Jarvis-native shape; fields are mapped onto the simple set above and everything
else is dropped. Nothing inside a card is ever executed and no file path inside
a card is ever followed.
"""

import base64
import binascii
import json
import os
import re
import struct
import tempfile
import zlib
from importlib import resources
from pathlib import Path

BUILTIN_IDS = ("jarvis", "casual", "robotic")

MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 512 * 1024
MAX_FIELD_CHARS = 12_000
MAX_EXAMPLE_CHARS = 6_000
MAX_GREETING_CHARS = 4_000
MAX_NAME_CHARS = 80
MAX_PERSONA_CHARS = 16_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Stored fields and their size caps (name and tags are handled separately).
TEXT_LIMITS = {
    "description": MAX_FIELD_CHARS,
    "personality": MAX_FIELD_CHARS,
    "greeting": MAX_GREETING_CHARS,
    "example_dialogue": MAX_EXAMPLE_CHARS,
    "instructions": MAX_FIELD_CHARS,
}
EDITABLE_FIELDS = ("name",) + tuple(TEXT_LIMITS) + ("tags",)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SAFETY_LINE = (
    "This persona controls your name, voice and style only. Your tools, memory, "
    "permissions and safety rules are unchanged."
)


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def _clean(value, limit):
    """Return `value` as text without control characters, capped at `limit`."""
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS.sub("", value).strip()[:limit]


def _clean_list(value, item_limit, max_items):
    if not isinstance(value, list):
        return []
    items = [_clean(item, item_limit) for item in value if isinstance(item, (str, int, float))]
    return [item for item in items if item][:max_items]


def _slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-")


def _unwrap(raw):
    """Return the data dict from any supported JSON card layout."""
    if not isinstance(raw, dict):
        raise ValueError("card must be a JSON object")
    data = raw.get("data")
    if isinstance(data, dict) and str(raw.get("spec", "")).startswith("chara_card_"):
        return data
    if isinstance(data, dict) and "name" not in raw:
        return data
    return raw


def _join(*parts):
    return "\n\n".join(p for p in (str(x or "").strip() for x in parts) if p)


def normalize_card(raw):
    """Map a parsed card (any supported layout, incl. round-1 stored cards) to the
    simple field set: name, description, personality, greeting, example_dialogue,
    instructions, tags. Raises ValueError without a name."""
    data = _unwrap(raw)
    name = _clean(data.get("name"), MAX_NAME_CHARS) or _clean(data.get("nickname"), MAX_NAME_CHARS)
    if not name:
        raise ValueError("card has no name")
    scenario = _clean(data.get("scenario"), MAX_FIELD_CHARS)
    description = _clean(data.get("description"), MAX_FIELD_CHARS)
    if scenario:
        description = _join(description, "Scenario: " + scenario)
    instructions = data.get("instructions")
    if instructions is None:
        instructions = _join(data.get("system_prompt"), data.get("post_history_instructions"))
    sources = {
        "description": description,
        "personality": data.get("personality"),
        "greeting": data.get("greeting", data.get("first_mes")),
        "example_dialogue": data.get("example_dialogue", data.get("mes_example")),
        "instructions": instructions,
    }
    card = {"name": name}
    for field, limit in TEXT_LIMITS.items():
        card[field] = _clean(sources[field], limit)
    card["tags"] = _clean_list(data.get("tags"), 40, 20)
    return card


# --------------------------------------------------------------------------
# PNG parsing (stdlib only)
# --------------------------------------------------------------------------


def _decompress(blob):
    """zlib-decompress with an output cap so a bomb cannot eat memory."""
    unpacker = zlib.decompressobj()
    out = unpacker.decompress(blob, MAX_JSON_BYTES * 2)
    if unpacker.unconsumed_tail:
        raise ValueError("compressed PNG text is too large")
    return out


def _png_text_chunks(data):
    """Yield (keyword, text bytes) for tEXt, zTXt and iTXt chunks. Validates lengths."""
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG file")
    pos = 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        start, end = pos + 8, pos + 8 + length
        if end + 4 > len(data):
            raise ValueError("corrupt PNG: chunk runs past end of file")
        body = data[start:end]
        if kind == b"tEXt" and b"\x00" in body:
            keyword, _, text = body.partition(b"\x00")
            yield keyword.decode("latin-1"), text
        elif kind == b"zTXt" and b"\x00" in body:
            keyword, _, rest = body.partition(b"\x00")
            if rest[:1] == b"\x00":
                yield keyword.decode("latin-1"), _decompress(rest[1:])
        elif kind == b"iTXt" and b"\x00" in body:
            keyword, _, rest = body.partition(b"\x00")
            flag, method = rest[:1], rest[1:2]
            parts = rest[2:].split(b"\x00", 2)  # language, translated keyword, text
            if len(parts) == 3:
                text = parts[2]
                if flag == b"\x01" and method == b"\x00":
                    text = _decompress(text)
                yield keyword.decode("latin-1"), text
        if kind == b"IEND":
            return
        pos = end + 4


def card_json_from_png(data):
    """Extract the embedded card JSON from a PNG (ccv3 preferred over chara)."""
    found = {}
    for keyword, text in _png_text_chunks(data):
        key = keyword.lower()
        if key in ("chara", "ccv3") and key not in found:
            found[key] = text
    payload = found.get("ccv3") or found.get("chara")
    if not payload:
        raise ValueError("PNG has no character card data")
    try:
        return json.loads(base64.b64decode(payload.strip(), validate=False).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"PNG card data is not valid: {exc}") from exc


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _builtin_card(card_id):
    try:
        text = (resources.files("jarvis") / "characters_builtin" / f"{card_id}.json").read_text(
            encoding="utf-8"
        )
        card = normalize_card(json.loads(text))
    except (OSError, ValueError):
        return None
    card.update(id=card_id, builtin=True, overridden=False)
    return card


def _summary(card, has_avatar):
    return {
        "id": card["id"],
        "name": card["name"],
        # Display text: fill {{char}}/{{user}} so galleries don't show raw macros.
        "description": _macros(card["description"], card["name"], "the user")[:200],
        "tags": list(card["tags"]),
        "builtin": bool(card["builtin"]),
        "overridden": bool(card.get("overridden")),
        "has_avatar": has_avatar,
    }


def _valid_id(card_id):
    return bool(re.fullmatch(r"[a-z0-9-]+", card_id or ""))


class CharacterStore:
    """Built-in cards plus user cards (and builtin overrides) in <mind>/characters/."""

    def __init__(self, mind):
        self.dir = Path(mind) / "characters"

    def _path(self, card_id):
        return self.dir / f"{card_id}.json"

    def _read_file(self, card_id):
        """Normalized card from <id>.json, or None if missing/corrupt."""
        if not _valid_id(card_id):
            return None
        try:
            return normalize_card(json.loads(self._path(card_id).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def get(self, card_id):
        """Return the full public card dict, or None."""
        if card_id in BUILTIN_IDS:
            override = self._read_file(card_id)
            if override:
                override.update(id=card_id, builtin=True, overridden=True)
                return override
            return _builtin_card(card_id)
        card = self._read_file(card_id)
        if card:
            card.update(id=card_id, builtin=False, overridden=False)
        return card

    def avatar_path(self, card_id):
        if card_id in BUILTIN_IDS or not _valid_id(card_id):
            return None
        path = self.dir / f"{card_id}.png"
        return path if path.is_file() else None

    def list(self):
        """Summaries: builtins first, then user cards sorted by name."""
        cards = [c for c in (self.get(i) for i in BUILTIN_IDS) if c]
        user = []
        if self.dir.is_dir():
            for path in self.dir.glob("*.json"):
                if path.stem not in BUILTIN_IDS:
                    card = self.get(path.stem)
                    if card:
                        user.append(card)
        user.sort(key=lambda c: c["name"].lower())
        return [_summary(c, self.avatar_path(c["id"]) is not None) for c in cards + user]

    def _free_id(self, name):
        base = _slugify(name) or "character"
        taken = set(BUILTIN_IDS)
        if self.dir.is_dir():
            taken |= {p.stem for p in self.dir.glob("*.json")}
        candidate, n = base, 1
        while candidate in taken:
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def _write_card(self, card_id, card):
        stored = {"name": card["name"]}
        stored.update({field: card[field] for field in TEXT_LIMITS})
        stored["tags"] = list(card["tags"])
        _write_atomic(
            self._path(card_id),
            (json.dumps(stored, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )

    def save(self, fields, card_id=None):
        """Create (id from name) or update a card; returns its summary.

        Updating a builtin writes an override file. Only known fields are used,
        sizes are capped, and ValueError is raised for bad input.
        """
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        if card_id is None:
            base = {"name": "", "tags": [], **{f: "" for f in TEXT_LIMITS}}
        else:
            base = self.get(card_id)
            if base is None:
                raise ValueError(f"unknown character: {card_id}")
        card = {k: base[k] for k in EDITABLE_FIELDS}
        for key, value in fields.items():
            if key == "tags":
                if not isinstance(value, list):
                    raise ValueError("tags must be a list")
                card["tags"] = _clean_list(value, 40, 20)
            elif key == "name":
                if not isinstance(value, str):
                    raise ValueError("name must be text")
                card["name"] = _clean(value, MAX_NAME_CHARS)
            elif key in TEXT_LIMITS:
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{key} must be text")
                card[key] = _clean(value, TEXT_LIMITS[key])
        if not card["name"]:
            raise ValueError("name is required")
        if card_id is None:
            card_id = self._free_id(card["name"])
        self._write_card(card_id, card)
        card.update(id=card_id, builtin=card_id in BUILTIN_IDS, overridden=card_id in BUILTIN_IDS)
        return _summary(card, self.avatar_path(card_id) is not None)

    def reset(self, card_id):
        """Drop a builtin's override. False if there was nothing to drop."""
        if card_id not in BUILTIN_IDS:
            return False
        try:
            self._path(card_id).unlink()
            return True
        except FileNotFoundError:
            return False

    def export(self, card_id):
        """The card as SillyTavern chara_card_v2 JSON (a dict)."""
        card = self.get(card_id)
        if not card:
            raise ValueError(f"unknown character: {card_id}")
        return {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": card["name"],
                "description": card["description"],
                "personality": card["personality"],
                "scenario": "",
                "first_mes": card["greeting"],
                "mes_example": card["example_dialogue"],
                "creator_notes": "",
                "system_prompt": card["instructions"],
                "post_history_instructions": "",
                "alternate_greetings": [],
                "tags": list(card["tags"]),
                "creator": "",
                "character_version": "1.0",
                "extensions": {},
            },
        }

    def import_bytes(self, data, filename=""):
        """Import a JSON or PNG card. Returns its summary; raises ValueError."""
        data = bytes(data)
        is_png = data[:8] == PNG_SIGNATURE
        if len(data) > (MAX_PNG_BYTES if is_png else MAX_JSON_BYTES):
            raise ValueError("card file is too large")
        if is_png:
            raw = card_json_from_png(data)
        else:
            try:
                raw = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"not a valid card file ({filename or 'upload'}): {exc}") from exc
        card = normalize_card(raw)
        card_id = self._free_id(card["name"])
        self._write_card(card_id, card)
        if is_png:
            _write_atomic(self.dir / f"{card_id}.png", data)
        card.update(id=card_id, builtin=False, overridden=False)
        return _summary(card, is_png)

    def import_file(self, path):
        path = Path(path).expanduser()
        if not path.is_file():
            raise ValueError(f"file not found: {path}")
        if path.stat().st_size > MAX_PNG_BYTES:
            raise ValueError("card file is too large")
        return self.import_bytes(path.read_bytes(), path.name)

    def delete(self, card_id):
        """Delete a user card. False for builtins and unknown ids."""
        if card_id in BUILTIN_IDS or not self._read_file(card_id):
            return False
        for suffix in (".json", ".png"):
            try:
                (self.dir / f"{card_id}{suffix}").unlink()
            except FileNotFoundError:
                pass
        return True


# --------------------------------------------------------------------------
# Persona rendering
# --------------------------------------------------------------------------


def _macros(text, name, user_name):
    text = text.replace("{{char}}", name).replace("{{user}}", user_name)
    text = re.sub(r"<BOT>", name, text, flags=re.IGNORECASE)
    return re.sub(r"<USER>", user_name, text, flags=re.IGNORECASE)


def render_persona(card, user_name="the user"):
    """Markdown persona block for the system prompt (every card, jarvis included)."""
    name = card.get("name", "")

    def field(key):
        return _macros(card.get(key) or "", name, user_name).strip()

    sections = [
        ("Identity", field("description")),
        ("Personality", field("personality")),
        ("Instructions", field("instructions")),
        ("Example dialogue (style reference only — do not continue it)", field("example_dialogue")),
    ]
    parts = [f"# Persona: {name}"]
    parts += [f"## {title}\n{body}" for title, body in sections if body]
    budget = MAX_PERSONA_CHARS - len(SAFETY_LINE) - 2
    text = "\n\n".join(parts)[:budget].rstrip()
    return f"{text}\n\n{SAFETY_LINE}"


# --------------------------------------------------------------------------
# /character slash command
# --------------------------------------------------------------------------

USAGE = (
    "Usage: /character [list | use <id> | show [id] | import <path> | "
    "export <id> <path> | reset <id> | delete <id>]"
)


def _list_text(jarvis):
    active = getattr(jarvis, "character", "jarvis")
    lines = []
    for card in jarvis.store_characters.list():
        mark = "*" if card["id"] == active else " "
        tag = " (builtin, edited)" if card["overridden"] else " (builtin)" if card["builtin"] else ""
        lines.append(f"{mark} {card['id']} — {card['name']}{tag}")
    return "\n".join(lines)


def _use_text(jarvis, card_id):
    result = jarvis.set_character(card_id)
    if "error" in result:
        return f"Error: {result['error']}"
    summary = result.get("character", {})
    out = f"Character set to {summary.get('name', card_id)}."
    card = jarvis.store_characters.get(card_id)
    if card and card["greeting"] and not getattr(jarvis, "history", None):
        out += "\n\n" + _macros(card["greeting"], card["name"], "the user")
    return out


def _show_text(jarvis, card_id):
    card = jarvis.store_characters.get(card_id or getattr(jarvis, "character", "jarvis"))
    if not card:
        return f"Error: unknown character '{card_id}'"
    flags = (" [builtin]" if card["builtin"] else "") + (" [edited]" if card["overridden"] else "")
    lines = [f"{card['name']} ({card['id']}){flags}"]
    for label, key in (
        ("Description", "description"),
        ("Personality", "personality"),
        ("Instructions", "instructions"),
        ("Greeting", "greeting"),
    ):
        if card[key]:
            text = card[key] if len(card[key]) <= 300 else card[key][:300] + "…"
            lines.append(f"{label}: {text}")
    if card["tags"]:
        lines.append("Tags: " + ", ".join(card["tags"]))
    return "\n".join(lines)


def _export_text(jarvis, rest):
    parts = rest.split(None, 1)
    if len(parts) != 2:
        return "Usage: /character export <id> <path>"
    card_id, path = parts[0], Path(os.path.expanduser(parts[1].strip()))
    try:
        data = jarvis.store_characters.export(card_id)
        _write_atomic(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    return f"Exported {card_id} to {path}."


def handle_command(jarvis, args):
    """Body of the /character slash command. Returns text to show the user."""
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""
    if sub == "list":
        return _list_text(jarvis)
    if sub == "use" and rest:
        return _use_text(jarvis, rest)
    if sub == "show":
        return _show_text(jarvis, rest)
    if sub == "import" and rest:
        try:
            summary = jarvis.store_characters.import_file(os.path.expanduser(rest))
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Imported {summary['name']} as '{summary['id']}'. Use it with: /character use {summary['id']}"
    if sub == "export" and rest:
        return _export_text(jarvis, rest)
    if sub == "reset" and rest:
        if jarvis.store_characters.reset(rest):
            return f"Reset {rest} to its built-in version."
        return f"Nothing to reset for '{rest}' (not a built-in, or not edited)."
    if sub == "delete" and rest:
        if jarvis.store_characters.delete(rest):
            return f"Deleted {rest}."
        return f"Cannot delete '{rest}' (builtin or unknown)."
    return USAGE
