"""Configuration loading. config.toml lives in the project root, next to mind/."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "llm": {
        "base_url": "http://localhost:1234/v1",
        "model": "openai/gpt-oss-20b",
        "api_key_env": "JARVIS_API_KEY",
        "stream": True,
        "max_tokens": 4096,
    },
    "agent": {
        "max_tool_rounds": 20,
        # Summarize older history once the transcript exceeds this many chars
        # (~6k tokens), keeping at least this many recent messages verbatim.
        "history_char_limit": 24000,
        "keep_recent": 8,
    },
    "paths": {"mind": "mind"},
    "voice": {"enabled": False},
}


def load_config():
    config = {section: dict(values) for section, values in DEFAULTS.items()}
    path = ROOT / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            config.setdefault(section, {}).update(values)
    return config


def mind_dir(config):
    return ROOT / config["paths"]["mind"]
