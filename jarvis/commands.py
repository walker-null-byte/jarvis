"""Local slash commands shared by the Textual UI and plain CLI."""

import copy
import os
import shlex
from urllib.parse import urlparse

from . import provider_fields
from .config import (
    DEFAULTS,
    remember_model,
    remembered_model,
    runtime_config_path,
    save_runtime_overrides,
)
from .llm import LLM
from .providers import PRESETS

# Presets are pure data in jarvis/providers; re-exported for ui.py.
PROVIDER_PRESETS = PRESETS


SETTING_PATHS = (
    "llm.provider",
    "llm.model",
    "llm.base_url",
    "llm.api_key_env",
    "llm.stream",
    "llm.max_tokens",
    "llm.effort",
    "llm.timeout",
    "llm.retries",
    "llm.retry_delay",
    "agent.max_tool_rounds",
    "agent.history_char_limit",
    "agent.keep_recent",
    "agent.tool_mode",
    "agent.character",
    "voice.enabled",
    "voice.engine",
    "voice.model",
    "voice.kokoro_voice",
    "voice.stt_model",
    "voice.stt_device",
    "voice.stt_compute_type",
    "voice.audio_device",
    "security.mode",
    "security.max_command_timeout",
    "security.approval_timeout",
    "jobs.backend",
    "jobs.crontab_file",
)


# This is intentionally kept next to the command parser so the CLI, TUI, and
# any future web composer can present the same command vocabulary.
SLASH_COMMANDS = (
    ("help", "Show slash command help"),
    ("model", "Show or change the active model"),
    ("provider", "List or switch LLM providers"),
    ("login", "Sign in with ChatGPT (uses your Plus/Pro plan)"),
    ("logout", "Sign out of ChatGPT"),
    ("character", "Switch or import a character card"),
    ("voice", "Voice output: auto-speak, engine, model downloads"),
    ("settings", "Inspect or change runtime settings"),
    ("status", "Show active provider, model, and voice status"),
)

SLASH_SUBCOMMANDS = {
    "model": (
        ("list", "List models the endpoint exposes"),
    ),
    "provider": (
        ("list", "List supported provider presets"),
        ("use", "Switch to a provider preset"),
        ("set", "Configure a custom endpoint"),
        ("test", "Test the connection to an endpoint"),
        ("models", "List models an endpoint exposes"),
    ),
    "login": (
        ("chatgpt", "Sign in with your ChatGPT plan"),
    ),
    "logout": (
        ("chatgpt", "Sign out and revoke the ChatGPT token"),
    ),
    "character": (
        ("list", "List character cards"),
        ("use", "Switch the active character"),
        ("show", "Show a character card"),
        ("import", "Import a card (PNG/JSON)"),
        ("delete", "Delete an imported card"),
        ("export", "Export a card as SillyTavern JSON"),
        ("reset", "Reset an edited built-in card"),
    ),
    "voice": (
        ("status", "Show voice engine and readiness"),
        ("on", "Speak every reply aloud"),
        ("off", "Stop speaking replies (default)"),
        ("engine", "Choose piper or kokoro"),
        ("models", "List downloadable voice/STT/embedding models"),
        ("download", "Download a model by id"),
    ),
    "settings": (
        ("get", "Read one setting"),
        ("set", "Change one setting for this session"),
        ("reset", "Restore one setting to its default"),
        ("save", "Persist current runtime settings"),
    ),
}


def slash_command_suggestions(text):
    """Return palette entries for the partial slash command in *text*.

    Entries are small dictionaries rather than UI objects so the same
    vocabulary remains usable by a future web UI.  Suggestions only appear at
    the beginning of a message and disappear once command arguments begin.
    """
    raw = text.lstrip()
    if not raw.startswith("/") or "\n" in raw:
        return []

    parts = raw.split()
    if len(parts) == 1 and not raw.endswith(" "):
        query = parts[0][1:].lower()
        return [
            {
                "command": f"/{name}",
                "insert": f"/{name} ",
                "label": f"/{name}",
                "description": description,
            }
            for name, description in SLASH_COMMANDS
            if name.startswith(query)
        ]

    if not parts or len(parts) > 2:
        return []
    command = parts[0][1:].lower()
    subcommands = SLASH_SUBCOMMANDS.get(command)
    if not subcommands:
        return []
    if len(parts) == 2 and raw.endswith(" "):
        # The user has accepted a subcommand and is now entering its
        # argument; reopening the same subcommand list would be distracting.
        if parts[1].lower() in {name for name, _ in subcommands}:
            return []
    query = "" if raw.endswith(" ") else parts[-1].lower()
    return [
        {
            "command": f"/{command} {name}",
            "insert": f"/{command} {name} ",
            "label": f"/{command} {name}",
            "description": description,
        }
        for name, description in subcommands
        if name.startswith(query)
    ]


