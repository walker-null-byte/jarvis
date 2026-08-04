"""Jarvis's memory on disk.

EpisodeLog — raw daily logs (mind/episodes/), written as things happen.
FactStore — long-term semantic memory (mind/facts/), one fact per file with a
strength score. Recalling a fact reinforces it; the dream cycle (phase 3) will
decay what's never recalled.
"""

import datetime
import json
import re
from pathlib import Path

from .redaction import redact_text


class EpisodeLog:
    def __init__(self, mind):
        self.dir = Path(mind) / "episodes"
        self.dir.mkdir(parents=True, exist_ok=True)

    def log(self, kind, text, max_chars=2000):
        text = redact_text(text).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n[... truncated {len(text) - max_chars} chars]"
        now = datetime.datetime.now()
        path = self.dir / f"{now:%Y-%m-%d}.md"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## {now:%H:%M:%S} — {kind}\n{text}\n")


def _slugify(text, max_words=6):
    words = re.findall(r"[a-z0-9]+", text.lower())[:max_words]
    return "-".join(words) or "memory"


def _tokenize(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _today():
    return f"{datetime.date.today():%Y-%m-%d}"


class FactStore:
    """One fact per file in mind/facts/, with simple frontmatter:

    ---
    created: 2026-06-13
    strength: 1.0
    recalls: 0
    last_recalled:
    ---
    The fact itself, stated plainly.

    Why: how it was learned / why it matters.
    """

    def __init__(self, mind):
        self.dir = Path(mind) / "facts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(mind) / "MEMORY.md"
        self.rules_path = Path(mind) / "memory_rules.json"

    # ---------- file format ----------

    @staticmethod
    def _read(path):
        text = path.read_text(encoding="utf-8")
        meta, body = {}, text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        meta[key.strip()] = value.strip()
                body = parts[2].strip()
        return {"meta": meta, "body": body}

    @staticmethod
    def _write(path, meta, body):
        """Atomically write one fact so a partial process cannot corrupt it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_meta = {}
        for key, value in meta.items():
            value = "" if value is None else str(value)
            safe_meta[key] = value.replace("\n", " ").replace("\r", " ")
        front = "\n".join(f"{k}: {v}" for k, v in safe_meta.items())
        content = f"---\n{front}\n---\n{body}\n"
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _write_text_atomic(path, content):
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _number(value, default, minimum=0.0, maximum=1.0):
        try:
            return round(max(minimum, min(maximum, float(value))), 3)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _meta_value(value, default="", max_chars=240):
        value = redact_text(str(value or "")).strip()
        return value.replace("\n", " ").replace("\r", " ")[:max_chars] or default

    # ---------- operations ----------

    def remember(
        self,
        fact,
        why="",
        name="",
        source=None,
        source_episode="",
        source_event_id="",
        confidence=None,
        category=None,
        observed_at="",
        valid_from="",
        valid_until="",
        correction_of="",
        provenance="",
        review_status=None,
        review_note="",
    ):
        """Save a fact with reviewable source and validity metadata."""
        name = _slugify(str(name or fact))
        blocked = self._blocked_rule(fact, why, name)
        if blocked:
            return {
                "blocked": True,
                "reason": "Never-remember rule matched",
                "rule": blocked,
            }
        path = self.dir / f"{name}.md"
        old = self._read(path)["meta"] if path.exists() else {}
        effective_source = source or old.get("source") or "direct"
        default_review = old.get("review_status")
        if not default_review or effective_source != "consolidation":
            default_review = "pending" if effective_source == "consolidation" else "approved"

        meta = {
            "created": old.get("created", _today()),
            "strength": round(float(old.get("strength", 0.5)) + 0.5, 2),
            "recalls": int(old.get("recalls", 0)),
            "last_recalled": old.get("last_recalled", ""),
            "source": self._meta_value(effective_source, "direct"),
            "source_episode": self._meta_value(source_episode or old.get("source_episode")),
            "source_event_id": self._meta_value(source_event_id or old.get("source_event_id")),
            "confidence": self._number(
                confidence if confidence is not None else old.get("confidence", 0.8),
                0.8,
            ),
            "category": self._meta_value(category or old.get("category"), "general", 80),
            "observed_at": self._meta_value(observed_at or old.get("observed_at")),
            "valid_from": self._meta_value(valid_from or old.get("valid_from")),
            "valid_until": self._meta_value(valid_until or old.get("valid_until")),
            "correction_of": self._meta_value(correction_of or old.get("correction_of")),
            "provenance": self._meta_value(provenance or old.get("provenance")),
            "review_status": self._meta_value(review_status or default_review, "approved", 40),
            "review_note": self._meta_value(review_note or old.get("review_note")),
        }
        body = redact_text(str(fact or "")).strip()
        if not body:
            raise ValueError("Fact cannot be empty")
        if why.strip():
            body += f"\n\nWhy: {redact_text(why).strip()}"
        self._write(path, meta, body)
        self.rebuild_index()
        return {
            "saved": name,
            "updated": bool(old),
            "strength": meta["strength"],
            "confidence": meta["confidence"],
            "category": meta["category"],
            "source": meta["source"],
        }

    def recall(self, query, top_n=3):
        """Keyword-score facts against the query; recalling reinforces them."""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return {"found": 0, "note": "Empty query."}

        scored = []
        for path in self.dir.glob("*.md"):
            data = self._read(path)
            if self._is_expired(data["meta"]):
                continue
            tokens = _tokenize(path.stem + " " + data["body"])
            exact = len(query_tokens & tokens)
            prefix = sum(
                0.5 for q in query_tokens if len(q) >= 3
                and any(t.startswith(q) and t != q for t in tokens)
            )
            if exact + prefix > 0:
                scored.append((exact + prefix, path, data))

        scored.sort(key=lambda item: -item[0])
        results = []
        for _, path, data in scored[:top_n]:
            meta = data["meta"]
            meta["recalls"] = int(meta.get("recalls", 0)) + 1
            meta["strength"] = round(float(meta.get("strength", 1.0)) + 0.25, 2)
            meta["last_recalled"] = _today()
            self._write(path, meta, data["body"])
            results.append({
                "name": path.stem,
                "memory": data["body"],
                "strength": meta["strength"],
                "confidence": self._number(meta.get("confidence", 0.8), 0.8),
                "category": meta.get("category", "general"),
                "source": meta.get("source", "unknown"),
                "source_episode": meta.get("source_episode", ""),
                "observed_at": meta.get("observed_at", ""),
                "valid_from": meta.get("valid_from", ""),
                "valid_until": meta.get("valid_until", ""),
                "correction_of": meta.get("correction_of", ""),
                "review_status": meta.get("review_status", "approved"),
                "expired": self._is_expired(meta),
            })
        if not results:
            return {"found": 0, "note": "No matching memories."}
        self.rebuild_index()
        return results

    def review_memories(self, limit=50, include_approved=False):
        """Return memory records needing review, including expiry state."""
        items = []
        for path in sorted(self.dir.glob("*.md")):
            data = self._read(path)
            meta = data["meta"]
            if not include_approved and meta.get("review_status", "approved") != "pending":
                continue
            items.append({
                "name": path.stem,
                "memory": data["body"],
                "status": meta.get("review_status", "approved"),
                "expired": self._is_expired(meta),
                "confidence": self._number(meta.get("confidence", 0.8), 0.8),
                "category": meta.get("category", "general"),
                "source": meta.get("source", "unknown"),
                "source_episode": meta.get("source_episode", ""),
                "valid_until": meta.get("valid_until", ""),
                "provenance": meta.get("provenance", ""),
            })
            if limit and len(items) >= limit:
                break
        return {"count": len(items), "memories": items}

    def set_review(self, name, decision, note=""):
        """Approve, reject, or expire a memory without destroying its history."""
        path = self._path_for_name(name)
        if not path or not path.exists():
            return {"error": f"No memory named '{name}'."}
        decision = (decision or "").strip().lower()
        aliases = {"keep": "approved", "approve": "approved", "reject": "rejected", "expire": "expired"}
        status = aliases.get(decision)
        if not status:
            return {"error": "Decision must be approve, reject, or expire."}
        data = self._read(path)
        meta = data["meta"]
        meta["review_status"] = status
        meta["reviewed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        if note:
            meta["review_note"] = self._meta_value(note)
        if status in {"rejected", "expired"}:
            meta["valid_until"] = _today()
        self._write(path, meta, data["body"])
        self.rebuild_index()
        return {"reviewed": name, "status": status, "valid_until": meta.get("valid_until", "")}

    def expire(self, name, valid_until):
        """Set a memory's validity end date without deleting its source."""
        path = self._path_for_name(name)
        if not path or not path.exists():
            return {"error": f"No memory named '{name}'."}
        try:
            expiry = datetime.date.fromisoformat(valid_until)
        except (TypeError, ValueError):
            return {"error": "valid_until must be an ISO date (YYYY-MM-DD)."}
        data = self._read(path)
        data["meta"]["valid_until"] = expiry.isoformat()
        if expiry <= datetime.date.today():
            data["meta"]["review_status"] = "expired"
        self._write(path, data["meta"], data["body"])
        self.rebuild_index()
        return {"expired": name, "valid_until": expiry.isoformat()}

    def memory_rules(self):
        return self._read_rules()

    def add_never_remember(self, pattern):
        pattern = self._meta_value(pattern, max_chars=200).casefold()
        if len(pattern) < 2:
            return {"error": "Never-remember pattern must contain at least 2 characters."}
        rules = self._read_rules()
        if pattern not in rules["never_remember"]:
            rules["never_remember"].append(pattern)
            self._write_rules(rules)
        return {"added": pattern, "rules": rules}

    def remove_never_remember(self, pattern):
        pattern = self._meta_value(pattern, max_chars=200).casefold()
        rules = self._read_rules()
        if pattern not in rules["never_remember"]:
            return {"error": f"Never-remember pattern not found: {pattern}"}
        rules["never_remember"].remove(pattern)
        self._write_rules(rules)
        return {"removed": pattern, "rules": rules}

    def forget(self, name):
        path = self._path_for_name(name)
        if not path:
            return {"error": f"Invalid memory id '{name}'."}
        if not path.exists():
            return {"error": f"No memory named '{name}'."}
        path.unlink()
        self.rebuild_index()
        return {"forgotten": name}

    def rebuild_index(self):
        """Regenerate MEMORY.md — the always-in-context index of what's known."""
        entries = sorted(self.dir.glob("*.md"))
        lines = ["# Memory index"]
        if not entries:
            lines.append("(No long-term memories yet.)")
        for path in entries:
            data = self._read(path)
            first_line = data["body"].splitlines()[0][:100] if data["body"] else ""
            strength = data["meta"].get("strength", "?")
            category = data["meta"].get("category", "general")
            confidence = data["meta"].get("confidence", "?")
            review_status = data["meta"].get("review_status", "approved")
            lines.append(
                f"- **{path.stem}** ({category}, confidence {confidence}, {review_status}, strength {strength}) — {first_line}"
            )
        lines.append("")
        lines.append("(Full memories live in facts/ — fetch one with the recall tool.)")
        self._write_text_atomic(self.index_path, "\n".join(lines) + "\n")

    def _path_for_name(self, name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", name or ""):
            return None
        return self.dir / f"{name}.md"

    def _is_expired(self, meta):
        if meta.get("review_status") in {"rejected", "expired"}:
            return True
        value = meta.get("valid_until", "")
        if not value:
            return False
        try:
            return datetime.date.fromisoformat(value) <= datetime.date.today()
        except ValueError:
            return False

    def _read_rules(self):
        if not self.rules_path.exists():
            return {"never_remember": []}
        try:
            data = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"never_remember": []}
        patterns = data.get("never_remember", []) if isinstance(data, dict) else []
        return {"never_remember": [str(item).casefold() for item in patterns if str(item).strip()]}

    def _write_rules(self, rules):
        self._write_text_atomic(
            self.rules_path,
            json.dumps(rules, ensure_ascii=False, indent=2) + "\n",
        )

    def _blocked_rule(self, fact, why, name):
        haystack = " ".join((str(fact or ""), str(why or ""), str(name or ""))).casefold()
        for pattern in self._read_rules()["never_remember"]:
            if pattern in haystack:
                return pattern
        return ""
