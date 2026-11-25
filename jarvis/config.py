"""Configuration loading. config.toml lives in the project root, next to mind/."""

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 support
    import tomli as tomllib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The core_tools list shipped before 1.1.2; configs still holding exactly this get the new one.
OLD_CORE_TOOLS = ["run_command", "read_file", "write_file", "list_dir", "remember", "recall"]
NEW_CORE_TOOLS = ["run_command", "read_file", "edit_file", "write_file", "grep", "glob",
                  "remember", "recall", "routine_create"]

DEFAULTS = {
    "llm": {
        "base_url": "http://localhost:1234/v1",
        "provider": "custom",
        "model": "openai/gpt-oss-20b",
        "api_key_env": "JARVIS_API_KEY",
        "stream": True,
        "max_tokens": 4096,
        "timeout": 120,
        "retries": 2,
        "retry_delay": 1.0,
        # Optional reasoning effort for backends that support it
        # (anthropic: low|medium|high|xhigh|max). Empty = backend default.
        "effort": "",
    },
    "agent": {
        "max_tool_rounds": 50,
        # Run read-only tool calls from one reply concurrently (at most max_parallel_tools).
        "parallel_tools": True,
        "max_parallel_tools": 6,
        # Summarize older history once the transcript exceeds this many chars
        # (~6k tokens), keeping at least this many recent messages verbatim.
        "history_char_limit": 24000,
        "keep_recent": 8,
        # "search": send only core_tools + find_tools/call_tool and let the
        # model discover the rest through TurboMCP search (saves context).
        # "all": send every tool schema every request (legacy; for weak models).
        "tool_mode": "search",
        "core_tools": list(NEW_CORE_TOOLS),
        # Default character card id for new sessions (see mind/characters/)
        "character": "jarvis",
        # Cap on how much of MEMORY.md goes into the system prompt
        "memory_index_chars": 4000,
        # Blend embedding similarity into find_tools when the small embedding
        # model has been downloaded (Models panel). Never downloads by itself.
        "semantic_search": True,
    },
    "paths": {"mind": "mind", "models": "models"},
    "voice": {
        "enabled": False,
        "engine": "piper",
        "model": "en_US-lessac-medium",
        "kokoro_voice": "af_sky",
        "stt_model": "small",
        "stt_device": "auto",
        "stt_compute_type": "int8",
        "audio_device": "",
    },
    "security": {
        "mode": "guarded",
        "allowed_roots": [str(ROOT)],
        "allow_outside_roots": False,
        "max_command_timeout": 300,
        "approval_timeout": 120,
    },
    "routines": {"enabled": True, "tick_seconds": 20, "max_concurrent": 2},
    "jobs": {
        "backend": "private-file",
        "crontab_file": "~/.jarvis_crontab",
    },
    "webui": {
        "host": "127.0.0.1",
        "port": 7777,
        "open_browser": True,
    },
}


def load_config():
    config = {section: dict(values) for section, values in DEFAULTS.items()}
    path = ROOT / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            config.setdefault(section, {}).update(values)
    _apply_runtime_overrides(config)
    if config["agent"].get("core_tools") == OLD_CORE_TOOLS:
        config["agent"]["core_tools"] = list(NEW_CORE_TOOLS)  # also covers saved runtime overrides
    if config["agent"].get("max_tool_rounds") == 20:  # the 1.1 default, saved by older settings
        config["agent"]["max_tool_rounds"] = DEFAULTS["agent"]["max_tool_rounds"]
    _apply_environment(config)
    _validate_config(config)
    return config


