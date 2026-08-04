"""The agent loop: user message -> LLM -> tools -> ... -> reply.

Every step is logged to mind/episodes/ as it happens.
"""

import json
import threading

from .approvals import ApprovalManager
from .audit import AuditLog
from .config import load_config
from .context import build_system_prompt, ensure_mind
from .events import Event, EventBus, new_id
from .llm import LLM
from .memory import EpisodeLog
from .redaction import redact_text, redact_value
from .registry import get_registry
from .sessions import SessionStore
from .voice import get_voice

MAX_TOOL_RESULT_CHARS = 8000

SUMMARIZE_PROMPT = """\
Compress this conversation transcript into compact bullet points. Preserve
everything needed to continue seamlessly: open tasks and their state, decisions
made, facts learned, file paths, commands that worked or failed, and what the
user asked for most recently. Drop pleasantries and dead ends. Output only the
bullet points.

Transcript:
"""


class Jarvis:
    def __init__(self, config=None, session_id=None, event_bus=None):
        self.config = config or load_config()
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
        self.sessions = SessionStore(self.mind)
        self.session_id = session_id or self.sessions.create()
        self.history = self.sessions.load(self.session_id) if session_id else []
        self.voice = get_voice(self.config)
        self._active_turn = ""
        self._emit("session_started", session_id=self.session_id)
        self.audit.log("session", self.session_id, action="session_started", status="ok")

    def ask(self, user_text, on_text=None, on_tool=None, on_tool_result=None):
        """Run one full turn. Returns the final reply text.

        on_text(token) streams reply fragments; on_tool(name, args) fires
        before each tool execution.
        """
        self._cancel_event.clear()
        self._maybe_summarize()
        turn_id = new_id("turn")
        self._active_turn = turn_id
        self._emit("turn_started", turn_id=turn_id)
        self.audit.log("turn", self.session_id, turn_id, "turn_started", "running", {"content": user_text})
        self.episodes.log("user", user_text)
        user_message = {"role": "user", "content": user_text}
        self.history.append(user_message)
        self.sessions.append(self.session_id, user_message)
        self._emit("user_message", turn_id=turn_id, content=user_text)
        tools = self.registry.to_openai_tools()

        for _ in range(self.config["agent"]["max_tool_rounds"]):
            if self._cancel_event.is_set():
                return self._finish_cancelled(turn_id)
            messages = [{"role": "system", "content": build_system_prompt(self.mind)}]
            messages += self.history
            def emit_text(token):
                self._emit("assistant_delta", turn_id=turn_id, content=token)
                if on_text:
                    on_text(token)

            msg = self._chat_turn(messages, turn_id, tools=tools, on_text=emit_text)
            self.history.append(msg)
            self.sessions.append(self.session_id, msg)

            calls = msg.get("tool_calls")
            if not calls:
                reply = msg.get("content") or ""
                self.episodes.log("jarvis", reply)
                self._emit("assistant_message", turn_id=turn_id, content=reply)
                self._emit("turn_finished", turn_id=turn_id, status="ok")
                self.audit.log("turn", self.session_id, turn_id, "turn_finished", "ok")
                self._active_turn = ""
                return reply

            for call in calls:
                tool_message = self._run_tool(call, on_tool, on_tool_result, turn_id)
                self.history.append(tool_message)
                self.sessions.append(self.session_id, tool_message)
                if self._cancel_event.is_set():
                    return self._finish_cancelled(turn_id)

        # Tool budget exhausted: ask for a wrap-up without tools.
        messages = [{"role": "system", "content": build_system_prompt(self.mind)}]
        messages += self.history
        messages.append({
            "role": "user",
            "content": "(Tool budget for this turn is exhausted — summarize where things stand.)",
        })
        def emit_wrapup(token):
            self._emit("assistant_delta", turn_id=turn_id, content=token)
            if on_text:
                on_text(token)

        msg = self._chat_turn(messages, turn_id, on_text=emit_wrapup)
        self.history.append(msg)
        self.sessions.append(self.session_id, msg)
        reply = msg.get("content") or ""
        self.episodes.log("jarvis", reply)
        self._emit("assistant_message", turn_id=turn_id, content=reply)
        self._emit("turn_finished", turn_id=turn_id, status="budget_exhausted")
        self.audit.log("turn", self.session_id, turn_id, "turn_finished", "budget_exhausted")
        self._active_turn = ""
        return reply

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
            {"role": "user", "content": f"[Summary of the earlier conversation]\n{summary}"},
            {"role": "assistant", "content": "Understood — context noted."},
        ] + recent
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

    def _run_tool(self, call, on_tool, on_tool_result=None, turn_id=""):
        """Execute one tool call; returns the tool-result message for history."""
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        safe_args = redact_value(args)
        if on_tool:
            on_tool(name, safe_args)
        self._emit("tool_started", turn_id=turn_id, name=name, args=safe_args)

        try:
            result = self.registry.call(name, args)
        except Exception as e:
            result = f"Error: {e}"

        result_str = result if isinstance(result, str) else json.dumps(result, default=str)
        result_str = redact_text(result_str)
        if len(result_str) > MAX_TOOL_RESULT_CHARS:
            result_str = result_str[:MAX_TOOL_RESULT_CHARS] + "\n[... truncated]"

        if on_tool_result:
            on_tool_result(name, redact_value(result))
        self._emit("tool_finished", turn_id=turn_id, name=name, args=safe_args, result=result_str[:2000])
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

    def _chat_turn(self, messages, turn_id, tools=None, on_text=None):
        try:
            return self.llm.chat(messages, tools=tools, on_text=on_text)
        except Exception as e:
            self._emit("error", turn_id=turn_id, message=str(e))
            self._emit("turn_finished", turn_id=turn_id, status="error")
            self.audit.log("llm", self.session_id, turn_id, "chat", "error", {"error": str(e)})
            self._active_turn = ""
            raise

    def _finish_cancelled(self, turn_id):
        reply = "Cancelled."
        message = {"role": "assistant", "content": reply}
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
        self.session_id = self.sessions.create()
        self.history = []
        self._emit("session_started", session_id=self.session_id)
        return self.session_id

    def resume_session(self, session_id):
        """Resume a persisted session in this agent instance."""
        if not self.sessions.exists(session_id):
            return {"error": f"Session not found: {session_id}"}
        self.session_id = session_id
        self.history = self.sessions.load(session_id)
        self._emit("session_started", session_id=session_id)
        return {"session_id": session_id, "messages": len(self.history)}

    def last_user_message(self):
        """Return the latest real user request, excluding injected summaries."""
        for message in reversed(self.history):
            if message.get("role") != "user":
                continue
            content = (message.get("content") or "").strip()
            if content and not content.startswith("[Summary of the earlier conversation]"):
                return content
        return ""

    def retry_last_turn(self, on_text=None, on_tool=None, on_tool_result=None):
        """Run the latest user request again as a new, auditable turn."""
        if self._active_turn:
            raise RuntimeError("A turn is already in progress")
        user_text = self.last_user_message()
        if not user_text:
            return ""
        return self.ask(
            user_text,
            on_text=on_text,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
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
