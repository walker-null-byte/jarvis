"""Chat/model-output behaviour: inline <think> tags, Gemini thought signatures,
usage stats, readable errors, mid-stream Stop, retry and saved history."""

import copy
import json
from types import SimpleNamespace

import pytest

from jarvis.config import DEFAULTS
from jarvis.events import EventBus
from jarvis.llm import LLM, Cancelled, LLMError
from jarvis.providers.errors import explain_error
from jarvis.providers.openai_chat import GEMINI_DUMMY_SIGNATURE, clean_messages
from jarvis.providers.thinking import ThinkSplitter, split_all
from jarvis.webui.history import render_history
from tests.test_core_tools import ScriptedLLM, _agent, _tool_call

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def make_llm(provider="custom", base_url="http://localhost:1234/v1", stream=True, **extra):
    config = copy.deepcopy(DEFAULTS)
    config["llm"].update({"provider": provider, "base_url": base_url, "model": "m", "retries": 0,
                          "retry_delay": 0, "stream": stream, **extra})
    return LLM(config)


def chunk(content=None, reasoning=None, tool_calls=None, usage=None, choices=True):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [], reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)] if choices else [], usage=usage)


def tool_delta(index=0, id=None, name=None, arguments=None, extra=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    call = SimpleNamespace(index=index, id=id, function=function)
    if extra is not None:
        call.extra_content = extra
    return call


def client_returning(chunks, seen=None):
    def create(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        return iter(chunks)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


# ---- <think> tags -----------------------------------------------------------

def run_splitter(pieces):
    splitter = ThinkSplitter()
    out = []
    for piece in pieces:
        out += splitter.feed(piece)
    out += splitter.finish()
    reasoning = "".join(t for k, t in out if k == "reasoning")
    text = "".join(t for k, t in out if k == "text")
    return reasoning, text


@pytest.mark.parametrize("tag", ["think", "thinking", "thought", "THINK"])
def test_splitter_handles_tag_variants(tag):
    assert run_splitter([f"<{tag}>plan it</{tag}>\n\nAnswer"]) == ("plan it", "Answer")


def test_splitter_tags_split_across_chunks_char_by_char():
    source = "<think>step one\nstep two</think>\n\nThe **answer** is 4."
    assert run_splitter(list(source)) == ("step one\nstep two", "The **answer** is 4.")


def test_splitter_never_leaks_partial_tag_text():
    splitter = ThinkSplitter()
    first = splitter.feed("<thi")
    assert first == []
    assert splitter.feed("nk>abc</th") == [("reasoning", "abc")]
    assert splitter.feed("ink>done") == [("text", "done")]


def test_splitter_leaves_plain_text_and_mid_answer_mentions_alone():
    assert run_splitter(["Use the <think> tag like this."]) == ("", "Use the <think> tag like this.")
    assert run_splitter(["Hello <thinking>x</thinking>"]) == ("", "Hello <thinking>x</thinking>")
    assert run_splitter(["<b>bold</b> html"]) == ("", "<b>bold</b> html")


def test_splitter_unclosed_think_is_reasoning_and_short_text_flushes():
    assert run_splitter(["<think>never closed"]) == ("never closed", "")
    assert run_splitter(["<"]) == ("", "<")
    assert split_all("<think>a</think>b") == ("a", "b")


def test_streaming_think_tags_become_reasoning_and_stay_out_of_content():
    llm = make_llm()
    llm.client = client_returning([chunk("<thi"), chunk("nk>why"), chunk("</think>\n\nIt is "), chunk("42.")])
    text, reasoning = [], []
    msg = llm.chat([{"role": "user", "content": "q"}], on_text=text.append, on_reasoning=reasoning.append)
    assert msg["content"] == "It is 42."
    assert msg["reasoning"] == "why"
    assert "".join(text) == "It is 42." and "".join(reasoning) == "why"
    assert "<think>" not in json.dumps(msg)


def test_blocking_think_tags_and_reasoning_field_combine():
    llm = make_llm(stream=False)
    message = SimpleNamespace(content="<think>inline</think>final", tool_calls=None, reasoning_content="field ")
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    msg = llm.chat([{"role": "user", "content": "q"}])
    assert msg["content"] == "final" and msg["reasoning"] == "field inline"


# ---- Gemini thought signatures ---------------------------------------------

SIGNATURE = {"google": {"thought_signature": "c2ln"}}


def test_streaming_captures_thought_signature_on_tool_call():
    llm = make_llm("gemini", GEMINI_URL)
    llm.client = client_returning([
        chunk(tool_calls=[tool_delta(0, "call_a", "list_dir", "", extra=SIGNATURE)]),
        chunk(tool_calls=[tool_delta(0, arguments='{"path": "."}')]),
        chunk(tool_calls=[tool_delta(1, "call_b", "read_file", '{"path": "x"}')]),
    ])
    msg = llm.chat([{"role": "user", "content": "q"}])
    first, second = msg["tool_calls"]
    assert first["extra_content"] == SIGNATURE
    assert first["function"]["arguments"] == '{"path": "."}'
    assert "extra_content" not in second  # parallel calls: only the first carries one


def test_extra_content_in_pydantic_model_extra_is_found():
    from jarvis.providers.openai_chat import _extra_of
    call = SimpleNamespace(model_extra={"extra_content": SIGNATURE})
    assert _extra_of(call) == SIGNATURE


def test_blocking_captures_thought_signature():
    llm = make_llm("gemini", GEMINI_URL, stream=False)
    function = SimpleNamespace(name="list_dir", arguments="{}")
    call = SimpleNamespace(id="c1", function=function, extra_content=SIGNATURE)
    message = SimpleNamespace(content="", tool_calls=[call])
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    assert llm.chat([{"role": "user", "content": "q"}])["tool_calls"][0]["extra_content"] == SIGNATURE


def history_with_call(extra=None):
    call = {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
    if extra:
        call["extra_content"] = extra
    return [{"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [call], "reasoning": "r", "stats": {"x": 1}},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"}]


def test_signature_is_sent_back_unchanged_to_gemini():
    sent = clean_messages(history_with_call(SIGNATURE), gemini=True)
    assert sent[1]["tool_calls"][0]["extra_content"] == SIGNATURE


def test_missing_signature_gets_documented_dummy_for_gemini_only():
    gemini = clean_messages(history_with_call(), gemini=True)
    assert gemini[1]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": GEMINI_DUMMY_SIGNATURE}}
    other = clean_messages(history_with_call(SIGNATURE), gemini=False)
    assert "extra_content" not in other[1]["tool_calls"][0]


def test_dummy_only_added_to_first_parallel_call():
    messages = history_with_call()
    messages[1]["tool_calls"].append({"id": "c2", "type": "function", "function": {"name": "x", "arguments": "{}"}})
    calls = clean_messages(messages, gemini=True)[1]["tool_calls"]
    assert "extra_content" in calls[0] and "extra_content" not in calls[1]


def test_outgoing_messages_drop_jarvis_only_keys():
    sent = clean_messages(history_with_call(), gemini=False)
    assert set(sent[1]) == {"role", "content", "tool_calls"}
    assert "reasoning" not in json.dumps(sent) and "stats" not in json.dumps(sent)


def test_gemini_request_asks_for_thought_summaries_and_usage():
    seen = []
    llm = make_llm("gemini", GEMINI_URL)
    llm.client = client_returning([chunk("hi")], seen)
    llm.chat([{"role": "user", "content": "q"}])
    body = seen[0]
    assert body["extra_body"] == {"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}}
    assert body["stream_options"] == {"include_usage": True}


def test_non_gemini_request_has_no_google_fields():
    seen = []
    llm = make_llm()
    llm.client = client_returning([chunk("hi")], seen)
    llm.chat([{"role": "user", "content": "q"}])
    assert "extra_body" not in seen[0] and seen[0]["stream_options"] == {"include_usage": True}


def test_gemini_thought_tags_route_to_reasoning():
    llm = make_llm("gemini", GEMINI_URL)
    llm.client = client_returning([chunk("<thought>weighing options</thought>Go left.")])
    msg = llm.chat([{"role": "user", "content": "q"}])
    assert msg["reasoning"] == "weighing options" and msg["content"] == "Go left."


# ---- stream_options fallback -------------------------------------------------

class Rejected(Exception):
    def __init__(self, text, status=400):
        super().__init__(text)
        self.status_code = status
        self.body = {"error": {"message": text}}


def test_stream_options_rejection_retries_once_and_is_remembered():
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        if "stream_options" in kwargs:
            raise Rejected("Unrecognized request argument supplied: stream_options")
        return iter([chunk("ok")])

    llm = make_llm()
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert llm.chat([{"role": "user", "content": "q"}])["content"] == "ok"
    assert len(seen) == 2 and "stream_options" not in seen[1]
    llm.chat([{"role": "user", "content": "q"}])
    assert len(seen) == 3 and "stream_options" not in seen[2]  # remembered: no third failed attempt


def test_unrelated_400_is_not_retried():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise Rejected("model not found")

    llm = make_llm()
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "q"}])
    assert len(calls) == 1
    assert "stream_options" in calls[0]


def test_thought_signature_error_does_not_disable_optional_fields():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise Rejected("Function call is missing a thought_signature in functionCall parts")

    llm = make_llm("gemini", GEMINI_URL)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "q"}])
    assert len(calls) == 1


