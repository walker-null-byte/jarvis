"""Which tool schemas the model sees, and the meta-tools that reach the rest.

In "search" mode (default) every request carries a small FIXED tools array:
the core tools plus find_tools / call_tool / read_skill. Everything else in the
registry is discovered with find_tools and run through call_tool, so the model
never gets the whole catalog dumped into its context (the point of TurboMCP).
In "all" mode the array is every registry tool (legacy behaviour).
"""

import re
from pathlib import Path

META_TOOLS = ("find_tools", "call_tool", "read_skill")
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_LISTED = 60  # fallback listing size when a search matches nothing
MAX_PROMPT_NAMES = 80
# turbomcp's older web tools: web_search / fetch_page replace them, so the model sees one choice.
LEGACY_HIDDEN = {"internet_search", "webpage_scrape"}
# Tools that only read: calls to these in one reply may run concurrently.
READ_ONLY_TOOLS = {
    "read_file", "grep", "glob", "list_dir", "recall", "find_tools", "read_skill", "web_search",
    "fetch_page", "spawn_agent", "command_output", "get_current_time", "calculate", "get_system_info",
}
MCP_PREFIX = "mcp__"
# Old tool names kept callable but never listed (cron_* -> routine_*).
ALIAS_TOOLS = frozenset({"cron_add", "cron_remove", "cron_list", "cron_status", "cron_enable", "cron_disable"})

META_SCHEMAS = [
    {"type": "function", "function": {
        "name": "find_tools",
        "description": "Search the catalog of extra tools by keyword. Returns names, descriptions and input schemas.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What you want to do, e.g. 'fetch a webpage'"},
            "top_n": {"type": "integer", "description": "Max results (default 5)"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "call_tool",
        "description": "Run any tool found with find_tools. Pass its exact name and an arguments object.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Tool name"},
            "arguments": {"type": "object", "description": "Arguments matching the tool's input schema"},
        }, "required": ["name", "arguments"]},
    }},
    {"type": "function", "function": {
        "name": "read_skill",
        "description": "Read a skill's markdown from the Skills index before following it.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name from the Skills index"},
        }, "required": ["name"]},
    }},
]


def _one_line(text):
    return ((text or "").strip().splitlines() or [""])[0][:100]