def dialog_for_command(jarvis, text):
    """Return a modal form spec for a slash command that needs input.

    The TUI calls this before executing a command; it returns ``None`` for
    commands that can run directly. Specs are plain data any UI can render:

        {
            "title": str,
            "description": str | None,
            "fields": [
                {"type": "text", "key", "label", "placeholder"?, "required"?},
                {"type": "select", "key", "label", "options": [(label, value)],
                 "default"?},
            ],
            "command": callable(values: dict) -> str,
        }

    Provider configuration uses a dedicated screen instead of a plain form
    (``{"screen": "provider"}``) so test/list/set-model live in one place.
    The ``command`` callable builds the slash command string from the
    collected values so execution stays on :func:`handle_slash_command`.
    """
    try:
        tokens = shlex.split(text.strip())
    except ValueError:
        return None
    if not tokens or not tokens[0].startswith("/"):
        return None
    command = tokens[0][1:].lower()
    args = tokens[1:]

    if command == "model":
        if args and args[0].lower() == "set":
            args = args[1:]
        if args:
            return None
        return _model_dialog(jarvis)
    if command in {"provider", "providers"}:
        if args and args[0].lower() == "list":
            return None
        if not args or (args[0].lower() in {"use", "set"} and len(args) < 2):
            return _provider_manager_dialog()
        return None
    if command in {"settings", "config"}:
        if not args:
            return None
        action = args[0].lower()
        if action == "get" and len(args) < 2:
            return _settings_get_dialog(jarvis)
        if action == "set" and len(args) < 3:
            return _settings_set_dialog(jarvis)
        if action == "reset" and len(args) < 2:
            return _settings_reset_dialog(jarvis)
        return None
    return None


def _provider_manager_dialog():
    return {
        "screen": "provider",
        "title": "Provider manager",
        "description": "Pick the endpoint, paste the API key, test the connection, and choose the model it exposes.",
    }


def _model_dialog(jarvis):
    current = jarvis.config["llm"].get("model", "")
    return {
        "title": "Change model",
        "description": "Applies to this session immediately.",
        "fields": [
            {
                "type": "text",
                "key": "model",
                "label": "Model name",
                "placeholder": current or "e.g. gemma4, openai/gpt-oss-20b",
                "required": True,
            },
        ],
        "command": lambda values: f"/model set {values['model']}",
    }


def _setting_options(jarvis):
    options = []
    for path in SETTING_PATHS:
        section, key = path.split(".", 1)
        value = jarvis.config.get(section, {}).get(key)
        options.append((f"{path}  =  {value!r}", path))
    return options


def _settings_get_dialog(jarvis):
    return {
        "title": "Read a setting",
        "fields": [
            {
                "type": "select",
                "key": "path",
                "label": "Setting",
                "options": _setting_options(jarvis),
            },
        ],
        "command": lambda values: f"/settings get {values['path']}",
    }


def _settings_set_dialog(jarvis):
    return {
        "title": "Change a setting",
        "description": "Applies immediately. Use /settings save to persist it.",
        "fields": [
            {
                "type": "select",
                "key": "path",
                "label": "Setting",
                "options": _setting_options(jarvis),
            },
            {
                "type": "text",
                "key": "value",
                "label": "New value",
                "placeholder": "e.g. on/off, 45, piper, guarded",
                "required": True,
            },
        ],
        "command": lambda values: f"/settings set {values['path']} {values['value']}",
    }


def _settings_reset_dialog(jarvis):
    return {
        "title": "Reset a setting",
        "description": "Restores the built-in default for this session.",
        "fields": [
            {
                "type": "select",
                "key": "path",
                "label": "Setting",
                "options": _setting_options(jarvis),
            },
        ],
        "command": lambda values: f"/settings reset {values['path']}",
    }