# ---- stats -------------------------------------------------------------------

def test_stats_use_reported_usage():
    llm = make_llm()
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=250)
    llm.client = client_returning([chunk("a"), chunk("b"), chunk(choices=False, usage=usage)])
    msg = llm.chat([{"role": "user", "content": "q"}])
    stats = msg["stats"]
    assert "usage" not in msg
    assert stats["output_tokens"] == 250 and stats["input_tokens"] == 100 and stats["approx"] is False
    assert stats["model"] == "m" and stats["seconds"] >= 0 and stats["ttft"] is not None


def test_stats_estimate_when_server_reports_nothing():
    llm = make_llm()
    llm.client = client_returning([chunk("x" * 40)])
    stats = llm.chat([{"role": "user", "content": "q"}])["stats"]
    assert stats["output_tokens"] == 10 and stats["approx"] is True


def test_stats_thinking_seconds_only_when_reasoning_happened():
    llm = make_llm()
    llm.client = client_returning([chunk(reasoning="hmm"), chunk("done")])
    assert llm.chat([{"role": "user", "content": "q"}])["stats"]["thinking_seconds"] is not None
    llm.client = client_returning([chunk("done")])
    assert llm.chat([{"role": "user", "content": "q"}])["stats"]["thinking_seconds"] is None


