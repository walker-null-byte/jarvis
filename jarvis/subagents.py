"""spawn_agent: run a focused sub-agent and return only its final answer.

The sub-agent is a fresh `Jarvis` (kind="subagent") with its own history, so a
broad research or exploration task never bloats the caller's context. It cannot
spawn further agents (depth 1). At most MAX_CONCURRENT run at once.
"""

import dataclasses
import threading
import time

from . import toolctx

MAX_CONCURRENT = 3
MAX_ROUNDS = 30
ANSWER_CAP = 6000

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_agent_manager = None   # set by the web server: the AgentManager that knows Agents plugins

SUBAGENT_PROMPT = (
    "You are a sub-agent working for another agent. Complete the task below using your tools, "
    "then reply with a concise final answer: findings, file paths, and conclusions. The agent "
    "that hired you sees ONLY your final message, so put everything it needs in it, and do not "
    "ask questions back."
)


def set_agent_manager(manager):
    """Register the AgentManager (jarvis/specialists.py) so `agent=<plugin id>` works."""
    global _agent_manager
    _agent_manager = manager


def _plugin_profile(agent_id):
    """AgentProfile of an installed Agents plugin, or None when unknown/unavailable."""
    manager = _agent_manager
    if manager is None:
        return None
    try:
        if hasattr(manager, "get"):
            manager.get(agent_id)  # raises for an id that is not installed
        return manager.profile(agent_id)
    except Exception:
        return None


def build_profile(parent, agent, task):
    """The sub-agent's AgentProfile; None when `agent` names no known plugin."""
    from .runtime import AgentProfile
    title = f"Subagent: {task.strip()[:60]}"
    if agent in ("", "general"):
        return AgentProfile(kind="subagent", owner=parent.session_id, title=title,
                            system_prompt=SUBAGENT_PROMPT, replace_persona=True,
                            max_tool_rounds=MAX_ROUNDS, allow_spawn=False)
    base = _plugin_profile(agent)
    if base is None:
        return None
    rounds = min(base.max_tool_rounds or MAX_ROUNDS, MAX_ROUNDS)
    return dataclasses.replace(
        base, kind="subagent", owner=parent.session_id, title=title, allow_spawn=False,
        max_tool_rounds=rounds, hidden_tools=set(base.hidden_tools) | {"spawn_agent"},
        system_prompt=(base.system_prompt + "\n\n" + SUBAGENT_PROMPT).strip())


def spawn_agent(task: str, agent: str = "general", context: str = ""):
    """Delegate a self-contained task to a fresh sub-agent and get back only its final answer. Use it for broad research or exploration whose raw output would clutter this conversation. Several can run in parallel.

    Args:
        task (str): What the sub-agent must do, with everything it needs to know
        agent (str): "general", or the id of an installed Agents plugin
        context (str): Optional background to pass along (file paths, constraints)
    """
    ctx = toolctx.current()
    parent = ctx.agent if ctx else None
    if parent is None:
        return {"error": "spawn_agent can only run inside an agent turn"}
    if parent.profile is not None and not parent.profile.allow_spawn:
        return {"error": "Sub-agents cannot spawn further sub-agents"}
    if not str(task).strip():
        return {"error": "task is empty"}
    profile = build_profile(parent, agent, task)
    if profile is None:
        return {"error": f"Unknown agent: {agent!r}. Use 'general' or an installed Agents plugin id."}
    # Wait for a free slot, but stay cancellable.
    while not _slots.acquire(timeout=0.2):
        if ctx.cancel_event and ctx.cancel_event.is_set():
            return {"error": "Cancelled"}
    try:
        return _run(parent, ctx, profile, agent, task, context)
    finally:
        _slots.release()


def _run(parent, ctx, profile, agent, task, context):
    from .agent import Jarvis
    started = time.monotonic()
    child = Jarvis(parent.config, None, parent.events, profile)
    child.approvals = parent.approvals  # approval cards reach the user through the parent's manager
    sid = child.session_id
    counter = {"calls": 0}

    def watch(event):
        if event.session_id == sid and event.kind == "tool_started":
            counter["calls"] += 1
            parent._emit("subagent_progress", turn_id=ctx.turn_id, call_id=ctx.call_id,
                         session_id=sid, tool_calls=counter["calls"], last_tool=event.data.get("name", ""))

    parent.events.subscribe(watch)
    parent._emit("subagent_started", turn_id=ctx.turn_id, call_id=ctx.call_id, session_id=sid,
                 agent=agent or "general", task=task[:300])
    prompt = task if not context.strip() else f"{task}\n\nContext:\n{context.strip()}"
    box = {}

    def work():
        try:
            box["answer"] = child.ask(prompt)
        except Exception as e:  # reported to the parent as a tool error
            box["error"] = str(e)

    worker = threading.Thread(target=toolctx.capture(work), daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(0.2)
        if ctx.cancel_event and ctx.cancel_event.is_set() and not child._cancel_event.is_set():
            child.cancel()
    parent.events.unsubscribe(watch)
    info = {"tool_calls": counter["calls"], "seconds": round(time.monotonic() - started, 1),
            "session_id": sid}
    child.close()
    if "error" in box:
        return {**info, "error": f"Sub-agent failed: {box['error']}"}
    answer = (box.get("answer") or "").strip() or "(the sub-agent returned no answer)"
    if len(answer) > ANSWER_CAP:
        answer = answer[:ANSWER_CAP] + "\n[... answer cut at 6000 characters]"
    return {**info, "answer": answer}


spawn_agent.read_only = True