def handle_slash_command(jarvis, text, blocking=False):
    """Handle one slash command, returning output or ``None`` for normal chat.

    *blocking* only matters for ``/login chatgpt``: False (default, safe for
    the TUI/web event loop) returns the sign-in URL immediately; True waits up
    to 300 s for the browser sign-in to finish (plain CLI use).
    """
    if not text.lstrip().startswith("/"):
        return None
    try:
        tokens = shlex.split(text.strip())
    except ValueError as error:
        return f"Command parse error: {error}"
    if not tokens:
        return "Type /help for available commands."
    command = tokens[0][1:].lower()
    args = tokens[1:]
    try:
        if command in {"help", "?"}:
            return _help()
        if command == "model":
            return _model(jarvis, args)
        if command in {"provider", "providers"}:
            return _provider(jarvis, args)
        if command in {"settings", "config"}:
            return _settings(jarvis, args)
        if command in {"status", "info"}:
            return _status(jarvis)
        if command == "login":
            return _login(jarvis, args, blocking)
        if command == "logout":
            return _logout(jarvis, args)
        if command == "character":
            from .characters import handle_command
            return handle_command(jarvis, " ".join(args))
        if command == "voice":
            from .voice import handle_voice_command
            return handle_voice_command(jarvis, " ".join(args))
        return f"Unknown command: /{command}. Type /help for available commands."
    except (OSError, ValueError, RuntimeError) as error:
        return f"Command failed: {error}"


def _help():
    return """Slash commands

/model [NAME]                         Show or change the active model
/model list                           List models the endpoint exposes
/provider                             Open the provider manager (TUI)
/provider list                        List supported provider presets
/provider use NAME                    Switch provider preset
/provider use NAME SETTING=VALUE      ...with its settings (Cloudflare: account_id, gateway_id)
/provider set URL [KEY_OR_ENV]        Configure a custom endpoint; paste an
                                      API key or the name of an env var
/provider test [NAME]                 Test the connection (current or preset)
/provider models [NAME]               List models an endpoint exposes
/settings                             Show runtime settings
/settings get SECTION.KEY             Read one setting
/settings set SECTION.KEY VALUE       Change one setting for this session
/settings reset SECTION.KEY           Restore its default
/settings save                        Persist current slash settings
/login chatgpt                        Sign in with your ChatGPT Plus/Pro plan
/logout chatgpt                       Sign out of ChatGPT
/character list|use|show|import|delete|export|reset  Manage character cards
/voice status|on|off|engine|models|download     Voice output and model downloads
/status                               Show provider, backend, model and voice status
/help                                 Show this help

Settings changes apply immediately. They are session-local until `/settings save`.
Each provider remembers its own model. Literal API keys are session-local and
never persisted. Custom endpoints are tested when configured; run /provider
test to re-check."""


def _model(jarvis, args):
    if args and args[0].lower() == "set":
        args = args[1:]
    if args and args[0].lower() == "list":
        ok, message, ids, _ = _test_connection(jarvis)
        if not ids:
            if not ok:
                return f"Could not list models: {message}"
            return "The endpoint reports no models."
        # If we got ids, show them even when ok is False (model mismatch case)
        # but surface the warning so the user knows why ok is False.
        header = "Models:\n" if ok else f"Models (warning: {message}):\n"
        return header + "\n".join(ids)
    if not args:
        return f"Model: {jarvis.config['llm']['model']}"
    model = args[0].strip()
    if not model or len(model) > 200:
        return "Model name must be between 1 and 200 characters."
    jarvis.config["llm"]["model"] = model
    jarvis.llm.reconfigure(jarvis.config)
    jarvis._emit("config_changed", section="llm", key="model")
    remember_model(
        jarvis.config,
        jarvis.config["llm"].get("provider", "custom"),
        model,
    )
    save_runtime_overrides(jarvis.config, ["llm.model", "llm.provider"])
    return f"Model changed to: {model} (saved to {runtime_config_path(jarvis.config)})"