def test_anthropic_and_chatgpt_usage_are_captured():
    from jarvis.providers import anthropic_native as an
    final = SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=7, output_tokens=9),
                            content=[SimpleNamespace(type="text", text="hi", model_dump=lambda **k: {"type": "text", "text": "hi"})])
    assert an.final_to_message(final, "m")["usage"] == {"input": 7, "output": 9}

    from jarvis.providers.chatgpt import ChatGPTBackend
    backend = ChatGPTBackend({"model": "m"})
    done = SimpleNamespace(type="response.completed",
                           response=SimpleNamespace(usage=SimpleNamespace(input_tokens=3, output_tokens=5)))
    backend._consume(iter([SimpleNamespace(type="response.reasoning_summary_text.delta", delta="why"), done]),
                     [], [], None, None)
    assert backend._usage == {"input": 3, "output": 5} and backend._reasoning == ["why"]


# ---- readable errors ---------------------------------------------------------

def test_gemini_style_error_body_is_summarised():
    body = [{"error": {"code": 400, "message": "Function call is missing a thought_signature in functionCall parts."}}]
    error = Rejected("Error code: 400 - [{'error': {'code': 400, 'message': 'x'}}]")
    error.body = body
    message, status, details = explain_error(error, "Google Gemini", GEMINI_URL)
    assert status == 400
    assert message == ("Google Gemini returned HTTP 400: Function call is missing a thought_signature "
                       "in functionCall parts.")
    assert "Error code: 400" in details


def test_error_hints_and_connection_errors():
    message, status, _ = explain_error(Rejected("bad key", 401), "OpenAI API", "")
    assert status == 401 and "Check the API key" in message
    ConnectError = type("APIConnectionError", (Exception,), {})
    message, status, _ = explain_error(ConnectError("Connection error."), "LM Studio", "http://localhost:1234/v1")
    assert status is None and "localhost:1234" in message and "Error code" not in message


def test_facade_raises_llmerror_with_details_and_provider():
    def create(**kwargs):
        raise Rejected("nope", 403)

    llm = make_llm("openai", "https://api.openai.com/v1")
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(LLMError) as caught:
        llm.chat([{"role": "user", "content": "q"}])
    error = caught.value
    assert error.status == 403 and error.provider == "openai" and "nope" in error.details
    assert str(error).startswith("OpenAI API returned HTTP 403: nope")


# ---- agent: events, Stop, retry, history -------------------------------------

def collect(bus):
    events = []
    bus.subscribe(events.append)
    return events


