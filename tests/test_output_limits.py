"""Stop reasons, recovery from a cut-off reply, the output cap fallback and the Thinking setting."""

import json
from types import SimpleNamespace

import pytest

from jarvis import config as cfg
from jarvis.config import DEFAULTS, load_config
from jarvis.events import EventBus
from jarvis.providers import anthropic_native as an
from jarvis.providers import effort as eff
from jarvis.providers.openai_chat import parse_token_cap
from tests.test_chat_ux import GEMINI_URL, Rejected, chunk, client_returning, make_llm
from tests.test_harness import _agent
from tests.test_core_tools import _calls, _tool_call
from tests.test_providers import _chatgpt_llm, _ev

HISTORY = [{"role": "user", "content": "hi"}]


def stopped(chunk_, reason):
    chunk_.choices[0].finish_reason = reason
    return chunk_


# ---- finish_reason capture ---------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("stop", "end"), ("length", "length"), ("tool_calls", "tool_calls"),
                                          ("content_filter", "content_filter"), ("weird", "weird")])
def test_streaming_reports_stop(raw, expected):
    llm = make_llm()
    llm.client = client_returning([chunk("a"), stopped(chunk("b"), raw), chunk(usage=None, choices=False)])
    assert llm.chat(HISTORY)["stop"] == expected


def test_blocking_reports_stop_and_missing_is_absent():
    llm = make_llm(stream=False)
    message = SimpleNamespace(content="x", tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="length")], usage=None)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    assert llm.chat(HISTORY)["stop"] == "length"
    llm = make_llm()
    llm.client = client_returning([chunk("x")])
    assert "stop" not in llm.chat(HISTORY)


def test_stop_is_not_sent_back_to_the_api():
    seen = []
    llm = make_llm()
    llm.client = client_returning([chunk("x")], seen)
    llm.chat([{"role": "user", "content": "q"}, {"role": "assistant", "content": "a", "stop": "length"}])
    assert all("stop" not in m for m in seen[0]["messages"])


def test_anthropic_stop_reasons():
    block = lambda **kw: SimpleNamespace(model_dump=lambda exclude_none=True: dict(kw), **kw)  # noqa: E731
    text = [block(type="text", text="hi")]
    for raw, expected in [("end_turn", "end"), ("tool_use", "tool_calls"), ("max_tokens", "length"),
                          ("pause_turn", "pause_turn")]:
        msg = an.final_to_message(SimpleNamespace(stop_reason=raw, content=text), "claude-opus-5-5")
        assert msg["stop"] == expected
    refusal = an.final_to_message(SimpleNamespace(stop_reason="refusal", stop_details=None, content=None), "m")
    assert refusal["stop"] == "refusal"


def test_chatgpt_stop_reasons():
    done = _chatgpt_llm([_ev("response.output_text.delta", delta="x"), _ev("response.completed", response=None)])
    assert done.chat(HISTORY)["stop"] == "end"
    cut = SimpleNamespace(incomplete_details=SimpleNamespace(reason="max_output_tokens"))
    llm = _chatgpt_llm([_ev("response.output_text.delta", delta="part"), _ev("response.incomplete", response=cut)])
    msg = llm.chat(HISTORY)
    assert msg["stop"] == "length" and msg["content"] == "part"


# ---- agent loop --------------------------------------------------------------

class LengthLLM:
    """Replies cut off `cuts` times, then finishes; remembers the limit of each request."""

    def __init__(self, cuts, limit=4096, calls=False):
        self.cuts, self.limit, self.requests, self.with_calls = cuts, limit, [], calls

    def output_limit(self, override=None):
        return override or self.limit

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None, max_tokens=None):
        self.requests.append(max_tokens)
        if len(self.requests) <= self.cuts:
            on_reasoning("pondering")
            msg = {"role": "assistant", "content": "", "reasoning": "pondering", "stop": "length"}
            if self.with_calls:
                msg["tool_calls"] = [_tool_call("list_dir", {"path": '"half'}, "c1")]
            return msg
        on_text("done")
        return {"role": "assistant", "content": "done", "stop": "end"}


def run(tmp_path, llm):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    jarvis = _agent(tmp_path, llm, bus)
    return jarvis, jarvis.ask("go"), events


def test_length_retries_once_with_double_limit(tmp_path):
    llm = LengthLLM(cuts=1)
    jarvis, reply, events = run(tmp_path, llm)
    assert reply == "done" and llm.requests == [None, 8192]
    note = next(e for e in events if e.kind == "output_truncated")
    assert note.data == {"limit": 4096, "new_limit": 8192, "retrying": True}
    assert [m["content"] for m in jarvis.history if m["role"] == "assistant"] == ["done"]  # cut-off reply discarded
    assert [e.data["status"] for e in events if e.kind == "turn_finished"] == ["ok"]