def _provider(jarvis, args):
    if not args or args[0].lower() == "list":
        current = jarvis.config["llm"].get("provider", "custom")
        lines = [f"Active provider: {current}", "", "Provider presets:"]
        for name, preset in PROVIDER_PRESETS.items():
            marker = "*" if name == current else " "
            lines.append(f"{marker} {name:<10} {preset['description']} ({preset['base_url']})")
        lines.append("  custom      Any reachable OpenAI-compatible endpoint")
        return "\n".join(lines)

    action = args[0].lower()
    if action == "test":
        return _provider_test(jarvis, args[1:])
    if action == "models":
        return _provider_models(jarvis, args[1:])
    if action in {"use", "set"}:
        args = args[1:]
    if not args:
        return "Usage: /provider use NAME or /provider set URL [API_KEY_OR_ENV]"

    provider = args[0].lower()
    if provider == "custom" and action in {"use", ""}:
        return "Use /provider set URL [API_KEY_OR_ENV] to configure a custom endpoint."

    # Remember the model we were using so switching back restores it.
    previous_provider = jarvis.config["llm"].get("provider", "custom")
    remember_model(jarvis.config, previous_provider, jarvis.config["llm"].get("model", ""))

    if provider in PROVIDER_PRESETS and len(args) > 1:  # e.g. account_id=...
        if not all("=" in arg for arg in args[1:]):
            return f"Usage: /provider use {provider} [SETTING=VALUE ...]"
        try:
            provider_fields.save(provider, dict(arg.split("=", 1) for arg in args[1:]))
        except ValueError as error:
            return str(error)

    if provider in PROVIDER_PRESETS:
        preset = PROVIDER_PRESETS[provider]
        jarvis.config["llm"].update({
            "provider": provider,
            "base_url": preset["base_url"],
            "api_key_env": preset["api_key_env"],
            "api_key": None,  # presets authenticate via their env var
        })
    else:
        if action not in {"set", ""} and len(args) == 1:
            return f"Unknown provider preset: {provider}. Use /provider list."
        parsed = urlparse(args[0])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "Custom provider URL must start with http:// or https://."
        env_name, literal = _split_credentials(args[1] if len(args) > 1 else "")
        update = {
            "provider": "custom",
            "base_url": args[0],
        }
        if len(args) > 1:
            update["api_key_env"] = env_name or None
            update["api_key"] = literal
        jarvis.config["llm"].update(update)
        provider = "custom"
    jarvis.llm.reconfigure(jarvis.config)
    jarvis._emit("config_changed", section="llm", key="provider")

    # Restore this provider's own last-used model instead of carrying a
    # foreign one over (the classic "works elsewhere but 404s here" error).
    _restore_model(jarvis, provider, previous_provider)

    if provider == "chatgpt":
        from .providers.chatgpt_auth import get_chatgpt_auth
        save_runtime_overrides(jarvis.config, ["llm.provider", "llm.base_url", "llm.api_key_env", "llm.model"])
        summary = f"Provider changed to chatgpt (Sign in with ChatGPT)\nModel: {jarvis.config['llm']['model']}"
        if get_chatgpt_auth(jarvis.config).status()["signed_in"]:
            return summary + "\nSigned in. Use /model list to see available models."
        return summary + "\nNot signed in yet: run /login chatgpt (needs ChatGPT Plus or Pro)."

    summary = (
        f"Provider changed to {provider}: {jarvis.config['llm']['base_url']}\n"
        f"Model: {jarvis.config['llm']['model']}"
    )

    # Verify the endpoint right away so mistakes surface at config time.
    ok, message, ids, working_url = _test_connection(jarvis)
    if not ok:
        summary += f"\nConnection test: failed — {message}"
        if not ids and working_url != jarvis.config["llm"]["base_url"]:
            summary += f"\nTry setting the base URL to: {working_url}"
    else:
        if working_url != jarvis.config["llm"]["base_url"]:
            jarvis.config["llm"]["base_url"] = working_url
            jarvis.llm.reconfigure(jarvis.config)
            jarvis._emit("config_changed", section="llm", key="base_url")
            summary = (
                f"Provider changed to {provider}: {working_url}\n"
                f"Model: {jarvis.config['llm']['model']}"
            )
        summary += f"\nConnection test: OK — {message}"
    # Only persist provider-relevant keys; don't snapshot unrelated settings
    # that may be session-local (e.g. llm.timeout set but not yet saved).
    save_runtime_overrides(jarvis.config, ["llm.provider", "llm.base_url", "llm.api_key_env", "llm.model"])
    summary += f"\nSettings saved to {runtime_config_path(jarvis.config)} (provider + model)"
    return summary


