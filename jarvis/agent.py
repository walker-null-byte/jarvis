"""The agent loop: user message -> LLM -> tools -> ... -> reply.

Every step is logged to mind/episodes/ as it happens.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from .approvals import ApprovalManager
from .audit import AuditLog
from .characters import CharacterStore
from .config import load_config
from .context import build_system_prompt, ensure_mind
from .events import Event, EventBus, new_id
from .file_tools import SPILL_THRESHOLD, prune_spills, spill_output
from .llm import LLM, Cancelled
from .memory import EpisodeLog
from .redaction import redact_text, redact_value
from .registry import get_registry
from . import toolctx
from .sessions import MemorySessions, SessionStore
from .tool_exposure import ToolExposure, enable_semantic_search
from .voice import get_voice

NO_SPILL_TOOLS = {"read_file"}  # already paged by offset/limit; spilling it would be circular
SUMMARY_PREFIX = "[Summary of the earlier conversation]"

SUMMARIZE_PROMPT = """\
Compress this conversation transcript into compact bullet points. Preserve
everything needed to continue seamlessly: open tasks and their state, decisions
made, facts learned, file paths, commands that worked or failed, and what the
user asked for most recently. Drop pleasantries and dead ends. Output only the
bullet points.

Transcript:
"""


def _card_text(text, limit=2000):
    """What the chat card shows: the start and the end (where a spill note lives)."""
    return text if len(text) <= limit else text[:limit - 600] + "\n[...]\n" + text[-500:]


class Jarvis:
    def __init__(self, config=None, session_id=None, event_bus=None, profile=None):
        self.config = config or load_config()
        self.profile = profile
        if profile is not None:
            # Own config copy so this agent's LLM/limits never leak into others.
            from .runtime import apply_profile_config
            self.config = apply_profile_config(self.config, profile)
        self.mind = ensure_mind(self.config)
        self.episodes = EpisodeLog(self.mind)
        self.audit = AuditLog(self.mind)
        self.llm = LLM(self.config)
        self.events = event_bus or EventBus()
        self.approvals = ApprovalManager(self.events)
        self.registry = get_registry(self.config)
        self._cancel_event = threading.Event()
        self.registry.set_approval_handler(self.approvals)
        self.registry.set_cancel_event(self._cancel_event)
        self.approvals.set_cancel_event(self._cancel_event)
        persist = profile is None or profile.persist
        self.sessions = SessionStore(self.mind) if persist else MemorySessions()
        self.store_characters = CharacterStore(self.mind)
        self.character = self.config["agent"]["character"]
        if session_id:
            self.session_id = session_id
            self.history = self.sessions.load(session_id)
            self.character = self._stored_character(session_id)
        else:
            self.session_id = self._create_session()
            self.history = []
        enable_semantic_search(self.registry, self.config)
        self.exposure = ToolExposure(self.registry, self.config, self.mind, profile)
        self.voice = get_voice(self.config)
        self._active_turn = ""
        self._spill_count = 0
        self._spill_lock = threading.Lock()
        self._start_harness_services()
        self._emit("session_started", session_id=self.session_id, session_kind=self.kind)
        self.audit.log("session", self.session_id, action="session_started", status="ok")

    def _start_harness_services(self):
        """Housekeeping that must never block or break startup: old spill files, MCP servers."""
        try:
            prune_spills(self.mind)
            from . import mcp_client
            mcp_client.attach(self.registry, self.mind, self.events)
        except Exception as e:
            self._emit("error", message=f"Harness startup problem: {e}")

    def _create_session(self):
        p = self.profile
        if p is None:
            return self.sessions.create(character=self.character)
        return self.sessions.create(p.title or "New conversation", self.character, p.kind, p.owner)

    @property
    def kind(self):
        """Session kind: "chat" for the main chat, else the profile's kind."""
        return self.profile.kind if self.profile else "chat"

    @property
    def busy(self):
        """True while a turn is running."""
        return bool(self._active_turn)

    def _stored_character(self, session_id):
        """The session's saved card id, or 'jarvis' if it no longer exists."""
        card_id = self.sessions.get_character(session_id) or self.config["agent"]["character"]
        return card_id if self.store_characters.get(card_id) else "jarvis"

    def set_character(self, card_id):
        """Switch this session's persona. Returns {"character": summary} or {"error": ...}."""
        if not self.store_characters.get(card_id):
            return {"error": f"Unknown character: {card_id}"}
        self.character = card_id
        self.sessions.set_character(self.session_id, card_id)
        summary = next((c for c in self.store_characters.list() if c["id"] == card_id), {"id": card_id})
        self._emit("character_changed", character=summary)
        return {"character": summary}

    def _build_prompt(self):
        return build_system_prompt(
            self.mind,
            character=self.store_characters.get(self.character),
            tools_section=self.exposure.prompt_section(),
            memory_chars=self.config["agent"]["memory_index_chars"],
            role_prompt=self.profile.system_prompt if self.profile else "",
            replace_persona=bool(self.profile and self.profile.replace_persona),
        )

    def ask(self, user_text, on_text=None, on_tool=None, on_tool_result=None, on_reasoning=None):
        """Run one full turn. Returns the final reply text.

        on_text(token) streams reply fragments; on_tool(name, args) fires
        before each tool execution.
        """
        self._cancel_event.clear()
        try:
            self._maybe_summarize()
        except Exception as e:
            # Summarization is best-effort; never abort the user turn.
            self._emit("error", message=f"Summarization failed: {e}")
        user_message = {"role": "user", "content": user_text}

        def record_user_message():
            # Announced before turn_started so the browser shows your message first.
            self.episodes.log("user", user_text)
            self.history.append(user_message)
            self.sessions.append(self.session_id, user_message)
            self._emit("user_message", turn_id=self._active_turn, content=user_text)

        turn_id = self._begin_turn(user_text, before=record_user_message)
        return self._run_turn(turn_id, on_text, on_tool, on_tool_result, on_reasoning)

    def _begin_turn(self, user_text, before=None):
        turn_id = new_id("turn")
        self._active_turn = turn_id
        if before:
            before()
        self._emit("turn_started", turn_id=turn_id)
        self.audit.log("turn", self.session_id, turn_id, "turn_started", "running", {"content": user_text})
        return turn_id

    def _run_turn(self, turn_id, on_text=None, on_tool=None, on_tool_result=None, on_reasoning=None):
        """The model/tool loop for a turn whose user message is already last in history."""
        # Tools and system prompt are fixed for the whole turn (cache stability).
        self.exposure.refresh()
        tools = self.exposure.tools()
        system_prompt = self._build_prompt()
        partial = {"text": [], "reasoning": []}  # what the current model call streamed so far

        def emit_reasoning(token):
            if self._cancel_event.is_set():
                raise Cancelled()  # Stop takes effect mid-stream, not at the next round
            partial["reasoning"].append(token)
            self._emit("assistant_reasoning_delta", turn_id=turn_id, content=token)
            if on_reasoning:
                on_reasoning(token)

        def emit_text(token):
            if self._cancel_event.is_set():
                raise Cancelled()
            partial["text"].append(token)
            self._emit("assistant_delta", turn_id=turn_id, content=token)
            if on_text:
                on_text(token)

        try:
            rounds = (self.profile.max_tool_rounds if self.profile and self.profile.max_tool_rounds
                      else self.config["agent"]["max_tool_rounds"])
            for _ in range(rounds):
                if self._cancel_event.is_set():
                    return self._finish_cancelled(turn_id)
                messages = [{"role": "system", "content": system_prompt}]
                messages += self.history
                partial["text"].clear()
                partial["reasoning"].clear()
                try:
                    msg = self._chat_turn(messages, turn_id, tools=tools, on_text=emit_text,
                                          on_reasoning=emit_reasoning)
                except Cancelled:
                    return self._finish_cancelled(turn_id, "".join(partial["text"]),
                                                  "".join(partial["reasoning"]))
                self._record_assistant(msg, turn_id)

                calls = msg.get("tool_calls")
                if not calls:
                    reply = msg.get("content") or ""
                    self.episodes.log("jarvis", reply)
                    self._emit("assistant_message", turn_id=turn_id, content=reply)
                    self._emit("turn_finished", turn_id=turn_id, status="ok")
                    self.audit.log("turn", self.session_id, turn_id, "turn_finished", "ok")
                    self._active_turn = ""
                    return reply

                if not self._run_calls(calls, on_tool, on_tool_result, turn_id):
                    return self._finish_cancelled(turn_id)

            # Tool budget exhausted: ask for a wrap-up without tools.
            messages = [{"role": "system", "content": system_prompt}]
            messages += self.history
            messages.append({
                "role": "user",
                "content": "(Tool budget for this turn is exhausted — summarize where things stand.)",
            })
            # Same tools array as every other round: some APIs reject a history
            # containing tool calls when no tools are declared, and changing it
            # would break prompt caching. Calls made here are dropped unanswered.
            partial["text"].clear()
            partial["reasoning"].clear()
            try:
                msg = self._chat_turn(messages, turn_id, tools=tools, on_text=emit_text,
                                      on_reasoning=emit_reasoning)
            except Cancelled:
                return self._finish_cancelled(turn_id, "".join(partial["text"]),
                                              "".join(partial["reasoning"]))
            msg.pop("tool_calls", None)
            msg.pop("provider_state", None)
            self._record_assistant(msg, turn_id)
            reply = msg.get("content") or ""
            self.episodes.log("jarvis", reply)
            self._emit("assistant_message", turn_id=turn_id, content=reply)
            self._emit("turn_finished", turn_id=turn_id, status="budget_exhausted")
            self.audit.log("turn", self.session_id, turn_id, "turn_finished", "budget_exhausted")
            self._active_turn = ""
            return reply
        except Exception:
            self._active_turn = ""
            raise

    def _record_assistant(self, msg, turn_id):
        """Append a model reply to history/session and announce its speed stats."""
        self.history.append(msg)
        self.sessions.append(self.session_id, msg)
        if msg.get("stats"):
            self._emit("assistant_stats", turn_id=turn_id, stats=msg["stats"])

    # ---------- history editing ----------

    def _drop_tail(self, count):
        """Drop the last `count` messages from memory and the session JSONL.

        After summarization self.history is no longer a prefix-match of the
        JSONL, but its tail still equals the JSONL tail, so cutting `count`
        from the end of both keeps them consistent.
        """
        if count > 0:
            del self.history[-count:]
            self.sessions.truncate_tail(self.session_id, count)
        self._emit("history_truncated", length=len(self.history))

    def edit_message(self, index, new_text, on_text=None, on_tool=None, on_tool_result=None,
                     on_reasoning=None):
        """Replace the user message at history[index] and rerun from there.

        Drops that message and everything after it, then asks new_text as a
        normal turn. Raises ValueError for a bad index or a non-user message,
        RuntimeError("busy") while a turn runs.
        """
        if self._active_turn:
            raise RuntimeError("busy")
        new_text = (new_text or "").strip()
        if not new_text:
            raise ValueError("message is empty")
        if not isinstance(index, int) or not 0 <= index < len(self.history):
            raise ValueError("no such message")
        message = self.history[index]
        if message.get("role") != "user" or (message.get("content") or "").startswith(SUMMARY_PREFIX):
            raise ValueError("only your own messages can be edited")
        self._drop_tail(len(self.history) - index)
        return self.ask(new_text, on_text, on_tool, on_tool_result, on_reasoning)

    def regenerate(self, on_text=None, on_tool=None, on_tool_result=None, on_reasoning=None):
        """Redo the reply to the last user message (it is not appended again)."""
        if self._active_turn:
            raise RuntimeError("busy")
        index = next(
            (i for i in range(len(self.history) - 1, -1, -1)
             if self.history[i].get("role") == "user"
             and not (self.history[i].get("content") or "").startswith(SUMMARY_PREFIX)),
            None,
        )
        if index is None:
            raise ValueError("nothing to regenerate")
        self._cancel_event.clear()
        self._drop_tail(len(self.history) - index - 1)
        turn_id = self._begin_turn(self.history[index].get("content") or "")
        return self._run_turn(turn_id, on_text, on_tool, on_tool_result, on_reasoning)

    # ---------- history compression ----------

    def _history_chars(self):
        total = 0
        for msg in self.history:
            total += len(msg.get("content") or "")
            for call in msg.get("tool_calls") or []:
                total += len(call["function"]["arguments"] or "")
        return total

    def _maybe_summarize(self):
        """Compress older history into a summary once it grows past the limit.

        Runs between turns. The full text is already preserved in episodes/,
        so nothing is lost — only the in-context transcript shrinks.
        """
        agent_cfg = self.config["agent"]
        if self._history_chars() <= agent_cfg["history_char_limit"]:
            return

        # Cut at a user-message boundary so strict chat templates stay valid.
        cut = len(self.history) - agent_cfg["keep_recent"]
        while cut > 0 and self.history[cut].get("role") != "user":
            cut -= 1
        if cut <= 0:
            return

        old, recent = self.history[:cut], self.history[cut:]
        transcript = self._render_transcript(old)
        summary = (self.llm.chat(
            [{"role": "user", "content": SUMMARIZE_PROMPT + transcript}]
        ).get("content") or "").strip()
        if not summary:
            return  # model gave nothing back; try again next turn

        self.history = [
            {"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary}"},
            {"role": "assistant", "content": "Understood — context noted."},
        ] + [{k: v for k, v in m.items() if k != "provider_state"} for m in recent]
        self.episodes.log("summarized", f"compressed {len(old)} messages into:\n{summary}")

    @staticmethod
    def _render_transcript(messages):
        lines = []
        for msg in messages:
            role = msg.get("role")
            if role == "tool":
                lines.append(f"TOOL RESULT: {(msg.get('content') or '')[:300]}")
                continue
            content = (msg.get("content") or "").strip()
            if content:
                lines.append(f"{role.upper()}: {content}")
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                lines.append(f"ASSISTANT CALLED: {fn['name']}({fn['arguments'][:200]})")
        return "\n".join(lines)

    # ---------- tool execution ----------

    def _is_read_only_call(self, call):
        """True when this call only reads (so it may run concurrently with its neighbours)."""
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            name, _, _ = self.exposure.unwrap(call["function"]["name"], args)
        except (ValueError, TypeError):
            return False
        return self.exposure.is_read_only(name)

    def _run_calls(self, calls, on_tool, on_tool_result, turn_id):
        """Run one reply's tool calls and append the results to history in call order.

        Runs of consecutive read-only calls go in parallel (agent.parallel_tools, at most
        agent.max_parallel_tools at once); everything else runs one at a time, in order,
        because approvals are serial. Returns False if the turn was cancelled meanwhile.
        """
        cfg = self.config["agent"]
        parallel = bool(cfg.get("parallel_tools", True))
        workers = max(1, int(cfg.get("max_parallel_tools", 6)))
        i = 0
        while i < len(calls):
            end = i + 1
            if parallel and self._is_read_only_call(calls[i]):
                while end < len(calls) and self._is_read_only_call(calls[end]):
                    end += 1
            group = calls[i:end]
            if len(group) > 1:
                with ThreadPoolExecutor(max_workers=min(workers, len(group))) as pool:
                    futures = [toolctx.submit_in_context(pool, self._run_tool, c, on_tool, on_tool_result, turn_id)
                               for c in group]
                    messages = [f.result() for f in futures]
            else:
                messages = [self._run_tool(group[0], on_tool, on_tool_result, turn_id)]
            for message in messages:
                self.history.append(message)
                self.sessions.append(self.session_id, message)
            i = end
            if self._cancel_event.is_set():
                for call in calls[i:]:  # every tool_call needs an answer to keep the history valid
                    message = {"role": "tool", "tool_call_id": call["id"], "content": "Cancelled."}
                    self.history.append(message)
                    self.sessions.append(self.session_id, message)
                return False
        return True

    def _spill(self, name, text, turn_id):
        """Long results go to mind/tool_outputs (head + tail + path stay in context)."""
        if name in NO_SPILL_TOOLS or len(text) <= SPILL_THRESHOLD:
            return text
        with self._spill_lock:
            self._spill_count += 1
            number = self._spill_count
        return spill_output(self.mind, turn_id, number, text)

    def _run_tool(self, call, on_tool, on_tool_result=None, turn_id=""):
        """Execute one tool call; returns the tool-result message for history."""
        name = call["function"]["name"]
        call_id = call.get("id", "")
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            # Report the inner tool when the model went through call_tool.
            name, args, via = self.exposure.unwrap(name, args)
        except json.JSONDecodeError as e:
            error_msg = f"Tool arguments were not valid JSON: {e}"
            self._emit("tool_finished", turn_id=turn_id, name=name, args={}, result=error_msg, call_id=call_id)
            self.audit.log("tool", self.session_id, turn_id, name, "error", {"error": error_msg})
            self.episodes.log(f"tool: {name}", f"args: <invalid JSON> {call['function']['arguments'][:500]}\nresult: {error_msg}")
            return {"role": "tool", "tool_call_id": call["id"], "content": error_msg}
        safe_args = redact_value(args)
        if on_tool:
            on_tool(name, safe_args)
        extra = {"via": via} if via else {}
        extra["call_id"] = call_id
        self._emit("tool_started", turn_id=turn_id, name=name, args=safe_args, **extra)

        try:
            ctx = toolctx.ToolContext(self.approvals, self._cancel_event, self, self.session_id, turn_id, call_id)
            with toolctx.tool_context(ctx):
                result = self.exposure.run(name, args, via)
        except Exception as e:
            result = f"Error: {e}"

        result_str = result if isinstance(result, str) else json.dumps(result, default=str)
        result_str = redact_text(result_str)
        result_str = self._spill(name, result_str, turn_id)

        if on_tool_result:
            on_tool_result(name, redact_value(result))
        self._emit("tool_finished", turn_id=turn_id, name=name, args=safe_args,
                   result=_card_text(result_str), **extra)
        tool_status = "error" if (isinstance(result, dict) and result.get("error")) else "ok"
        self.audit.log(
            "tool", self.session_id, turn_id, name, tool_status,
            {"args": safe_args, "result": result_str[:4000]},
        )
        if isinstance(result, dict) and result.get("artifact_id"):
            artifact = result.get("artifact") or {}
            self._emit("artifact_created", turn_id=turn_id, artifact=artifact)

        self.episodes.log(
            f"tool: {name}",
            f"args: {json.dumps(safe_args, default=str)[:500]}\nresult: {result_str}",
        )
        return {"role": "tool", "tool_call_id": call["id"], "content": result_str}

    def _emit(self, kind, turn_id="", **data):
        self.events.emit(Event(kind, self.session_id, turn_id=turn_id, data=data))

    def _chat_turn(self, messages, turn_id, tools=None, on_text=None, on_reasoning=None):
        try:
            return self.llm.chat(messages, tools=tools, on_text=on_text, on_reasoning=on_reasoning)
        except Cancelled:
            raise
        except Exception as e:
            self._emit("error", turn_id=turn_id, message=str(e), status=getattr(e, "status", None),
                       details=getattr(e, "details", "") or "", provider=getattr(e, "provider", ""))
            self._emit("turn_finished", turn_id=turn_id, status="error")
            self.audit.log("llm", self.session_id, turn_id, "chat", "error", {"error": str(e)})
            self._active_turn = ""
            raise

    def _finish_cancelled(self, turn_id, partial_text="", partial_reasoning=""):
        # If last assistant message had tool_calls without matching tool results, inject cancellations
        if self.history and self.history[-1].get("tool_calls"):
            pending = self.history[-1].get("tool_calls") or []
            # count how many tool results already appended after that message
            # history ends with that assistant message, so none appended
            for call in pending:
                tool_msg = {"role": "tool", "tool_call_id": call["id"], "content": "Cancelled."}
                self.history.append(tool_msg)
                self.sessions.append(self.session_id, tool_msg)
        # Keep what was already streamed instead of replacing it with a bare "Cancelled."
        reply = partial_text.strip() or "Cancelled."
        message = {"role": "assistant", "content": reply}
        if partial_reasoning.strip():
            message["reasoning"] = partial_reasoning.strip()
        self.history.append(message)
        self.sessions.append(self.session_id, message)
        self.episodes.log("jarvis", reply)
        self._emit("assistant_message", turn_id=turn_id, content=reply)
        self._emit("turn_finished", turn_id=turn_id, status="cancelled")
        self.audit.log("turn", self.session_id, turn_id, "turn_finished", "cancelled")
        self._active_turn = ""
        return reply

    def new_session(self):
        """Start a fresh session while retaining the same runtime services."""
        self.character = self.config["agent"]["character"]
        if not self.store_characters.get(self.character):
            self.character = "jarvis"
        self.session_id = self._create_session()
        self.history = []
        self._emit("session_started", session_id=self.session_id, session_kind=self.kind)
        return self.session_id

    def resume_session(self, session_id):
        """Resume a persisted session in this agent instance."""
        if not self.sessions.exists(session_id):
            return {"error": f"Session not found: {session_id}"}
        self.session_id = session_id
        self.history = self.sessions.load(session_id)
        self.character = self._stored_character(session_id)
        self._emit("session_started", session_id=session_id, session_kind=self.kind)
        return {"session_id": session_id, "messages": len(self.history)}

    def last_user_message(self):
        """Return the latest real user request, excluding injected summaries."""
        for message in reversed(self.history):
            if message.get("role") != "user":
                continue
            content = (message.get("content") or "").strip()
            if content and not content.startswith(SUMMARY_PREFIX):
                return content
        return ""

    def retry_last_turn(self, on_text=None, on_tool=None, on_tool_result=None, on_reasoning=None):
        """Run the latest user request again as a new, auditable turn."""
        if self._active_turn:
            raise RuntimeError("A turn is already in progress")
        user_text = self.last_user_message()
        if not user_text:
            return ""
        if self.history and self.history[-1].get("role") in ("user", "tool"):
            # The failed turn left no reply: continue it instead of asking twice.
            self._cancel_event.clear()
            turn_id = self._begin_turn(user_text)
            return self._run_turn(turn_id, on_text, on_tool, on_tool_result, on_reasoning)
        return self.ask(
            user_text,
            on_text=on_text,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
            on_reasoning=on_reasoning,
        )

    def close(self):
        self.voice.stop()

    def refresh_voice(self):
        """Rebuild the optional voice engine after a runtime setting change."""
        self.voice.stop()
        self.voice = get_voice(self.config)
        self._emit("config_changed", section="voice")

    def cancel(self):
        self._cancel_event.set()
        self.voice.stop()
        self._emit("cancel_requested", turn_id=self._active_turn)