def test_retry_limit_is_capped(tmp_path):
    llm = LengthLLM(cuts=1, limit=40000)
    run(tmp_path, llm)
    assert llm.requests == [None, 65536]
    llm = LengthLLM(cuts=5, limit=65536)  # already at the ceiling: no retry, straight to the message
    _, reply, _ = run(tmp_path, llm)
    assert len(llm.requests) == 1 and "65,536" in reply


def test_still_cut_off_ends_turn_with_message_and_status(tmp_path):
    llm = LengthLLM(cuts=2)
    jarvis, reply, events = run(tmp_path, llm)
    assert llm.requests == [None, 8192]
    assert "ran out of output tokens (limit 8,192)" in reply and "Max output" in reply and "Continue" in reply
    finished = next(e for e in events if e.kind == "turn_finished")
    assert finished.data["status"] == "length" and finished.data["limit"] == 8192  # the larger, retried limit
    last = jarvis.history[-1]
    assert last["role"] == "assistant" and last["content"].endswith("press Continue.") and last["stop"] == "length"
    assert not jarvis.busy


def test_truncated_tool_call_is_never_run(tmp_path):
    ran = tmp_path / "x"
    llm = LengthLLM(cuts=2, calls=True)
    jarvis, reply, _ = run(tmp_path, llm)
    assert not any(m.get("tool_calls") for m in jarvis.history)
    assert not any(m["role"] == "tool" for m in jarvis.history) and not ran.exists()
    assert "ran out of output tokens" in reply


def test_fake_backends_without_max_tokens_support_still_work(tmp_path):
    from tests.test_core_tools import ScriptedLLM
    jarvis = _agent(tmp_path, ScriptedLLM(_calls(_tool_call("list_dir", {"path": "."}, "c1")), {"content": "ok"}))
    assert jarvis.ask("hi") == "ok"


# ---- output cap rejection ----------------------------------------------------

def test_parse_token_cap():
    assert parse_token_cap("max_tokens must be at most 8192", 16384) == 8192
    assert parse_token_cap("Invalid max_tokens: maximum allowed is 32,768", 65536) == 32768
    assert parse_token_cap("max_tokens is too large", 16384) == 4096
    assert parse_token_cap("max_tokens is too large; limit is 100000", 16384) == 4096  # not below the request


def test_max_tokens_rejection_retries_with_named_cap_and_remembers():
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        if kwargs["max_tokens"] > 8192:
            raise Rejected("max_tokens must be at most 8192 for this model")
        return iter([chunk("ok")])

    llm = make_llm(max_tokens=16384)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert llm.chat(HISTORY)["content"] == "ok"
    assert [s["max_tokens"] for s in seen] == [16384, 8192]
    assert llm.output_limit() == 8192
    llm.chat(HISTORY, max_tokens=32768)  # a bigger ask is clamped to the remembered cap
    assert [s["max_tokens"] for s in seen][-1] == 8192 and len(seen) == 3


def test_max_tokens_rejection_without_number_falls_back_to_4096():
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        if kwargs["max_tokens"] > 4096:
            raise Rejected("Invalid value for max_tokens")
        return iter([chunk("ok")])

    llm = make_llm(max_tokens=16384)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    llm.chat(HISTORY)
    assert [s["max_tokens"] for s in seen] == [16384, 4096]


def test_openai_wants_max_completion_tokens():
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        if "max_tokens" in kwargs:
            raise Rejected("Unsupported parameter: 'max_tokens' is not supported with this model. "
                           "Use 'max_completion_tokens' instead.")
        return iter([chunk("ok")])

    llm = make_llm("openai", "https://api.openai.com/v1")
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    llm.chat(HISTORY)
    assert "max_completion_tokens" in seen[1] and "max_tokens" not in seen[1]
    llm.chat(HISTORY)
    assert len(seen) == 3 and "max_completion_tokens" in seen[2]


# ---- Thinking (effort) mapping ----------------------------------------------

def request(provider, base_url, effort, model="m", **extra):
    seen = []
    llm = make_llm(provider, base_url, effort=effort, **extra)
    llm._impl.model = model
    llm.client = client_returning([chunk("x")], seen)
    llm.chat(HISTORY)
    return seen[0]


def test_auto_sends_nothing_extra():
    body = request("openai", "https://api.openai.com/v1", "")
    assert "reasoning_effort" not in body and "extra_body" not in body


def test_generic_servers_get_reasoning_effort():
    for provider, url in [("lmstudio", "http://localhost:1234/v1"), ("groq", "https://api.groq.com/openai/v1"),
                          ("vllm", "http://localhost:8000/v1")]:
        assert request(provider, url, "high")["reasoning_effort"] == "high"
        assert request(provider, url, "off")["reasoning_effort"] == "none"


def test_cloudflare_uses_the_generic_field():
    assert eff.chat_fields("cloudflare", "https://api.cloudflare.com/x", "@cf/m", "medium") == (
        {"reasoning_effort": "medium"}, {})