def _restore_model(jarvis, provider, previous_provider):
    """Pick the model for *provider* after a switch.

    Order: the model last used with it, else the preset default. Switching to a
    different provider with neither resets the model to empty rather than
    keeping the previous provider's id (it would 404 here). Moving between
    local servers or from a custom endpoint onto a local server keeps it, since
    those serve whatever the user has loaded.
    """
    model = remembered_model(jarvis.config, provider) or PROVIDER_PRESETS.get(provider, {}).get("default_model")
    kind = lambda name: PROVIDER_PRESETS.get(name, {}).get("kind", "custom")  # noqa: E731
    if not model and provider != previous_provider and (kind(provider) != "local" or kind(previous_provider) not in {"local", "custom"}):
        model = ""
    if model is None or model == jarvis.config["llm"].get("model"):
        return
    jarvis.config["llm"]["model"] = model
    jarvis.llm.reconfigure(jarvis.config)
    jarvis._emit("config_changed", section="llm", key="model")


def _provider_test(jarvis, args):
    label = "current provider"
    if args:
        name = args[0].lower()
        if name not in PROVIDER_PRESETS:
            return f"Unknown provider preset: {name}. Use /provider list."
        preset = PROVIDER_PRESETS[name]
        ok, message, _ids, _ = _test_connection(
            jarvis, base_url=preset["base_url"], api_key_env=preset["api_key_env"], provider=name
        )
        label = f"{name} ({preset['base_url']})"
    else:
        ok, message, _ids, _ = _test_connection(jarvis)
    return f"Testing {label}\n{'OK' if ok else 'Failed'} — {message}"


def _provider_models(jarvis, args):
    if args:
        name = args[0].lower()
        if name not in PROVIDER_PRESETS:
            return f"Unknown provider preset: {name}. Use /provider list."
        preset = PROVIDER_PRESETS[name]
        ok, message, ids, _ = _test_connection(
            jarvis, base_url=preset["base_url"], api_key_env=preset["api_key_env"], provider=name
        )
    else:
        ok, message, ids, _ = _test_connection(jarvis)
    if not ok and not ids:
        return f"Could not list models: {message}"
    if not ids:
        return "The endpoint reports no models."
    return "Models:\n" + "\n".join(ids)


def _login(jarvis, args, blocking=False):
    """``/login chatgpt``: start the browser sign-in (see handle_slash_command)."""
    if not args or args[0].lower() != "chatgpt":
        return "Usage: /login chatgpt"
    from .providers.chatgpt_auth import get_chatgpt_auth
    auth = get_chatgpt_auth(jarvis.config)
    info = auth.start_login(open_browser=True)
    lines = ["Opening your browser to sign in with ChatGPT.", f"If it did not open, visit:\n{info['auth_url']}"]
    if not blocking:
        lines.append("Finish in the browser, then run /status to confirm.")
        return "\n".join(lines)
    print("\n".join(lines), flush=True)
    status = auth.wait(timeout=300)
    if status["signed_in"] and status["plan_ok"]:
        return f"Signed in to ChatGPT as {status['email'] or 'your account'}. Use /provider use chatgpt to switch."
    return f"ChatGPT sign-in did not complete: {status['error'] or 'timed out'}"


def _logout(jarvis, args):
    if not args or args[0].lower() != "chatgpt":
        return "Usage: /logout chatgpt"
    from .providers.chatgpt_auth import get_chatgpt_auth
    get_chatgpt_auth(jarvis.config).logout()
    return "Signed out of ChatGPT."


def _provider_for_url(base_url):
    """Non-chat preset (e.g. anthropic) whose URL matches, so probes use its backend."""
    for name, preset in PROVIDER_PRESETS.items():
        if preset["backend"] == "anthropic" and preset["base_url"] == base_url:
            return name
    return None


def _split_credentials(raw):
    """Return ``(api_key_env, api_key)`` for a credentials field value.

    A value that names a variable present in the environment is treated as
    an env var name; anything else is treated as a literal API key
    (session-local — never persisted by /settings save).
    """
    if not raw:
        return None, None
    if raw in os.environ:
        return raw, None
    return None, raw


