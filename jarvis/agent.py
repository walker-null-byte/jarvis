"""The agent loop: user message -> LLM -> tools -> ... -> reply.

Every step is logged to mind/episodes/ as it happens.
"""

import json

from .config import load_config
from .context import build_system_prompt, ensure_mind
from .llm import LLM
from .memory import EpisodeLog
from .mind_tools import build_mind_tools
from .tools_core import build_registry

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
    def __init__(self, config=None):
        self.config = config or load_config()
        self.mind = ensure_mind(self.config)
        self.episodes = EpisodeLog(self.mind)
        self.llm = LLM(self.config)
        self.registry = build_registry()
        for func in build_mind_tools(self.mind):
            self.registry.register(func)
        self.history = []  # conversation messages, system prompt excluded

    def ask(self, user_text, on_text=None, on_tool=None):
        """Run one full turn. Returns the final reply text.

        on_text(token) streams reply fragments; on_tool(name, args) fires
        before each tool execution.
        """
        self._maybe_summarize()
        self.episodes.log("user", user_text)
        self.history.append({"role": "user", "content": user_text})
        tools = self.registry.to_openai_tools()

        for _ in range(self.config["agent"]["max_tool_rounds"]):
            messages = [{"role": "system", "content": build_system_prompt(self.mind)}]
            messages += self.history
            msg = self.llm.chat(messages, tools=tools, on_text=on_text)
            self.history.append(msg)

            calls = msg.get("tool_calls")
            if not calls:
                reply = msg.get("content") or ""
                self.episodes.log("jarvis", reply)
                return reply

            for call in calls:
                self.history.append(self._run_tool(call, on_tool))

        # Tool budget exhausted: ask for a wrap-up without tools.
        messages = [{"role": "system", "content": build_system_prompt(self.mind)}]
        messages += self.history
        messages.append({
            "role": "user",
            "content": "(Tool budget for this turn is exhausted — summarize where things stand.)",
        })
        msg = self.llm.chat(messages, on_text=on_text)
        self.history.append(msg)
        reply = msg.get("content") or ""
        self.episodes.log("jarvis", reply)
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

    def _run_tool(self, call, on_tool):
        """Execute one tool call; returns the tool-result message for history."""
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        if on_tool:
            on_tool(name, args)

        try:
            result = self.registry.call(name, args)
        except Exception as e:
            result = f"Error: {e}"

        result_str = result if isinstance(result, str) else json.dumps(result, default=str)
        if len(result_str) > MAX_TOOL_RESULT_CHARS:
            result_str = result_str[:MAX_TOOL_RESULT_CHARS] + "\n[... truncated]"

        self.episodes.log(
            f"tool: {name}",
            f"args: {json.dumps(args, default=str)[:500]}\nresult: {result_str}",
        )
        return {"role": "tool", "tool_call_id": call["id"], "content": result_str}
