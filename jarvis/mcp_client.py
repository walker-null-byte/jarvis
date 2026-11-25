"""MCP client: use tools from external MCP servers (mind/mcp.json, Claude Desktop format).

One background asyncio thread owns every server connection. Each server's tools are
registered into the tool registry as `mcp__<server>__<tool>`; they are hidden from the
model's fixed tools array (reach them with find_tools) unless listed in core_tools.

Approvals: readOnlyHint tools never ask; destructiveHint tools always ask; other tools
ask once per server per session (Approve = allowed for the rest of the session) unless
the server has "trust": true.
"""

import asyncio
import json
import os
import re
import threading
from contextlib import AsyncExitStack
from pathlib import Path

from . import keystore, toolctx
from .events import Event
from .redaction import redact_text

NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
CONNECT_TIMEOUT = 120      # first `npx -y` run may download the package
CALL_TIMEOUT = 120
SPEC_KEYS = ("command", "args", "env", "cwd", "url", "headers", "disabled", "trust", "timeout")


def tool_name(server, tool):
    return f"mcp__{server}__{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}"


def expand(value):
    """Fill ${NAME} from the keystore, then the environment; unknown names become ''."""
    return VAR_RE.sub(lambda m: keystore.get(m.group(1)) or os.environ.get(m.group(1)) or "", value)