class ToolExposure:
    """What one agent sees. `profile` (optional AgentProfile) adds core_tools and
    extra_tools (private callables, always directly visible) and hides hidden_tools."""

    def __init__(self, registry, config, mind, profile=None):
        self.registry = registry
        self.config = config
        self.mind = Path(mind)
        self.hidden = set(getattr(profile, "hidden_tools", None) or ()) | LEGACY_HIDDEN
        self.can_spawn = profile is None or getattr(profile, "allow_spawn", True)
        if not self.can_spawn:
            self.hidden.add("spawn_agent")  # sub-agents cannot spawn (depth 1)
        self.extra_core = list(getattr(profile, "core_tools", None) or ())
        self.extras = None  # tiny private turbomcp registry: schemas derived like the main one
        extra_tools = getattr(profile, "extra_tools", None) or {}
        if extra_tools:
            from turbomcp import TurboMCP
            self.extras = TurboMCP(name="extras")
            for name, func in extra_tools.items():
                self.extras.register(func, name=name)
                if isinstance(getattr(func, "input_schema", None), dict):  # e.g. records tools built from a manifest
                    self.extras.tools[name]["input_schema"] = func.input_schema
        self._names = None
        self._tools = []
        self.refresh()

    def _all_names(self):
        """Registry tool names this agent may use (hidden removed, extras added)."""
        names = [n for n in self.registry.tool_names() if n not in self.hidden and n not in ALIAS_TOOLS]
        return names + [n for n in (self.extras.tools if self.extras else ()) if n not in names]

    def _schemas(self, names):
        extras = self.extras.tools if self.extras else {}
        own = [n for n in names if n in extras]
        base = self.registry.to_openai_tools([n for n in names if n not in extras])
        return base + self.extras.to_openai_tools(own) if own else base

    @property
    def mode(self):
        return self.config["agent"].get("tool_mode", "search")

    def core_names(self):
        """Configured core tools that actually exist, sorted."""
        existing = set(self._all_names())
        wanted = [*self.config["agent"]["core_tools"], *self.extra_core, *(self.extras.tools if self.extras else ())]
        if self.can_spawn:
            wanted.append("spawn_agent")
        return sorted({n for n in wanted if n in existing})

    def refresh(self):
        """Rebuild the tools array if the registry's tool set changed.

        Call only at the start of a user turn so rounds stay identical.
        """
        names = tuple(sorted(self._all_names()))
        key = (names, self.mode, tuple(self.config["agent"]["core_tools"]))
        if key == self._names:
            return
        self._names = key
        if self.mode == "all":
            core = set(self.core_names())  # MCP tools stay out of the array unless made core
            self._tools = self._schemas([n for n in names if not n.startswith(MCP_PREFIX) or n in core])
        else:
            self._tools = self._schemas(self.core_names()) + META_SCHEMAS

    def tools(self):
        return self._tools

    def hidden_names(self):
        """Non-core registry tool names (what find_tools can reveal)."""
        core = set(self.core_names())
        return sorted(n for n in self._all_names() if n not in core)

    def prompt_section(self):
        """The '# Tools' system-prompt section ('' in all mode)."""
        if self.mode == "all":
            return ""
        all_names = self.hidden_names()
        mcp = [n for n in all_names if n.startswith(MCP_PREFIX)]
        names = [n for n in all_names if not n.startswith(MCP_PREFIX)]
        shown = ", ".join(names[:MAX_PROMPT_NAMES])
        if len(names) > MAX_PROMPT_NAMES:
            shown += f", ... (+{len(names) - MAX_PROMPT_NAMES} more, use find_tools)"
        if mcp:
            servers = sorted({n.split("__")[1] for n in mcp})
            shown += f"; {len(mcp)} MCP tools from {', '.join(servers)} (find them with find_tools)"
        return (
            "# Tools\n"
            f"- Directly callable: {', '.join(self.core_names())}.\n"
            "- Everything else: discover with find_tools(query), then run it with\n"
            "  call_tool(name, arguments). Use read_skill(name) for the Skills index.\n"
            f"- Other tools available: {shown or '(none)'}\n"
        )

    # ---------- execution ----------

    def is_read_only(self, name):
        """True for a (already unwrapped) tool that only reads, so it may run in parallel."""
        if name in self.hidden:
            return False
        if name in READ_ONLY_TOOLS:
            return True
        if self.extras and name in self.extras.tools:
            return False
        tool = self.registry.get_registry().tools.get(name)
        return bool(tool and getattr(tool["func"], "read_only", False))

    def unwrap(self, name, args):
        """Resolve a call_tool proxy to (inner_name, inner_args, via)."""
        if name != "call_tool" or not isinstance(args, dict):
            return name, args, ""
        inner, inner_args = args.get("name"), args.get("arguments", {})
        if isinstance(inner_args, str):
            try:
                import json
                inner_args = json.loads(inner_args or "{}")
            except ValueError:
                return name, args, ""
        if not isinstance(inner, str) or not isinstance(inner_args, dict):
            return name, args, ""
        return inner, inner_args, "call_tool"

    def run(self, name, args, via=""):
        """Execute a tool call (already unwrapped); returns the result."""
        if name == "call_tool":
            return {"error": "call_tool needs a string 'name' and an object 'arguments'."}
        if name in META_TOOLS:
            if via:
                return {"error": f"{name} is already directly callable; don't go through call_tool."}
            if name == "find_tools":
                return self.find_tools(args.get("query", ""), args.get("top_n", 5))
            return self.read_skill(args.get("name", ""))
        if self.extras and name in self.extras.tools:
            return self._call_extra(name, args)
        if name in self.hidden or name not in self.registry.tool_names():
            close = [t["name"] for t in self.registry.search_tools(name, 5)["tools"]
                     if t["name"] not in self.hidden]
            return {"error": f"Unknown tool: {name}", "did_you_mean": close}
        return self.registry.call(name, args)

    def _call_extra(self, name, args):
        from .validation import validate_arguments
        error = validate_arguments(self.extras.tools[name].get("input_schema", {}), args)
        if error:
            return {"error": error, "validation_error": True, "tool": name}
        return self.extras.call(name, args)

    def find_tools(self, query, top_n=5):
        try:
            top_n = max(1, min(int(top_n), 20))
        except (TypeError, ValueError):
            top_n = 5
        hidden = set(self.hidden_names())
        found = self.registry.search_tools(str(query), top_n + len(self.registry.tool_names()))
        # hidden is already filtered (see hidden_names); extras are core, never listed here
        tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in found["tools"] if t["name"] in hidden
        ][:top_n]
        if tools:
            return {"query": query, "tools": tools}
        names = self.hidden_names()
        listing = [
            {"name": n, "summary": _one_line(self.registry.get_capabilities(n)["description"])}
            for n in names[:MAX_LISTED] if n in self.registry.tool_names()
        ]
        return {"query": query, "tools": [], "note": "No match. All other tools:", "available": listing}

    def read_skill(self, name):
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            return {"error": "Invalid skill name."}
        path = self.mind / "skills" / f"{name}.md"
        if not path.is_file():
            return {"error": f"No such skill: {name}"}
        return path.read_text(encoding="utf-8")


EMBEDDING_REPO = "minishlab/potion-base-8M"
EMBEDDING_FILES = ("config.json", "model.safetensors")


def embedding_model_cached():
    """True if the embedding model is already in the local Hugging Face cache.

    Only looks at the cache; never touches the network.
    """
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    for filename in EMBEDDING_FILES:
        try:
            found = try_to_load_from_cache(EMBEDDING_REPO, filename)
        except Exception:
            return False
        if not isinstance(found, str):
            return False
    return True


def semantic_status(config=None):
    """{"enabled": bool, "model_cached": bool}: enabled means semantic search
    would be used (setting on, model2vec importable and model already cached)."""
    cached = embedding_model_cached()
    wanted = True if config is None else bool(config["agent"].get("semantic_search", True))
    try:
        import model2vec  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    return {"enabled": wanted and installed and cached, "model_cached": cached}


def enable_semantic_search(registry, config=None):
    """Turn on embedding search only if agent.semantic_search is true and the
    model is already downloaded. Never downloads; silent on any failure.
    Returns True when enabled."""
    if not semantic_status(config)["enabled"]:
        return False
    try:
        registry.enable_semantic_search()
        return True
    except Exception:
        return False
