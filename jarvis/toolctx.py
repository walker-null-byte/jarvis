"""Context-local tool context: who is running this tool call?

Several Jarvis instances can run at once in one process, so the approval
manager and cancel event of the *current* tool call live in a ContextVar
instead of a global. `Jarvis._run_tool` sets it; policy and command code read
`current()` first and fall back to the old global (CLI, tests).

Threads do not inherit contextvars: code that runs tools in a thread pool must
use `submit_in_context(pool, fn, ...)` or `capture(fn)` (both copy the context
at call time, i.e. `contextvars.copy_context().run`).
"""

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

_current = contextvars.ContextVar("jarvis_tool_context", default=None)


@dataclass
class ToolContext:
    approvals: Any = None       # ApprovalManager of the agent running the tool
    cancel_event: Any = None    # threading.Event
    agent: Any = None           # the Jarvis instance
    session_id: str = ""
    turn_id: str = ""
    call_id: str = ""          # id of the model's tool call (spawn_agent links its card with it)


def current():
    """The ToolContext of the running tool call, or None outside one."""
    return _current.get()


@contextmanager
def tool_context(ctx):
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def capture(fn):
    """Wrap fn so it runs later (in another thread) inside the context captured NOW:
    `Thread(target=toolctx.capture(work))`, `pool.map(toolctx.capture(work), items)`."""
    ctx = contextvars.copy_context()
    return lambda *args, **kwargs: ctx.run(fn, *args, **kwargs)


def submit_in_context(pool, fn, *args, **kwargs):
    """`pool.submit(fn, ...)` that keeps the caller's tool context in the worker thread."""
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)