def _validate_config(config):
    """Coerce and warn on obviously broken values without crashing."""
    import sys
    llm = config.get("llm", {})
    # provider
    from .providers import PRESETS
    valid_providers = set(PRESETS) | {"custom"}
    if llm.get("provider") not in valid_providers:
        print(f"[config] warning: llm.provider {llm.get('provider')!r} not in {valid_providers}; leaving as-is", file=sys.stderr)
    # base_url
    url = llm.get("base_url", "")
    if url and not (url.startswith("http://") or url.startswith("https://")):
        print(f"[config] warning: llm.base_url {url!r} should start with http:// or https://", file=sys.stderr)
    # numeric ranges
    for path in ("llm.timeout", "llm.retries", "llm.retry_delay", "agent.max_tool_rounds", "agent.history_char_limit", "agent.keep_recent"):
        sec, key = path.split(".")
        val = config.get(sec, {}).get(key)
        if isinstance(val, (int, float)) and val < 0:
            print(f"[config] warning: {path}={val} cannot be negative; resetting to default", file=sys.stderr)
            config[sec][key] = DEFAULTS[sec][key]
    # enums
    voice = config.get("voice", {})
    if voice.get("engine") not in ("piper", "kokoro"):
        print(f"[config] warning: voice.engine {voice.get('engine')!r} must be piper or kokoro", file=sys.stderr)
        config["voice"]["engine"] = DEFAULTS["voice"]["engine"]
    sec = config.get("security", {})
    if sec.get("mode") not in ("guarded", "permissive"):
        print(f"[config] warning: security.mode {sec.get('mode')!r} must be guarded or permissive", file=sys.stderr)
        config["security"]["mode"] = DEFAULTS["security"]["mode"]
    jobs = config.get("jobs", {})
    if jobs.get("backend") not in ("private-file", "system"):
        print(f"[config] warning: jobs.backend {jobs.get('backend')!r} must be private-file or system", file=sys.stderr)
        config["jobs"]["backend"] = DEFAULTS["jobs"]["backend"]


def runtime_config_path(config):
    """Return the opt-in runtime override file stored inside the mind."""
    return mind_dir(config) / "runtime_config.json"


def save_runtime_overrides(config, paths):
    """Persist only explicitly selected, non-secret setting paths.

    Merges with existing runtime overrides so saving a subset (e.g. model
    only) doesn't wipe previously saved keys (e.g. base_url).
    """
    target = runtime_config_path(config)
    # Start from existing persisted overrides
    existing = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    data = existing
    for path in paths:
        section, _, key = path.partition(".")
        if not section or not key or section not in config or key not in config[section]:
            continue
        data.setdefault(section, {})[key] = config[section][key]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def _apply_runtime_overrides(config):
    path = runtime_config_path(config)
    if not path.exists():
        return
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(overrides, dict):
        return
    for section, values in overrides.items():
        if isinstance(values, dict):
            config.setdefault(section, {}).update(values)


def _apply_environment(config):
    """Apply documented ``JARVIS_SECTION_KEY`` overrides with type coercion."""
    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        for key, old_value in values.items():
            env_name = f"JARVIS_{section}_{key}".upper()
            if env_name not in os.environ:
                continue
            raw = os.environ[env_name]
            if isinstance(old_value, list):
                # For list types (e.g. allowed_roots), split on os.pathsep / comma
                if not raw.strip():
                    continue
                parts = [p.strip() for p in raw.replace(";", ",").replace(":", ",").split(",")]
                values[key] = [p for p in parts if p]
                continue
            if isinstance(old_value, bool):
                values[key] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(old_value, int):
                try:
                    values[key] = int(raw)
                except ValueError:
                    continue
            elif isinstance(old_value, float):
                try:
                    values[key] = float(raw)
                except ValueError:
                    continue
            else:
                values[key] = raw


def mind_dir(config):
    return ROOT / config["paths"]["mind"]


def provider_models_path(config):
    """Per-provider model memory kept inside the mind (one slot per provider)."""
    return mind_dir(config) / "provider_models.json"


def remember_model(config, provider, model):
    """Remember the last model used with a provider so switching providers
    restores the right model instead of carrying a foreign one over."""
    if not provider or not model:
        return
    path = provider_models_path(config)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data[provider] = model
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def remembered_model(config, provider):
    """Return the last model used with *provider*, or ``None``."""
    if not provider:
        return None
    path = provider_models_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get(provider)
