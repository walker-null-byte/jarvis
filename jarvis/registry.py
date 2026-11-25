"""Tool registry with hot-reload for tools/ directory."""

import importlib
import importlib.util
import os
import threading


from .config import load_config, mind_dir
from .tools_core import build_registry
from .mind_tools import build_mind_tools
from .validation import validate_arguments


class ToolRegistry:
    """TurboMCP registry that hot-reloads tools/ directory on changes."""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.mind = mind_dir(self.config)
        self.tools_dir = self.mind.parent / "tools"
        self.tools_dir.mkdir(exist_ok=True)

        self._registry = self._build_base_registry()
        self._loaded_tools = {}  # name -> module mtime
        self._lock = threading.Lock()
        self._watcher_thread = None
        self._stop_watcher = threading.Event()

    def _build_base_registry(self):
        """Build registry with core + mind tools (not learned tools)."""
        reg = build_registry(self.mind, self.config)
        for f in build_mind_tools(self.mind):
            reg.register(f)
        return reg

    def _load_tools_dir(self):
        """Load all .py files from tools/ directory into the registry."""
        if not self.tools_dir.exists():
            return

        for tool_path in self.tools_dir.glob("*.py"):
            if tool_path.name.startswith("_"):
                continue
            self._load_tool_file(tool_path)

    def _load_tool_file(self, tool_path):
        """Load a single tool module and register its register() function.

        Exec is done outside the lock; only the final register step is
        guarded. Prevents blocking callers while arbitrary tool code runs.
        """
        name = tool_path.stem
        try:
            mtime = tool_path.stat().st_mtime
            with self._lock:
                if name in self._loaded_tools and self._loaded_tools[name] == mtime:
                    return  # unchanged

            spec = importlib.util.spec_from_file_location(name, tool_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if hasattr(module, "register"):
                with self._lock:
                    # Re-check after exec in case watcher removed it
                    if name in self._loaded_tools and self._loaded_tools[name] == mtime:
                        return
                    # Unregister previous version if exists
                    if name in self._loaded_tools:
                        self._unload_tool(name)
                    module.register(self._registry)
                    self._loaded_tools[name] = mtime
        except Exception as e:
            print(f"[registry] Failed to load {tool_path.name}: {e}", file=os.sys.stderr)

    def _unload_tool(self, name):
        """Unregister a tool by name."""
        if name in self._registry.tools:
            self._registry.unregister(name)
        self._loaded_tools.pop(name, None)

    def reload(self):
        """Full reload of tools/ directory."""
        with self._lock:
            # Unload all current tools
            for name in list(self._loaded_tools.keys()):
                self._unload_tool(name)
            # Reload from disk
            self._load_tools_dir()

    def get_registry(self):
        """Get the underlying TurboMCP registry."""
        return self._registry

    # Delegate common methods to the underlying registry
    def register(self, func, name=None, description=None):
        self._registry.register(func, name=name, description=description)

    def unregister(self, name):
        self._registry.unregister(name)

    def set_approval_handler(self, approval):
        if hasattr(self._registry, "_jarvis_policy"):
            self._registry._jarvis_policy.set_approval_handler(approval)

    def set_cancel_event(self, cancel_event):
        self._registry._cancel_event = cancel_event

    def call(self, name, arguments=None):
        arguments = arguments or {}
        # Copy tool reference under lock to avoid race with watcher
        with self._lock:
            tool = self._registry.tools.get(name)
        if tool:
            error = validate_arguments(tool.get("input_schema", {}), arguments)
            if error:
                return {"error": error, "validation_error": True, "tool": name}
        return self._registry.call(name, arguments)

    def list_tools(self, with_summary=False):
        return self._registry.list_tools(with_summary)

    def get_capabilities(self, name=None):
        return self._registry.get_capabilities(name)

    def to_openai_tools(self, names=None):
        return self._registry.to_openai_tools(names)

    def to_mcp_tools(self, names=None):
        return self._registry.to_mcp_tools(names)

    def tool_names(self):
        with self._lock:
            return list(self._registry.tools)

    def enable_semantic_search(self):
        """Enable embedding search once; callers should catch failures."""
        if not self._registry.semantic_enabled:
            self._registry.enable_semantic_search()

    def search_tools(self, query, top_n=5, alpha=0.5, threshold=0.15):
        return self._registry.search_tools(query, top_n, alpha, threshold)

    # Hot-reload watcher
    def _watch_loop(self):
        """Background thread that polls tools/ for changes."""
        last_mtimes = {}
        while not self._stop_watcher.is_set():
            try:
                if self.tools_dir.exists():
                    current = {}
                    for p in self.tools_dir.glob("*.py"):
                        if p.name.startswith("_"):
                            continue
                        current[p.stem] = p.stat().st_mtime

                    # Check for changes
                    if current != last_mtimes:
                        added = set(current) - set(last_mtimes)
                        removed = set(last_mtimes) - set(current)
                        modified = {n for n in current if n in last_mtimes and current[n] != last_mtimes[n]}

                        for name in added | modified:
                            path = self.tools_dir / f"{name}.py"
                            self._load_tool_file(path)
                        with self._lock:
                            for name in removed:
                                self._unload_tool(name)

                        last_mtimes = current
            except Exception:
                pass  # don't crash the watcher

            self._stop_watcher.wait(1.0)  # poll every second

    def start_watcher(self):
        """Start the background hot-reload watcher."""
        if self._watcher_thread is None or not self._watcher_thread.is_alive():
            self._stop_watcher.clear()
            self._watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
            self._watcher_thread.start()

    def stop_watcher(self):
        """Stop the background watcher."""
        self._stop_watcher.set()
        if self._watcher_thread:
            self._watcher_thread.join(timeout=2)


# Global registries (lazy-initialized per mind directory). Keeping one registry
# per mind avoids cross-test/config contamination while retaining hot reload.
_registries = {}
_registries_lock = threading.Lock()


def get_registry(config=None):
    """Get or create the global ToolRegistry."""
    cfg = config or load_config()
    key = str(mind_dir(cfg).resolve())
    with _registries_lock:
        if key not in _registries:
            registry = ToolRegistry(cfg)
            registry._load_tools_dir()
            registry.start_watcher()
            _registries[key] = registry
        return _registries[key]


def reload_tools():
    """Force a reload of the tools/ directory."""
    for registry in _registries.values():
        registry.reload()
