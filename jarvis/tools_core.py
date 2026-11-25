"""Jarvis's built-in tools, assembled into a turbomcp registry.

Learned tools (phase 4) will be hot-loaded into the same registry from tools/.
"""

import os
import subprocess
import time
from pathlib import Path

from turbomcp import TurboMCP
from jarvis import toolctx
from jarvis.policy import CapabilityPolicy
from jarvis.config import load_config


def _run_command(command: str, timeout: int = 60, policy=None, cancel_event=None, background: bool = False):
    """Run a shell command on this computer and capture its output.

    Args:
        command (str): The shell command to run (bash syntax)
        timeout (int): Seconds to wait before giving up
    """
    if policy:
        error = policy.check_command(command, timeout)
        if error:
            return {"error": error, "blocked": True}
        if policy.command_requires_approval(command) and not policy.request_approval(
            "run_command", {"command": command, "timeout": timeout}
        ):
            return {"error": "Command was not approved", "approval_required": True}
    if background:
        from jarvis.bg_commands import JOBS
        return JOBS.start(command)
    try:
        process = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        started = time.monotonic()
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel_event and cancel_event.is_set():
                    try:
                        os.killpg(process.pid, 15)  # SIGTERM to process group
                    except Exception:
                        process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, 9)
                        except Exception:
                            process.kill()
                        stdout, stderr = process.communicate()
                    return {"cancelled": True, "returncode": process.returncode,
                            "stdout": stdout[-6000:], "stderr": stderr[-3000:]}
                if time.monotonic() - started >= timeout:
                    try:
                        os.killpg(process.pid, 9)
                    except Exception:
                        process.kill()
                    stdout, stderr = process.communicate()
                    return {"error": f"Command timed out after {timeout}s", "timed_out": True,
                            "stdout": stdout[-6000:], "stderr": stderr[-3000:]}
    except OSError as e:
        return {"error": str(e)}
    result = {"returncode": process.returncode}
    if stdout:
        result["stdout"] = stdout[-6000:]
    if stderr:
        result["stderr"] = stderr[-3000:]
    return result


def run_command(command: str, timeout: int = 60):
    """Run a shell command using the configured capability policy."""
    return _run_command(command, timeout, CapabilityPolicy(load_config()))


def _write_file(file_path: str, content: str, append: bool = False, policy=None):
    """Write text to a file, creating parent directories if needed.

    Args:
        file_path (str): Where to write
        content (str): What to write
        append (bool): If true, append instead of overwriting
    """
    if policy:
        error, path = policy.check_path(file_path)
        if error:
            return {"error": error, "blocked": True}
    else:
        path = Path(os.path.expanduser(file_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
    else:
        # Atomic write to avoid half-written files on crash
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    return {"written": str(path), "bytes": len(content.encode())}


def write_file(file_path: str, content: str, append: bool = False):
    """Write a file using the configured capability policy."""
    return _write_file(file_path, content, append, CapabilityPolicy(load_config()))


def _list_dir(path: str = ".", policy=None):
    """List the contents of a directory (directories get a trailing /).

    Args:
        path (str): Directory to list
    """
    if policy:
        error, p = policy.check_path(path)
        if error:
            return {"error": error, "blocked": True}
    else:
        p = Path(os.path.expanduser(path))
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    entries = sorted(e.name + "/" if e.is_dir() else e.name for e in p.iterdir())
    return {"path": str(p), "entries": entries}


def list_dir(path: str = "."):
    """List a directory using the configured capability policy."""
    return _list_dir(path, CapabilityPolicy(load_config()))


def build_registry(mind=None, config=None):
    registry = TurboMCP(name="jarvis")
    policy = CapabilityPolicy(config)

    def guarded_run_command(command: str, timeout: int = 60, background: bool = False):
        """Run a shell command on this computer and capture its output.

        Args:
            command (str): The shell command to run (bash syntax)
            timeout (int): Seconds to wait before giving up (ignored when background is true)
            background (bool): Start it detached and return a job_id at once; for builds, servers and long scripts. Then use command_output / command_kill.
        """
        ctx = toolctx.current()
        cancel = (ctx.cancel_event if ctx and ctx.cancel_event else None) or getattr(registry, "_cancel_event", None)
        return _run_command(command, timeout, policy, cancel, background)

    def guarded_write_file(file_path: str, content: str, append: bool = False):
        return _write_file(file_path, content, append, policy)

    def guarded_list_dir(path: str = "."):
        return _list_dir(path, policy)

    registry.register(guarded_run_command, name="run_command")
    registry.register(guarded_write_file, name="write_file", description=write_file.__doc__)
    registry.register(guarded_list_dir, name="list_dir", description=list_dir.__doc__)
    from jarvis.routines import register_routine_tools  # routine_* tools + hidden cron_* aliases
    register_routine_tools(registry, config)
    # From turbomcp: read_file, calculate, get_current_time, get_system_info,
    # internet_search, webpage_scrape (the web ones need the [web] extra).
    registry.register_default(all=True)
    # Coding tools replace turbomcp's read_file; all go through the capability policy.
    from jarvis.file_tools import build_file_tools
    from jarvis.bg_commands import command_output, command_kill
    from jarvis import subagents, web_tools
    if "read_file" in registry.tools:
        registry.unregister("read_file")
    for func in build_file_tools(policy):
        registry.register(func)
    for func in (command_output, command_kill, subagents.spawn_agent,
                 web_tools.web_search, web_tools.fetch_page):
        registry.register(func)
    guarded_list_dir.read_only = True
    registry._jarvis_policy = policy
    registry._cancel_event = None
    if mind is not None:
        from jarvis.artifacts import build_artifact_tools
        for func in build_artifact_tools(mind):
            registry.register(func)
    return registry