def test_openai_off_depends_on_model_family():
    url = "https://api.openai.com/v1"
    assert request("openai", url, "off", model="gpt-5.2")["reasoning_effort"] == "none"
    assert request("openai", url, "off", model="gpt-5-mini")["reasoning_effort"] == "minimal"
    assert request("openai", url, "off", model="o4-mini")["reasoning_effort"] == "low"
    assert request("openai", url, "xhigh")["reasoning_effort"] == "xhigh"


def test_gemini_effort_coexists_with_thought_summaries():
    body = request("gemini", GEMINI_URL, "medium", model="gemini-2.5-flash")
    assert body["reasoning_effort"] == "medium"
    assert body["extra_body"] == {"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}}
    off = request("gemini", GEMINI_URL, "off", model="gemini-2.5-flash")
    assert off["reasoning_effort"] == "none" and "extra_body" not in off
    assert request("gemini", GEMINI_URL, "off", model="gemini-3-pro")["reasoning_effort"] == "minimal"
    assert request("gemini", GEMINI_URL, "max")["reasoning_effort"] == "high"


def test_deepseek_thinking_toggle_and_levels():
    url = "https://api.deepseek.com/v1"
    off = request("deepseek", url, "off")
    assert off["extra_body"] == {"thinking": {"type": "disabled"}} and "reasoning_effort" not in off
    med = request("deepseek", url, "medium")
    assert med["reasoning_effort"] == "high" and med["extra_body"] == {"thinking": {"type": "enabled"}}
    assert request("deepseek", url, "low")["reasoning_effort"] == "low"
    assert request("deepseek", url, "max")["reasoning_effort"] == "max"


def test_openrouter_reasoning_object():
    url = "https://openrouter.ai/api/v1"
    assert request("openrouter", url, "low")["extra_body"] == {"reasoning": {"effort": "low"}}
    assert request("openrouter", url, "off")["extra_body"] == {"reasoning": {"enabled": False}}
    assert "reasoning_effort" not in request("openrouter", url, "high")


def test_chatgpt_reasoning_and_drop_on_rejection():
    calls = []
    llm = _chatgpt_llm([_ev("response.completed", response=None)], calls)
    llm._impl.effort = "high"
    llm.chat(HISTORY)
    assert calls[0]["reasoning"] == {"effort": "high"}
    llm._impl.effort = "off"
    llm.chat(HISTORY)
    assert calls[1]["reasoning"] == {"effort": "none"}

    attempts = []

    def create(**kwargs):
        attempts.append(kwargs)
        if "reasoning" in kwargs:
            raise Rejected("Unsupported value: 'reasoning.effort' does not support 'none' with this model")
        return iter([_ev("response.completed", response=None)])

    llm._impl._client = lambda: SimpleNamespace(responses=SimpleNamespace(create=create))
    llm.chat(HISTORY)
    llm.chat(HISTORY)
    assert [("reasoning" in a) for a in attempts] == [True, False, False]


def test_rejected_reasoning_effort_is_dropped_and_remembered():
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        if "reasoning_effort" in kwargs:
            raise Rejected("Unrecognized request argument supplied: reasoning_effort")
        return iter([chunk("ok")])

    llm = make_llm("lmstudio", "http://localhost:1234/v1", effort="low")
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert llm.chat(HISTORY)["content"] == "ok"
    llm.chat(HISTORY)
    assert ["reasoning_effort" in s for s in seen] == [True, False, False]
    assert "stream_options" in seen[1]  # only the refused field was dropped


def test_anthropic_effort():
    def build(effort):
        backend = an.AnthropicBackend({**DEFAULTS["llm"], "model": "claude-opus-5-5", "effort": effort})
        return backend.build_request(HISTORY)

    assert "output_config" not in build("") and "thinking" in build("")
    assert build("high")["output_config"] == {"effort": "high"}
    off = build("off")
    assert "thinking" not in off and "output_config" not in off


def test_normalize():
    assert eff.normalize(" High ") == "high" and eff.normalize("none") == "off" and eff.normalize("bogus") == ""


# ---- config ------------------------------------------------------------------

def test_default_max_tokens_and_upgrade_of_old_default(monkeypatch, tmp_path):
    assert DEFAULTS["llm"]["max_tokens"] == 16384
    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    (tmp_path / "mind").mkdir()
    (tmp_path / "mind" / "runtime_config.json").write_text(json.dumps({"llm": {"max_tokens": 4096}}))
    assert load_config()["llm"]["max_tokens"] == 16384
    (tmp_path / "mind" / "runtime_config.json").write_text(json.dumps({"llm": {"max_tokens": 8192}}))
    assert load_config()["llm"]["max_tokens"] == 8192
    (tmp_path / "mind" / "runtime_config.json").unlink()
    (tmp_path / "config.toml").write_text("[llm]\nmax_tokens = 4096\n")
    assert load_config()["llm"]["max_tokens"] == 16384

