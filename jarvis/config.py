"""Configuration loading. config.toml lives in the project root, next to mind/."""

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 support
    import tomli as tomllib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
    },
    "agent": {
        "max_tool_rounds": 20,
        # Summarize older history once the transcript exceeds this many chars
        # (~6k tokens), keeping at least this many recent messages verbatim.
        "history_char_limit": 24000,
        "keep_recent": 8,
    },
    "paths": {"mind": "mind", "models": "models"},
    "voice": {
        "enabled": False,
        "engine": "piper",
        "model": "en_US-lessac-medium",
        "kokoro_model": "hexgrad/Kokoro-82M",
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
    "jobs": {
        "backend": "private-file",
        "crontab_file": "~/.jarvis_crontab",
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
    _apply_environment(config)
    return config


def runtime_config_path(config):
    """Return the opt-in runtime override file stored inside the mind."""
    return mind_dir(config) / "runtime_config.json"


def save_runtime_overrides(config, paths):
    """Persist only explicitly selected, non-secret setting paths."""
    data = {}
    for path in paths:
        section, _, key = path.partition(".")
        if not section or not key or section not in config or key not in config[section]:
            continue
        data.setdefault(section, {})[key] = config[section][key]
    target = runtime_config_path(config)
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