def test_user_message_is_announced_before_turn_started(tmp_path):
    bus = EventBus()
    events = collect(bus)
    jarvis = _agent(tmp_path, ScriptedLLM({"role": "assistant", "content": "hi"}), bus)
    jarvis.ask("hello")
    kinds = [e.kind for e in events]
    assert kinds.index("user_message") < kinds.index("turn_started")


def test_stats_event_and_persistence_and_history_items(tmp_path):
    bus = EventBus()
    events = collect(bus)
    stats = {"model": "m", "output_tokens": 12, "approx": False, "tok_per_s": 30.0, "seconds": 0.4,
             "thinking_seconds": 2.0}
    reply = {"role": "assistant", "content": "ok", "reasoning": "pondering", "stats": stats}
    jarvis = _agent(tmp_path, ScriptedLLM(reply), bus)
    jarvis.ask("hi")
    assert [e.data["stats"] for e in events if e.kind == "assistant_stats"] == [stats]
    saved = jarvis.sessions.load(jarvis.session_id)[-1]
    assert saved["reasoning"] == "pondering" and saved["stats"] == stats
    items = render_history(saved_history := jarvis.sessions.load(jarvis.session_id))
    assistant = [i for i in items if i["type"] == "assistant"][0]
    assert assistant["reasoning"] == "pondering" and assistant["stats"] == stats
    assert saved_history  # reload path reads the same JSONL


def test_tool_only_message_keeps_reasoning_in_history():
    messages = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "", "reasoning": "need the folder", "stats": {"output_tokens": 3},
                 "tool_calls": [_tool_call("list_dir", {"path": "."})]},
                {"role": "tool", "tool_call_id": "c1", "content": "a b"}]
    items = render_history(messages)
    assert [i["type"] for i in items] == ["user", "assistant", "tool"]
    assert items[1]["reasoning"] == "need the folder" and items[1]["content"] == ""


class StreamingLLM:
    """Streams tokens through the callbacks like a real backend, raising whatever they raise."""

    def __init__(self, jarvis_cancel=None, tokens=("one ", "two ", "three ", "four ")):
        self.tokens = tokens
        self.cancel_after = jarvis_cancel
        self.requests = 0

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        self.requests += 1
        done = []
        for i, token in enumerate(self.tokens):
            if self.cancel_after is not None and i == self.cancel_after:
                self.cancel()
            on_text(token)
            done.append(token)
        return {"role": "assistant", "content": "".join(done)}


def test_stop_interrupts_streaming_and_keeps_partial_text(tmp_path):
    bus = EventBus()
    events = collect(bus)
    llm = StreamingLLM(jarvis_cancel=2)
    jarvis = _agent(tmp_path, llm, bus)
    llm.cancel = jarvis.cancel
    reply = jarvis.ask("count")
    assert reply == "one two"
    assert jarvis.history[-1] == {"role": "assistant", "content": "one two"}
    finished = [e.data["status"] for e in events if e.kind == "turn_finished"]
    assert finished == ["cancelled"] and not jarvis.busy
    assert not [e for e in events if e.kind == "error"]


def test_retry_after_error_does_not_duplicate_the_user_message(tmp_path):
    class Flaky:
        calls = 0

        def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise LLMError("boom", details="raw text", status=500)
            return {"role": "assistant", "content": "recovered"}

    bus = EventBus()
    events = collect(bus)
    jarvis = _agent(tmp_path, Flaky(), bus)
    with pytest.raises(LLMError):
        jarvis.ask("please")
    error = [e for e in events if e.kind == "error"][0]
    assert error.data["message"] == "boom" and error.data["details"] == "raw text" and error.data["status"] == 500
    assert jarvis.retry_last_turn() == "recovered"
    assert [m["role"] for m in jarvis.history] == ["user", "assistant"]
    assert [e.kind for e in events].count("user_message") == 1


def test_cancelled_is_not_swallowed_by_retry_logic():
    llm = make_llm(retries=3)

    def create(**kwargs):
        raise Cancelled()

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(Cancelled):
        llm.chat([{"role": "user", "content": "q"}])


def test_custom_server_errors_are_named_by_host():
    def create(**kwargs):
        raise Rejected("no such model", 404)

    llm = make_llm("custom", "http://10.0.0.5:8080/v1")
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(LLMError) as caught:
        llm.chat([{"role": "user", "content": "q"}])
    assert str(caught.value).startswith("The server at 10.0.0.5:8080 returned HTTP 404: no such model.")
