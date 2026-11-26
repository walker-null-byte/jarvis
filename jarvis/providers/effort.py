"""Reasoning effort ("Thinking") as one setting, translated per provider.

``llm.effort`` is "" (Auto: the model's own default), "off", "low", "medium",
"high", or one of the extra levels some providers accept ("minimal", "xhigh", "max").
Servers differ a lot, so every field built here is *optional*: openai_chat sends it,
and if the server rejects it, drops it and remembers (see ``ChatBackend.dropped``).

Sources (official docs, read Nov 2026):
  OpenAI      reasoning_effort: none|minimal|low|medium|high|xhigh (platform.openai.com, Chat API reference)
  Gemini      reasoning_effort: minimal|low|medium|high; "none" only on 2.5 Flash-class models
              (aistudio.google.com/docs/openai)
  DeepSeek    extra_body {"thinking": {"type": "enabled"|"disabled"}} plus reasoning_effort low|high|max;
              minimal->low, medium->high, xhigh->high (api-docs.deepseek.com/guides/thinking_mode)
  OpenRouter  reasoning: {"effort": ...} or {"enabled": false} (openrouter.ai/docs/guides/best-practices/reasoning-tokens)
Not verified against docs: Workers AI, Groq, Mistral, xAI, LM Studio/Ollama/vLLM. They get the
OpenAI-style ``reasoning_effort`` and drop it if refused.
"""

LEVELS = ("low", "medium", "high")
VALID = ("", "off", "minimal", "low", "medium", "high", "xhigh", "max")


def normalize(value):
    """A valid effort string, or "" for anything unknown. "none" means off."""
    text = str(value or "").strip().lower()
    if text in ("none", "disabled", "false"):
        text = "off"
    return text if text in VALID else ""


def _openai_off(model):
    """The lowest setting that exists for this OpenAI model family."""
    name = (model or "").lower()
    if name.startswith("o") and name[1:2].isdigit():
        return "low"  # o-series reasoning cannot be switched off
    if name.startswith("gpt-5") and not name.startswith(("gpt-5.", "gpt-5-chat")):
        return "minimal"  # gpt-5 / mini / nano / codex predate "none"
    return "none"


def _gemini_off(model):
    name = (model or "").lower().removeprefix("models/")
    return "none" if "2.5" in name and "pro" not in name else "minimal"


def chat_fields(provider, base_url, model, effort):
    """``(top_level_kwargs, extra_body)`` for a Chat Completions request."""
    effort = normalize(effort)
    if not effort:
        return {}, {}
    url = str(base_url or "")
    if "generativelanguage.googleapis.com" in url:
        return {"reasoning_effort": _gemini_off(model) if effort == "off" else _clamp(effort, "gemini")}, {}
    if provider == "deepseek" or "api.deepseek.com" in url:
        if effort == "off":
            return {}, {"thinking": {"type": "disabled"}}
        level = {"minimal": "low", "low": "low", "medium": "high", "high": "high",
                 "xhigh": "max", "max": "max"}[effort]
        return {"reasoning_effort": level}, {"thinking": {"type": "enabled"}}
    if provider == "openrouter" or "openrouter.ai" in url:
        if effort == "off":
            return {}, {"reasoning": {"enabled": False}}
        return {}, {"reasoning": {"effort": effort}}
    if provider == "openai" or "api.openai.com" in url:
        return {"reasoning_effort": _openai_off(model) if effort == "off" else effort}, {}
    return {"reasoning_effort": "none" if effort == "off" else effort}, {}


def _clamp(effort, family):
    if family == "gemini" and effort in ("xhigh", "max"):
        return "high"
    return effort


def responses_reasoning(model, effort):
    """The ``reasoning`` object for the Responses API (ChatGPT plan), or None."""
    effort = normalize(effort)
    if not effort:
        return None
    return {"effort": "none" if effort == "off" else ("xhigh" if effort == "max" else effort)}
