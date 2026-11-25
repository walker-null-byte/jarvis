"""Provider presets and backend modules.

This file is pure data on purpose: config.py and commands.py import PRESETS
without pulling in any SDK. Each preset names a backend:

- "chat":      OpenAI-compatible Chat Completions (openai_chat.py)
- "chatgpt":   Sign in with ChatGPT -> Responses API (chatgpt.py)
- "anthropic": official anthropic SDK (anthropic_native.py)
"""

_LOCAL_KEY = "JARVIS_API_KEY"


def _chat(base_url, api_key_env, description, label, kind, order, default_model=""):
    preset = {
        "label": label,
        "kind": kind,  # "subscription" | "local" | "api_key"
        "order": order,  # display order: chatgpt, local presets, then API-key ones
        "backend": "chat",
        "base_url": base_url,
        "api_key_env": api_key_env,
        "description": description,
    }
    if default_model:  # local servers serve whatever is loaded: no default
        preset["default_model"] = default_model
    return preset


PRESETS = {
    "lmstudio": _chat("http://localhost:1234/v1", _LOCAL_KEY, "LM Studio local OpenAI-compatible server",
        "LM Studio — local server", "local", 10),
    "ollama": _chat("http://localhost:11434/v1", _LOCAL_KEY, "Ollama OpenAI-compatible endpoint", "Ollama — local server", "local", 11),
    "llama.cpp": _chat("http://localhost:8080/v1", _LOCAL_KEY, "llama.cpp server endpoint", "llama.cpp — local server", "local", 12),
    "vllm": _chat("http://localhost:8000/v1", _LOCAL_KEY, "vLLM OpenAI-compatible server", "vLLM — local server", "local", 13),
    "openai": _chat("https://api.openai.com/v1", "OPENAI_API_KEY", "OpenAI API",
        "OpenAI API — pay-as-you-go API key", "api_key", 20, "gpt-5"),
    "gemini": _chat(
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GEMINI_API_KEY",
        "Google Gemini OpenAI-compatible endpoint",
        "Google Gemini — API key", "api_key", 21, "gemini-2.5-flash",
    ),
    "openrouter": _chat("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OpenRouter (many models, one key)",
        "OpenRouter — API key (many models)", "api_key", 22, "openai/gpt-4o-mini"),
    "groq": _chat("https://api.groq.com/openai/v1", "GROQ_API_KEY", "Groq fast inference", "Groq — API key", "api_key", 23, "llama-3.3-70b-versatile"),
    "deepseek": _chat("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "DeepSeek API", "DeepSeek — API key", "api_key", 24, "deepseek-chat"),
    "mistral": _chat("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "Mistral API", "Mistral — API key", "api_key", 25, "mistral-large-latest"),
    "xai": _chat("https://api.x.ai/v1", "XAI_API_KEY", "xAI Grok API", "xAI Grok — API key", "api_key", 26, "grok-4"),
    "chatgpt": {
        "label": "ChatGPT — sign in with your Plus/Pro plan",
        "kind": "subscription",
        "order": 0,
        "backend": "chatgpt",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "",
        "description": "Sign in with ChatGPT (uses your Plus/Pro plan)",
    },
    "anthropic": {
        "label": "Anthropic Claude — API key",
        "kind": "api_key",
        "order": 27,
        "backend": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
        "description": "Anthropic Claude (native API)",
        "default_model": "claude-opus-5-5",
    },
}


def backend_for(provider):
    """Backend name for a provider id; unknown/custom providers use "chat"."""
    return PRESETS.get(provider, {}).get("backend", "chat")
