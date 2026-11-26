"""Run one of a draft agent's Python tools in a throw-away subprocess (the builder's test runs).

Parent side: `run_tool_subprocess(...)` starts `python -m jarvis.specialists.tool_runner` in a
fresh temp directory with a small environment, sends a JSON request on stdin and returns the
JSON the child printed. Child side: `main()` builds a stub `ctx` with the same API as
`PluginContext`, imports the plugin's tools the way `load_plugin_tools` does, calls the tool
and prints {ok, result | error, traceback, logs, stdout} after a marker line.

Records live in a temp data dir (a copy in edit mode), so test runs never change real data.
"""

import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

TIMEOUT = 60
OUTPUT_CAP = 20_000
MARKER = "@@JARVIS_TOOL_RESULT@@"
NETWORK_TOOLS = ("web_search", "fetch_page")


# ---------- the child process ----------

class _NoNetwork:
    def __getattr__(self, name):
        raise PermissionError("this agent's manifest says network = false")


class _Settings(dict):
    """Like SettingsView: .get(key, default) treats empty values as missing."""

    def get(self, key, default=None):
        value = dict.get(self, key)
        return default if value in (None, "") else value


class StubContext:
    """The `ctx` a plugin's register(ctx) receives while it is being tested."""

    def __init__(self, request, manifest):
        self.id = manifest["id"]
        self.name = manifest["name"]
        self.data_dir = Path(request["data_dir"])
        self.settings = _Settings(request.get("settings") or {})
        self.network = bool(request.get("network"))
        self.http = self._client() if self.network else _NoNetwork()
        self.tools = {}
        self.logs = []
        self._tables = {t["name"]: t for t in manifest["records"]}

    @staticmethod
    def _client():
        from .plugin_tools import shared_http
        return shared_http()

    def tool(self, fn=None, *, name=None, description=None):
        from .plugin_tools import RECORD_TOOL_NAMES

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
        from .records import RecordTable
        if name not in self._tables:
            raise KeyError(f"this agent has no records table '{name}' (tables: {', '.join(self._tables) or 'none'})")
        return RecordTable(self.data_dir, self._tables[name])

    def call(self, tool_name, **arguments):
        if tool_name not in NETWORK_TOOLS:
            raise RuntimeError(f"in test runs ctx.call only supports {', '.join(NETWORK_TOOLS)}; got '{tool_name}'")
        if not self.network:
            raise PermissionError("this agent's manifest says network = false, so ctx.call can't use the web")
        from .. import web_tools
        return getattr(web_tools, tool_name)(**arguments)

    def log(self, message):
        self.logs.append(str(message))


def _plugin_frames(error, plugin_dir):
    """The traceback lines inside the plugin's own files, like 'tools/x.py:12 in fn: code'."""
    root = str(Path(plugin_dir).resolve())
    lines = []
    for frame in traceback.extract_tb(error.__traceback__):
        if str(Path(frame.filename).resolve()).startswith(root):
            rel = Path(frame.filename).resolve().relative_to(root).as_posix()
            lines.append(f"{rel}:{frame.lineno} in {frame.name}: {(frame.line or '').strip()}")
    return lines[-8:]


def _cap(text):
    return text if len(text) <= OUTPUT_CAP else text[:OUTPUT_CAP] + f"\n… (cut: {len(text)} characters)"


def _call(request):
    """Import the plugin tools and call one. Returns the reply dict (never raises)."""
    from .manifest import load_manifest
    from .plugin_tools import import_tool_files

    plugin_dir = Path(request["plugin_dir"])
    manifest, _ = load_manifest(plugin_dir, check_version=False)
    ctx = StubContext(request, manifest)
    out, err = io.StringIO(), io.StringIO()
    reply = {"ok": False}
    try:
        with redirect_stdout(out), redirect_stderr(err):
            tools, errors = import_tool_files(ctx, plugin_dir / "tools", manifest["id"])
            name = request["tool"]
            if name not in tools:
                broken = "; ".join(f"{e['file']}: {e['error']}" for e in errors)
                reply["error"] = (f"no tool called '{name}'" + (f" (tools: {', '.join(sorted(tools))})" if tools else "")
                                  + (f". Problems loading the tool files: {broken}" if broken else ""))
            else:
                result = tools[name](**(request.get("arguments") or {}))
                if inspect.isawaitable(result):
                    import asyncio
                    result = asyncio.run(result)
                reply = {"ok": True, "result": json.loads(json.dumps(result, default=str))}
    except Exception as error:  # the plugin's own bug: report it with its own frames only
        reply = {"ok": False, "error": f"{error.__class__.__name__}: {error}",
                 "traceback": _plugin_frames(error, plugin_dir)}
    reply["logs"] = ctx.logs[-50:]
    reply["stdout"] = _cap(out.getvalue() + (("\n[stderr]\n" + err.getvalue()) if err.getvalue() else ""))
    text = json.dumps(reply.get("result"), ensure_ascii=False) if "result" in reply else ""
    if len(text) > OUTPUT_CAP:
        reply["result"] = {"truncated": True, "preview": text[:OUTPUT_CAP]}
    return reply


def main():
    try:
        request = json.loads(sys.stdin.read())
        reply = _call(request)
    except Exception as error:  # a broken request or manifest: still answer in JSON
        reply = {"ok": False, "error": f"{error.__class__.__name__}: {error}", "logs": [], "stdout": ""}
    sys.stdout.write("\n" + MARKER + json.dumps(reply, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------- the parent side ----------

def _child_env():
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "VIRTUAL_ENV", "PYTHONUSERBASE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    root = str(Path(__file__).resolve().parents[2])  # where `import jarvis` works from a checkout
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH", "")) if p)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _redact(reply, secrets):
    """Hide secret values anywhere in the reply (a tool that echoes a key must not leak it)."""
    text = json.dumps(reply, ensure_ascii=False)
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "••••")
    return json.loads(text)


def run_tool_subprocess(plugin_dir, tool, arguments, settings=None, network=False, records_from=None,
                        secrets=(), timeout=TIMEOUT):
    """Run one plugin tool in a subprocess. Returns {ok, result | error, traceback, logs, stdout}.

    records_from: a data dir whose records/ is copied into the temp data dir (edit mode).
    secrets: values to blank out of the reply.
    """
    with tempfile.TemporaryDirectory(prefix="jarvis-tool-test-") as temp:
        temp = Path(temp)
        (temp / "work").mkdir()
        data_dir = temp / "data"
        data_dir.mkdir()
        if records_from and (Path(records_from) / "records").is_dir():
            shutil.copytree(Path(records_from) / "records", data_dir / "records")
        request = {"plugin_dir": str(plugin_dir), "tool": tool, "arguments": arguments or {},
                   "settings": settings or {}, "network": bool(network), "data_dir": str(data_dir)}
        try:
            done = subprocess.run(
                [sys.executable, "-m", "jarvis.specialists.tool_runner"], input=json.dumps(request),
                capture_output=True, text=True, timeout=timeout, cwd=temp / "work", env=_child_env())
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"the tool did not finish within {timeout} seconds and was stopped",
                    "logs": [], "stdout": ""}
        line = next((x for x in reversed(done.stdout.splitlines()) if x.startswith(MARKER)), None)
        if line is None:
            return {"ok": False, "logs": [], "stdout": _cap(done.stdout),
                    "error": "the test runner crashed: " + (_cap(done.stderr).strip()[-600:] or f"exit code {done.returncode}")}
        return _redact(json.loads(line[len(MARKER):]), secrets)


if __name__ == "__main__":
    main()
