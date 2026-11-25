"""Coding tools: read_file (line ranges), edit_file, grep, glob, plus output spilling.

`build_file_tools(policy)` returns plain functions closed over a CapabilityPolicy,
so every path goes through the same allowed-roots guard as write_file.
"""

import fnmatch
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

READ_CHAR_CAP = 20000      # one read_file call never returns more than this many characters
LINE_CAP = 2000            # longer lines are cut
GREP_TIMEOUT = 30
GLOB_CAP = 500
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}

SPILL_THRESHOLD = 8000     # tool results longer than this are saved to a file
SPILL_HEAD = 5000
SPILL_TAIL = 2000
SPILL_KEEP_DAYS = 7


def _is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return False


def _numbered(lines, start):
    return "\n".join(f"{start + i:>6}→{line[:LINE_CAP]}" for i, line in enumerate(lines))


def build_file_tools(policy):
    """The four file tools, bound to `policy` (used by tools_core.build_registry)."""

    def read_file(path: str, offset: int = 1, limit: int = 2000):
        """Read a text file with line numbers. Use offset/limit to page through big files.

        Args:
            path (str): File to read
            offset (int): First line to return (1-based)
            limit (int): Maximum number of lines to return
        """
        error, p = policy.check_path(path)
        if error:
            return {"error": error, "blocked": True}
        if not p.is_file():
            return {"error": f"Not a file: {path}"}
        if _is_binary(p):
            return {"error": f"{path} looks like a binary file; not reading it"}
        try:
            offset, limit = max(1, int(offset)), max(1, int(limit))
            text = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as e:
            return {"error": str(e)}
        lines = text.splitlines()
        total = len(lines)
        chunk = lines[offset - 1: offset - 1 + limit]
        body = _numbered(chunk, offset)
        shown = len(chunk)
        if len(body) > READ_CHAR_CAP:  # keep whole lines: cut at the last newline that fits
            body = body[:READ_CHAR_CAP].rsplit("\n", 1)[0]
            shown = body.count("\n") + 1 if body else 0
        truncated = offset - 1 + shown < total
        result = {"path": str(p), "total_lines": total, "truncated": truncated, "content": body}
        if truncated:
            result["next_offset"] = offset + shown
        return result

    def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False):
        """Replace an exact string in a file. Fails if old_string is missing or ambiguous.

        Args:
            path (str): File to edit
            old_string (str): Exact text to replace, including whitespace; include enough context to be unique
            new_string (str): Replacement text
            replace_all (bool): Replace every occurrence instead of requiring exactly one
        """
        error, p = policy.check_path(path)
        if error:
            return {"error": error, "blocked": True}
        if not p.is_file():
            return {"error": f"Not a file: {path} (use write_file to create it)"}
        if not old_string:
            return {"error": "old_string is empty; use write_file to create or overwrite a file"}
        if old_string == new_string:
            return {"error": "old_string and new_string are identical; nothing to change"}
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"error": f"Cannot read {path}: {e}"}
        count = text.count(old_string)
        if count == 0:
            first = old_string.strip().splitlines()[0].strip() if old_string.strip() else ""
            hint = ""
            if first:
                at = [i + 1 for i, line in enumerate(text.splitlines()) if first in line]
                if at:
                    hint = (f" The first line of old_string does appear at line(s) "
                            f"{', '.join(map(str, at[:5]))}: check indentation and the lines after it.")
            return {"error": "old_string was not found in the file." + hint
                    + " Use read_file to copy the exact text."}
        if count > 1 and not replace_all:
            lines = [text.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(old_string), text)]
            return {"error": f"old_string appears {count} times (lines {', '.join(map(str, lines[:8]))}). "
                             "Add surrounding lines to make it unique, or set replace_all=true."}
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(p)
        return {"edited": str(p), "replacements": count if replace_all else 1}

    def grep(pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False,
             context: int = 0, output: str = "content", max_results: int = 200):
        """Search file contents with a regular expression (ripgrep when installed).

        Args:
            pattern (str): Regular expression to search for
            path (str): File or directory to search
            glob (str): Only search files matching this glob, e.g. "*.py"
            ignore_case (bool): Case-insensitive match
            context (int): Lines of context around each match (content output only)
            output (str): "content" (matching lines), "files" (file names only) or "count" (matches per file)
            max_results (int): Maximum lines/files returned
        """
        error, root = policy.check_path(path)
        if error:
            return {"error": error, "blocked": True}
        if not root.exists():
            return {"error": f"No such path: {path}"}
        if output not in ("content", "files", "count"):
            return {"error": 'output must be "content", "files" or "count"'}
        try:
            max_results, context = max(1, int(max_results)), max(0, min(int(context), 10))
        except (TypeError, ValueError):
            return {"error": "max_results and context must be integers"}
        if shutil.which("rg"):
            result = _grep_rg(pattern, root, glob, ignore_case, context, output)
        else:
            result = _grep_python(pattern, root, glob, ignore_case, context, output)
        if "error" in result:
            return result
        lines = result["lines"]
        return {"engine": result["engine"], "output": "\n".join(lines[:max_results]),
                "total": len(lines), "truncated": len(lines) > max_results}

    def glob(pattern: str, path: str = "."):
        """Find files by glob pattern (supports **), newest first. A bare name like "*.py" searches all subfolders.

        Args:
            pattern (str): Glob such as "*.py", "**/*.py" or "src/*.js"
            path (str): Directory to search from
        """
        error, root = policy.check_path(path)
        if error:
            return {"error": error, "blocked": True}
        if not root.is_dir():
            return {"error": f"Not a directory: {path}"}
        found = []
        if "/" not in pattern and "**" not in pattern:
            pattern = "**/" + pattern  # models expect "view_*.js" to search everywhere, like find -name
        try:
            for p in root.glob(pattern):
                rel = p.relative_to(root)
                if any(part in SKIP_DIRS for part in rel.parts) or not p.is_file():
                    continue
                try:
                    found.append((p.stat().st_mtime, str(rel)))
                except OSError:
                    continue
        except (ValueError, NotImplementedError) as e:
            return {"error": f"Bad glob pattern: {e}"}
        found.sort(reverse=True)
        return {"matches": [name for _, name in found[:GLOB_CAP]], "count": len(found),
                "truncated": len(found) > GLOB_CAP}

    for fn in (read_file, grep, glob):
        fn.read_only = True
    return read_file, edit_file, grep, glob


