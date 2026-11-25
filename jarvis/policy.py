"""Capability policy for tools that can affect the host machine."""

import os
import re
from pathlib import Path

from . import toolctx
from .config import ROOT


DEFAULT_BLOCKED_COMMANDS = (
    r"\brm\s+(-[a-z]*r[a-z]*|--recursive)",
    r"\b(mkfs|fdisk|parted)\b",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{",
    r"\bgit\s+(reset|clean)\b",
    r"\b(find\s+.*-exec|\bxargs\s+.*rm)\b",
    r"\b(curl|wget)\b.*\|\s*(sh|bash)\b",
    r"\bchmod\s+.*777\b",
)


class CapabilityPolicy:
    """Small deterministic policy shared by built-in and learned tools.

    ``guarded`` is the default: normal inspection/development commands work,
    destructive command patterns are blocked, and file tools stay inside the
    configured roots. ``permissive`` preserves the prototype's old behavior for
    a deliberately trusted local setup.
    """

    def __init__(self, config=None):
        security = (config or {}).get("security", {})
        self.mode = security.get("mode", "guarded")
        self.max_timeout = int(security.get("max_command_timeout", 300))
        self.allow_outside_roots = bool(security.get("allow_outside_roots", False))
        root_values = security.get("allowed_roots") or [str(ROOT)]
        self.allowed_roots = []
        for value in root_values:
            path = Path(os.path.expanduser(str(value)))
            if not path.is_absolute():
                path = ROOT / path
            self.allowed_roots.append(path.resolve())
        self.blocked_commands = tuple(security.get("blocked_commands") or DEFAULT_BLOCKED_COMMANDS)
        self.approval_timeout = int(security.get("approval_timeout", 120))
        self._approval = None

    def set_approval_handler(self, approval):
        self._approval = approval

    def request_approval(self, action, payload, session_id="", turn_id=""):
        if self.mode == "permissive":
            return True
        ctx = toolctx.current()
        approval = (ctx.approvals if ctx and ctx.approvals else None) or self._approval
        if not approval:
            return False
        if ctx:  # route the request to the agent that is running this tool
            session_id, turn_id = session_id or ctx.session_id, turn_id or ctx.turn_id
        approval.timeout = self.approval_timeout
        return approval.request(action, payload, session_id, turn_id)

    def command_requires_approval(self, command):
        if self.mode == "permissive":
            return False
        # Require approval for privileged/destructive verbs and shell metachars
        if re.search(r"(^|\s)(sudo|git\s+push|mv|chmod)\b", command, re.I):
            return True
        if any(ch in command for ch in [">", "|", ";", "$", "`"]):
            return True
        if "&&" in command or "||" in command:
            return True
        return False

    _HUMAN_MESSAGES = {
        r"\brm\s+(-[a-z]*r[a-z]*|--recursive)": "recursive delete (rm -r) is blocked — use explicit file tools or a precise path",
        r"\b(mkfs|fdisk|parted)\b": "disk partitioning/formatting is blocked",
        r"\b(shutdown|reboot|poweroff|halt)\b": "system shutdown/reboot is blocked",
        r"\bdd\s+if=": "raw disk copy (dd) is blocked",
        r":\(\)\s*\{": "fork bomb pattern is blocked",
        r"\bgit\s+(reset|clean)\b": "destructive git reset/clean is blocked",
        r"\b(find\s+.*-exec|\bxargs\s+.*rm)\b": "find -exec / xargs rm is blocked",
        r"\b(curl|wget)\b.*\|\s*(sh|bash)\b": "piping curl/wget to shell is blocked",
        r"\bchmod\s+.*777\b": "chmod 777 is blocked",
    }

    def check_command(self, command, timeout):
        if self.mode == "permissive":
            return ""
        for pattern in self.blocked_commands:
            if re.search(pattern, command, flags=re.IGNORECASE):
                human = self._HUMAN_MESSAGES.get(pattern, pattern)
                return f"Command blocked by safety policy: {human}"
        if timeout < 1 or timeout > self.max_timeout:
            return f"Command timeout must be between 1 and {self.max_timeout} seconds"
        return ""

    def resolve_path(self, file_path):
        path = Path(os.path.expanduser(file_path))
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()

    def check_path(self, file_path):
        path = self.resolve_path(file_path)
        if self.mode == "permissive" or self.allow_outside_roots:
            return "", path
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            roots = ", ".join(str(root) for root in self.allowed_roots)
            return f"Path is outside allowed roots ({roots})", path
        return "", path
