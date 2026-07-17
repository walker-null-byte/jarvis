"""Jarvis's memory on disk.

EpisodeLog — raw daily logs (mind/episodes/), written as things happen.
FactStore — long-term semantic memory (mind/facts/), one fact per file with a
strength score. Recalling a fact reinforces it; the dream cycle (phase 3) will
decay what's never recalled.
"""

import datetime
import re
from pathlib import Path


class EpisodeLog:
    def __init__(self, mind):
        self.dir = Path(mind) / "episodes"
        self.dir.mkdir(parents=True, exist_ok=True)

    def log(self, kind, text, max_chars=2000):
        text = str(text).strip()
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
        front = "\n".join(f"{k}: {v}" for k, v in meta.items())
        path.write_text(f"---\n{front}\n---\n{body}\n", encoding="utf-8")

    # ---------- operations ----------

    def remember(self, fact, why="", name=""):
        """Save a fact; saving again under the same name corrects/reinforces it."""
        name = _slugify(name or fact)
        path = self.dir / f"{name}.md"
        old = self._read(path)["meta"] if path.exists() else {}

        meta = {
            "created": old.get("created", _today()),
            "strength": round(float(old.get("strength", 0.5)) + 0.5, 2),
            "recalls": int(old.get("recalls", 0)),
            "last_recalled": old.get("last_recalled", ""),
        }
        body = fact.strip()
        if why.strip():
            body += f"\n\nWhy: {why.strip()}"
        self._write(path, meta, body)
        self.rebuild_index()
        return {"saved": name, "updated": bool(old), "strength": meta["strength"]}

    def recall(self, query, top_n=3):
        """Keyword-score facts against the query; recalling reinforces them."""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return {"found": 0, "note": "Empty query."}

        scored = []
        for path in self.dir.glob("*.md"):
            data = self._read(path)
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
            })
        if not results:
            return {"found": 0, "note": "No matching memories."}
        return results

    def forget(self, name):
        path = self.dir / f"{name}.md"
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
            lines.append(f"- **{path.stem}** (strength {strength}) — {first_line}")
        lines.append("")
        lines.append("(Full memories live in facts/ — fetch one with the recall tool.)")
        self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