def _grep_rg(pattern, root, glob, ignore_case, context, output):
    cmd = ["rg", "--no-heading", "--color", "never", "--max-columns", "400", "--max-columns-preview"]
    cmd += {"content": ["--line-number"], "files": ["-l"], "count": ["-c"]}[output]
    if ignore_case:
        cmd.append("-i")
    if glob:
        cmd += ["-g", glob]
    if context and output == "content":
        cmd += ["-C", str(context)]
    cmd += ["-e", pattern, "--", str(root)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=GREP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": f"grep timed out after {GREP_TIMEOUT}s; narrow the path or pattern"}
    if done.returncode == 2 and not done.stdout:
        return {"error": (done.stderr or "ripgrep failed").strip()[:500]}
    return {"engine": "rg", "lines": done.stdout.splitlines()}


def _grep_python(pattern, root, glob, ignore_case, context, output):
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}
    files = [root] if root.is_file() else _walk(root, glob)
    lines, deadline = [], time.monotonic() + GREP_TIMEOUT
    for f in files:
        if time.monotonic() > deadline:
            return {"error": f"grep timed out after {GREP_TIMEOUT}s; narrow the path or pattern"}
        if glob and root.is_file() and not fnmatch.fnmatch(f.name, glob):
            continue
        if _is_binary(f):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits = [i for i, line in enumerate(content) if rx.search(line)]
        if not hits:
            continue
        if output == "files":
            lines.append(str(f))
        elif output == "count":
            lines.append(f"{f}:{len(hits)}")
        else:
            shown = set()
            for i in hits:
                shown.update(range(max(0, i - context), min(len(content), i + context + 1)))
            for i in sorted(shown):
                sep = ":" if i in hits else "-"
                lines.append(f"{f}{sep}{i + 1}{sep}{content[i][:400]}")
    return {"engine": "python", "lines": lines}


def _walk(root, glob):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or (glob and not fnmatch.fnmatch(name, glob)):
                continue
            yield Path(dirpath) / name


# ---------- large tool outputs spill to disk ----------

def spill_dir(mind):
    return Path(mind) / "tool_outputs"


def spill_output(mind, turn_id, number, text):
    """Save a long tool result to mind/tool_outputs and return head + tail with a note.

    Returns `text` unchanged when it is short enough, or when the file cannot be written
    (then it is simply cut, as before).
    """
    if len(text) <= SPILL_THRESHOLD:
        return text
    head, tail = text[:SPILL_HEAD], text[-SPILL_TAIL:]
    try:
        folder = spill_dir(mind)
        folder.mkdir(parents=True, exist_ok=True)
        safe_turn = re.sub(r"[^A-Za-z0-9_-]", "", turn_id) or "turn"
        path = folder / f"{safe_turn}-{number}.txt"
        path.write_text(text, encoding="utf-8")
    except OSError:
        return f"{head}\n[... {len(text) - SPILL_HEAD - SPILL_TAIL} characters cut ...]\n{tail}"
    note = (f"[Full output ({len(text)} chars) saved to {path}; page through it with "
            f"read_file(path, offset, limit) or grep.]")
    return f"{head}\n[... middle omitted ...]\n{tail}\n{note}"


def prune_spills(mind, days=SPILL_KEEP_DAYS):
    """Delete spilled outputs older than `days`. Returns how many files were removed."""
    folder, removed = spill_dir(mind), 0
    if not folder.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    for f in folder.glob("*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