def _expand_all(value):
    if isinstance(value, str):
        return expand(value)
    if isinstance(value, list):
        return [_expand_all(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_all(v) for k, v in value.items()}
    return value


def _get(obj, snake, camel=None):
    """Attribute under its mcp 2.x (snake_case) or 1.x (camelCase) name."""
    value = getattr(obj, snake, None)
    return value if value is not None else getattr(obj, camel or snake, None)


def parse_servers(data):
    """Server specs from pasted JSON: {"mcpServers": {...}}, {"name": {...}}, or one object with a "name".

    Returns {name: spec}. Raises ValueError with a readable message.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise ValueError(f"That is not valid JSON ({e.msg} at line {e.lineno})") from None
    if not isinstance(data, dict) or not data:
        raise ValueError("Expected a JSON object describing one or more servers")
    if isinstance(data.get("mcpServers"), dict):
        servers = data["mcpServers"]
    elif isinstance(data.get("name"), str) and ("command" in data or "url" in data):
        servers = {data["name"]: {k: v for k, v in data.items() if k != "name"}}
    elif all(isinstance(v, dict) for v in data.values()):
        servers = data
    else:
        raise ValueError('Use {"mcpServers": {"name": {"command": ...}}} or one server object with a "name"')
    out = {}
    for name, spec in servers.items():
        if not NAME_RE.fullmatch(str(name)):
            raise ValueError(f"Server name {name!r} must be letters, digits, - or _ (max 40)")
        if not isinstance(spec, dict) or not (isinstance(spec.get("command"), str) or isinstance(spec.get("url"), str)):
            raise ValueError(f'Server "{name}" needs a "command" (local program) or a "url" (remote server)')
        if "args" in spec and not (isinstance(spec["args"], list) and all(isinstance(a, str) for a in spec["args"])):
            raise ValueError(f'Server "{name}": "args" must be a list of strings')
        for key in ("env", "headers"):
            if key in spec and not isinstance(spec[key], dict):
                raise ValueError(f'Server "{name}": "{key}" must be an object')
        out[str(name)] = {k: v for k, v in spec.items() if k in SPEC_KEYS}
    return out


def explain(error):
    """A one-line human message for an exception (unwraps anyio ExceptionGroups)."""
    # ExceptionGroup is built in from Python 3.11; on 3.10 anyio uses the backport.
    while getattr(error, "exceptions", None) and isinstance(error.exceptions, (list, tuple)):
        error = error.exceptions[0]
    if isinstance(error, FileNotFoundError):
        return f"Command not found: {error.filename or error}. Is it installed and on PATH?"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "Timed out while connecting"
    message = str(error).strip() or error.__class__.__name__
    return message.splitlines()[0][:300]


class _Server:
    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.status = "connecting" if not spec.get("disabled") else "disabled"
        self.error = ""
        self.tools = []          # full registry names
        self.info = {}           # tool name -> {"read_only", "destructive"}
        self.session = None
        self.stop = None         # asyncio.Event, created on the loop
        self.task = None

    @property
    def enabled(self):
        return not self.spec.get("disabled")

    def summary(self):
        spec = self.spec
        if spec.get("command"):
            target = " ".join([spec["command"], *spec.get("args", [])])
        else:
            target = spec.get("url", "")
        return {"name": self.name, "status": self.status, "error": self.error,
                "enabled": self.enabled, "trust": bool(spec.get("trust")),
                "transport": "stdio" if spec.get("command") else "http",
                "target": target[:200], "tool_count": len(self.tools),
                "tools": [t.split("__", 2)[2] for t in self.tools][:200]}


class McpManager:
    def __init__(self, registry, mind):
        self.registry = registry
        self.mind = Path(mind)
        self.path = self.mind / "mcp.json"
        self.servers = {}
        self.buses = []
        self._approved = set()          # (server, session_id) the user approved
        self._lock = threading.RLock()
        self._loop = None
        self._thread = None
        self._mtime = None

    # ---------- file ----------

    def _read_file(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            return {n: s for n, s in servers.items() if isinstance(s, dict)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _write_file(self):
        data = {"mcpServers": {n: s.spec for n, s in self.servers.items()}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    def sync_file(self):
        """Pick up hand edits of mcp.json: start new servers, drop removed ones, restart changed ones."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._mtime and self.servers:
            return
        self._mtime = mtime
        wanted = self._read_file()
        with self._lock:
            for name in [n for n in self.servers if n not in wanted]:
                self._stop(name)
                del self.servers[name]
            for name, spec in wanted.items():
                current = self.servers.get(name)
                if current is None:
                    self.servers[name] = _Server(name, spec)
                    self._start(name)
                elif current.spec != spec:
                    self._stop(name)
                    self.servers[name] = _Server(name, spec)
                    self._start(name)

    # ---------- public API (thread-safe) ----------

    def start(self):
        """Read mcp.json and connect enabled servers in the background. Returns at once."""
        self.sync_file()

    def list(self):
        self.sync_file()
        return [s.summary() for s in self.servers.values()]

    def add(self, data):
        """Add or replace servers from pasted JSON; returns the names."""
        parsed = parse_servers(data)
        with self._lock:
            for name, spec in parsed.items():
                if name in self.servers:
                    self._stop(name)
                self.servers[name] = _Server(name, spec)
            self._write_file()
            for name in parsed:
                self._start(name)
        return list(parsed)

    def remove(self, name):
        with self._lock:
            if name not in self.servers:
                return False
            self._stop(name)
            del self.servers[name]
            self._write_file()
        return True

    def update(self, name, enabled=None, trust=None):
        with self._lock:
            srv = self.servers.get(name)
            if srv is None:
                return False
            spec = dict(srv.spec)
            if trust is not None:
                spec.pop("trust", None)
                if trust:
                    spec["trust"] = True
            if enabled is not None:
                spec.pop("disabled", None)
                if not enabled:
                    spec["disabled"] = True
            restart = enabled is not None and enabled != srv.enabled
            srv.spec = spec
            self._write_file()
            if restart:
                self._stop(name)
                self.servers[name] = _Server(name, spec)
                self._start(name)
            else:
                self._emit_status(srv)
        return True

    def restart(self, name):
        with self._lock:
            srv = self.servers.get(name)
            if srv is None:
                return False
            self._stop(name)
            self.servers[name] = _Server(name, srv.spec)
            self._start(name)
        return True

    def shutdown(self):
        with self._lock:
            names = list(self.servers)
        for name in names:
            self._stop(name, wait=2.0)
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

    # ---------- event loop thread ----------

    def _ensure_loop(self):
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._loop = loop
        self._thread = threading.Thread(target=run, name="mcp-client", daemon=True)
        self._thread.start()
        ready.wait(5)

    def _start(self, name):
        srv = self.servers[name]
        if not srv.enabled:
            self._emit_status(srv)
            return
        self._ensure_loop()
        self._emit_status(srv)
        srv.task = asyncio.run_coroutine_threadsafe(self._serve(srv), self._loop)

    def _stop(self, name, wait=5.0):
        srv = self.servers.get(name)
        if srv is None or srv.task is None:
            if srv:
                self._unregister(srv)
            return
        loop = self._loop
        if srv.stop is not None and loop:
            loop.call_soon_threadsafe(srv.stop.set)
        try:
            srv.task.result(wait)
        except Exception:
            srv.task.cancel()
        self._unregister(srv)
        srv.task = None

    # ---------- one server's lifetime (runs on the loop) ----------

    async def _serve(self, srv):
        srv.stop = asyncio.Event()
        log = None
        try:
            async with AsyncExitStack() as stack:
                log = self._open_log(srv.name)
                streams = await asyncio.wait_for(self._open_transport(stack, srv, log), CONNECT_TIMEOUT)
                from mcp import ClientSession
                session = await stack.enter_async_context(ClientSession(*streams[:2]))
                await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
                tools = await self._list_tools(session)
                srv.session = session
                self._register(srv, tools)
                srv.status, srv.error = "connected", ""
                self._emit_status(srv)
                await srv.stop.wait()
        except BaseException as e:  # includes anyio ExceptionGroup from a dead transport
            if not srv.stop.is_set():
                srv.status, srv.error = "failed", self._failure_text(srv, e, log)
                self._emit_status(srv)
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
        finally:
            srv.session = None
            self._unregister(srv)
            if log:
                log.close()
            if srv.status == "connected":
                srv.status = "disabled" if not srv.enabled else "stopped"
                self._emit_status(srv)

    def _failure_text(self, srv, error, log):
        text = explain(error)
        tail = ""
        if log:
            try:
                log.flush()
                lines = Path(log.name).read_text(errors="replace").strip().splitlines()
                tail = " | ".join(line.strip() for line in lines[-3:])[:300]
            except OSError:
                pass
        if tail and tail not in text:
            text = f"{text} (server said: {tail})"
        return redact_text(text)

    def _open_log(self, name):
        folder = self.mind / "mcp_logs"
        folder.mkdir(parents=True, exist_ok=True)
        return open(folder / f"{name}.log", "w", encoding="utf-8")

    async def _open_transport(self, stack, srv, log):
        spec = _expand_all(srv.spec)
        if spec.get("command"):
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(command=spec["command"], args=spec.get("args", []),
                                           env=spec.get("env") or None, cwd=spec.get("cwd") or None)
            return await stack.enter_async_context(stdio_client(params, errlog=log))
        headers = spec.get("headers") or {}
        try:  # mcp 2.x
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client
        except ImportError:  # mcp 1.x
            from mcp.client.streamable_http import streamablehttp_client
            return await stack.enter_async_context(streamablehttp_client(spec["url"], headers=headers))
        client = await stack.enter_async_context(create_mcp_http_client(headers=headers))
        return await stack.enter_async_context(streamable_http_client(spec["url"], http_client=client))

    @staticmethod
    async def _list_tools(session):
        tools, cursor = [], None
        for _ in range(50):  # pagination guard
            if cursor:
                try:  # mcp 2.x
                    from mcp_types import PaginatedRequestParams
                    page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
                except ImportError:  # mcp 1.x
                    page = await session.list_tools(cursor=cursor)
            else:
                page = await session.list_tools()
            tools += list(page.tools)
            cursor = _get(page, "next_cursor", "nextCursor")
            if not cursor:
                break
        return tools

    # ---------- registry ----------

    def _register(self, srv, tools):
        table = self.registry.get_registry().tools
        for tool in tools:
            full = tool_name(srv.name, tool.name)
            ann = getattr(tool, "annotations", None)
            read_only = bool(_get(ann, "read_only_hint", "readOnlyHint")) if ann else False
            destructive = bool(_get(ann, "destructive_hint", "destructiveHint")) if ann else False
            srv.info[tool.name] = {"read_only": read_only, "destructive": destructive}
            desc = f"[MCP: {srv.name}] {(tool.description or tool.name).strip()}"[:1200]
            func = self._make_func(srv.name, tool.name)
            func.read_only = read_only and not destructive
            self.registry.register(func, name=full, description=desc)
            table[full]["input_schema"] = _plain_schema(_get(tool, "input_schema", "inputSchema"))
            srv.tools.append(full)

    def _unregister(self, srv):
        for full in srv.tools:
            try:
                self.registry.unregister(full)
            except Exception:
                pass
        srv.tools = []
        srv.info = {}

    def _make_func(self, server, tool):
        def call(**arguments):
            return self.call_tool(server, tool, arguments)
        return call

    # ---------- calling ----------

    def call_tool(self, server, tool, arguments):
        srv = self.servers.get(server)
        if srv is None or srv.session is None or srv.status != "connected":
            why = f" ({srv.status}: {srv.error})" if srv and srv.error else ""
            return {"error": f"MCP server {server!r} is not connected{why}"}
        denied = self._check_approval(srv, tool, arguments)
        if denied:
            return denied
        timeout = float(srv.spec.get("timeout") or CALL_TIMEOUT)
        future = asyncio.run_coroutine_threadsafe(self._call(srv, tool, arguments, timeout), self._loop)
        ctx = toolctx.current()
        while True:
            try:
                return future.result(0.2)
            except TimeoutError:
                if ctx and ctx.cancel_event and ctx.cancel_event.is_set():
                    future.cancel()
                    return {"error": "Cancelled"}
            except Exception as e:
                return {"error": redact_text(explain(e))}

    async def _call(self, srv, tool, arguments, timeout):
        try:
            result = await asyncio.wait_for(srv.session.call_tool(tool, arguments), timeout)
        except asyncio.TimeoutError:
            return {"error": f"{srv.name}/{tool} timed out after {timeout:.0f}s"}
        except Exception as e:
            if type(e).__name__ in ("ClosedResourceError", "BrokenResourceError", "EndOfStream"):
                srv.status, srv.error = "failed", "Connection lost; use Restart"
                self._emit_status(srv)
            raise
        return result_to_value(result)

    def _check_approval(self, srv, tool, arguments):
        """None when the call may proceed, else an error dict."""
        info = srv.info.get(tool, {})
        if info.get("read_only") and not info.get("destructive"):
            return None
        ctx = toolctx.current()
        session_id = ctx.session_id if ctx else ""
        always = bool(info.get("destructive"))
        if not always and (srv.spec.get("trust") or (srv.name, session_id) in self._approved):
            return None
        policy = getattr(self.registry.get_registry(), "_jarvis_policy", None)
        payload = {"server": srv.name, "tool": tool, "arguments": arguments,
                   "note": ("destructive tool: asked every time" if always else
                            f"Approve to let this session use {srv.name}'s tools without asking again")}
        if policy is None or not policy.request_approval("mcp_tool", payload):
            return {"error": "Tool call was not approved", "approval_required": True}
        if not always:
            self._approved.add((srv.name, session_id))
        return None

    # ---------- events ----------

    def add_bus(self, bus):
        if bus is not None and bus not in self.buses:
            self.buses.append(bus)

    def _emit_status(self, srv):
        data = srv.summary()
        for bus in list(self.buses):
            bus.emit(Event("mcp_status", "", data=data))


def _plain_schema(schema):
    """The tool's JSON schema, made safe for Jarvis's small validator (no list-valued types)."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    schema = dict(schema)
    schema["type"] = "object"
    props = {}
    for key, rule in (schema.get("properties") or {}).items():
        rule = dict(rule) if isinstance(rule, dict) else {}
        if not isinstance(rule.get("type", ""), str):
            rule.pop("type")
        props[key] = rule
    schema["properties"] = props
    return schema


def result_to_value(result):
    """A CallToolResult as text for the model, or {"error": text} when the server flagged an error."""
    parts = []
    for block in _get(result, "content") or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(block.text)
        elif kind == "image":
            parts.append(f"[image: {getattr(block, 'mime_type', None) or getattr(block, 'mimeType', '')}]")
        elif kind == "resource":
            res = getattr(block, "resource", None)
            parts.append(getattr(res, "text", None) or f"[resource: {getattr(res, 'uri', '')}]")
        else:
            parts.append(str(block))
    text = "\n".join(p for p in parts if p).strip()
    structured = _get(result, "structured_content", "structuredContent")
    if not text and structured:
        text = json.dumps(structured, default=str)
    if _get(result, "is_error", "isError"):
        return {"error": text or "The tool reported an error"}
    return text


# ---------- one manager per mind folder ----------

_managers = {}
_managers_lock = threading.Lock()


def manager_for(registry, mind, events=None):
    """The McpManager of this mind folder (created on first use); `events` gets mcp_status events."""
    key = str(Path(mind).resolve())
    with _managers_lock:
        mgr = _managers.get(key)
        if mgr is None:
            mgr = _managers[key] = McpManager(registry, mind)
    mgr.add_bus(events)
    return mgr


def attach(registry, mind, events=None):
    """Called when an agent starts: make sure the configured servers are connecting."""
    mgr = manager_for(registry, mind, events)
    if mgr.path.exists() and not mgr.servers:
        mgr.start()
    return mgr


def shutdown_all():
    for mgr in list(_managers.values()):
        mgr.shutdown()


import atexit  # noqa: E402

atexit.register(shutdown_all)