def _test_connection(jarvis, base_url=None, api_key_env=None, api_key=None, timeout=5.0, check_model=True, provider=None,
                     fields=None):
    """Probe an endpoint and return ``(ok, message, model_ids, working_url)``.

    Probes the live client unless *base_url* (and optionally credentials)
    are given — used to inspect a preset or a draft config without applying
    it. When the probe 404s on a URL that lacks the ``/v1`` path, it retries
    with ``/v1`` appended and reports the URL that worked. With
    *check_model* (default) it also verifies the configured model against
    the endpoint; the provider manager passes ``check_model=False`` because
    there the model is picked from the listing afterwards.
    """
    working_url = base_url or jarvis.config["llm"]["base_url"]
    probe = jarvis.llm
    if provider is None and base_url is not None:
        provider = _provider_for_url(base_url)
    if base_url is not None or api_key_env or api_key or provider or fields:
        probe_config = copy.deepcopy(jarvis.config)
        if provider:
            probe_config["llm"]["provider"] = provider
        if fields:  # unsaved provider settings being tried out
            probe_config["llm"]["fields"] = fields
        if base_url is not None:
            probe_config["llm"]["base_url"] = base_url
        if api_key_env:
            probe_config["llm"]["api_key_env"] = api_key_env
            # Preset probe uses env var — clear any stale session-local literal
            # so literal doesn't win over env (LLM._resolve_key prefers literal)
            if not api_key:
                probe_config["llm"]["api_key"] = None
        if api_key:
            probe_config["llm"]["api_key"] = api_key
            # Literal probe (custom /provider set URL KEY) — clear env var
            # when caller supplied a literal but no env, to force literal use.
            if not api_key_env:
                probe_config["llm"]["api_key_env"] = None
        probe = LLM(probe_config)
    probe_kwargs = {"timeout": timeout}
    if not check_model:
        probe_kwargs["check_model"] = False
    ok, message, ids, status = probe.probe(**probe_kwargs)
    if (not ok and status == 404 and not ids and getattr(probe, "backend", "chat") == "chat"
            and not working_url.rstrip("/").endswith("/v1")):
        candidate = working_url.rstrip("/") + "/v1"
        probe_config = copy.deepcopy(jarvis.config)
        if provider:
            probe_config["llm"]["provider"] = provider
        probe_config["llm"]["base_url"] = candidate
        if api_key_env:
            probe_config["llm"]["api_key_env"] = api_key_env
            if not api_key:
                probe_config["llm"]["api_key"] = None
        if api_key:
            probe_config["llm"]["api_key"] = api_key
            if not api_key_env:
                probe_config["llm"]["api_key_env"] = None
        retry_ok, retry_message, retry_ids, _ = LLM(probe_config).probe(**probe_kwargs)
        if retry_ok:
            return True, retry_message, retry_ids, candidate
    return ok, message, ids, working_url


def _settings(jarvis, args):
    if not args:
        return _format_settings(jarvis)
    action = args[0].lower()
    if action == "save":
        target = save_runtime_overrides(jarvis.config, SETTING_PATHS)
        return f"Settings saved to {target}"
    if action in {"get", "show"}:
        if len(args) != 2:
            return "Usage: /settings get SECTION.KEY"
        return _read_setting(jarvis, args[1])
    if action == "reset":
        if len(args) != 2:
            return "Usage: /settings reset SECTION.KEY"
        path = args[1]
        if path not in SETTING_PATHS:
            return f"Unsupported setting: {path}"
        section, key = path.split(".", 1)
        jarvis.config[section][key] = copy.deepcopy(DEFAULTS[section][key])
        _apply_runtime_change(jarvis, path)
        return f"Reset {path} to {jarvis.config[section][key]}"
    if action == "set":
        if len(args) < 3:
            return "Usage: /settings set SECTION.KEY VALUE"
        path, raw = args[1], " ".join(args[2:])
    else:
        if len(args) < 2:
            return "Usage: /settings SECTION.KEY VALUE"
        path, raw = args[0], " ".join(args[1:])
    return _write_setting(jarvis, path, raw)


def _format_settings(jarvis):
    lines = ["Runtime settings (use /settings save to persist):"]
    for path in SETTING_PATHS:
        section, key = path.split(".", 1)
        lines.append(f"{path} = {jarvis.config[section].get(key)!r}")
    return "\n".join(lines)


def _read_setting(jarvis, path):
    if path not in SETTING_PATHS:
        return f"Unsupported setting: {path}"
    section, key = path.split(".", 1)
    return f"{path} = {jarvis.config[section].get(key)!r}"


def _write_setting(jarvis, path, raw):
    if path not in SETTING_PATHS:
        return f"Unsupported setting: {path}"
    section, key = path.split(".", 1)
    current = jarvis.config[section].get(key)
    value = _coerce(raw, current)
    if path == "llm.effort":
        from .providers.effort import normalize
        value = normalize(value)
    if path == "agent.tool_mode" and value not in ("search", "all"):
        return "agent.tool_mode must be 'search' or 'all'"
    if path == "agent.character":
        # Default for new sessions; /character use switches the current one.
        if not jarvis.store_characters.get(str(value)):
            return f"Unknown character: {value}. Use /character list."
    if path == "llm.provider":
        value = value.lower()
        if value not in set(PROVIDER_PRESETS) | {"custom"}:
            return f"Unsupported provider: {value}. Use /provider list."
        previous_provider = jarvis.config["llm"].get("provider", "custom")
        remember_model(jarvis.config, previous_provider, jarvis.config["llm"].get("model", ""))
        if value in PROVIDER_PRESETS:
            preset = PROVIDER_PRESETS[value]
            jarvis.config["llm"].update({
                "base_url": preset["base_url"],
                "api_key_env": preset["api_key_env"],
                "api_key": None,
            })
            jarvis.config["llm"]["provider"] = value
            _restore_model(jarvis, value, previous_provider)
            _apply_runtime_change(jarvis, path)
            return f"Set provider to {value}: {jarvis.config['llm']['base_url']} (model: {jarvis.config['llm']['model']})"
        else:
            jarvis.config["llm"]["provider"] = value
            _apply_runtime_change(jarvis, path)
            return f"Set provider to {value}: {jarvis.config['llm']['base_url']}"
    if path == "llm.api_key_env":
        # A named env var replaces any session-local literal key.
        jarvis.config["llm"]["api_key"] = None
    if path == "voice.engine" and value not in {"piper", "kokoro"}:
        return "voice.engine must be piper or kokoro."
    if path == "security.mode" and value not in {"guarded", "permissive"}:
        return "security.mode must be guarded or permissive."
    if isinstance(value, str) and key == "base_url":
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "llm.base_url must start with http:// or https://."
    if path in {"llm.timeout", "llm.retry_delay"} and value < 0:
        return f"{path} cannot be negative."
    if path in {
        "llm.retries", "llm.max_tokens", "agent.max_tool_rounds",
        "agent.history_char_limit", "agent.keep_recent",
        "security.max_command_timeout", "security.approval_timeout",
    } and value < 0:
        return f"{path} cannot be negative."
    jarvis.config[section][key] = value
    if path == "llm.model":
        remember_model(jarvis.config, jarvis.config["llm"].get("provider", "custom"), value)
    _apply_runtime_change(jarvis, path)
    return f"Set {path} = {value!r}"


def _coerce(raw, current):
    if isinstance(current, bool):
        value = raw.strip().lower()
        if value not in {"true", "false", "on", "off", "yes", "no", "1", "0"}:
            raise ValueError("Boolean values must be on/off, true/false, or yes/no")
        return value in {"true", "on", "yes", "1"}
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw)
        except ValueError as error:
            raise ValueError("Expected an integer") from error
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError as error:
            raise ValueError("Expected a number") from error
    return raw.strip()


def _apply_runtime_change(jarvis, path):
    if path.startswith("llm."):
        jarvis.llm.reconfigure(jarvis.config)
    elif path.startswith("voice."):
        jarvis.refresh_voice()
        return
    jarvis._emit("config_changed", section=path.split(".", 1)[0], key=path)


def _status(jarvis):
    llm = jarvis.config["llm"]
    voice = jarvis.config.get("voice", {})
    if llm.get("api_key"):
        key_line = "API key: literal (session-local)"
    elif llm.get("api_key_env"):
        key_line = f"API key: env {llm['api_key_env']}"
    else:
        key_line = "API key: not set"
    backend = getattr(jarvis.llm, "backend", "chat")
    lines = [
        f"Provider: {llm.get('provider', 'custom')}",
        f"Backend: {backend}",
        f"Endpoint: {llm.get('base_url')}",
        f"Model: {llm.get('model')}",
        key_line,
        f"Streaming: {'on' if llm.get('stream') else 'off'}",
        f"Voice: {'on' if voice.get('enabled') else 'off'} ({voice.get('engine', 'none')})",
        f"Session: {jarvis.session_id}",
    ]
    if backend == "chatgpt":
        from .providers.chatgpt_auth import get_chatgpt_auth
        st = get_chatgpt_auth(jarvis.config).status()
        who = st["email"] or "signed in" if st["signed_in"] else "not signed in (run /login chatgpt)"
        lines.append(f"ChatGPT: {who}")
    if getattr(jarvis.llm, "last_error", None):
        lines.append(f"Last error: {jarvis.llm.last_error}")
    lines.append("Run /provider test to verify the connection")
    return "\n".join(lines)
