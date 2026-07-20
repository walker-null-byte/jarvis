"""Jarvis's built-in tools, assembled into a turbomcp registry.

Learned tools (phase 4) will be hot-loaded into the same registry from tools/.
"""

import os
import subprocess
from pathlib import Path

from turbomcp import TurboMCP


def run_command(command: str, timeout: int = 60):
    """Run a shell command on this computer and capture its output.

    Args:
        command (str): The shell command to run (bash syntax)
        timeout (int): Seconds to wait before giving up
    """
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    result = {"returncode": r.returncode}
    if r.stdout:
        result["stdout"] = r.stdout[-6000:]
    if r.stderr:
        result["stderr"] = r.stderr[-3000:]
    return result


def write_file(file_path: str, content: str, append: bool = False):
    """Write text to a file, creating parent directories if needed.

    Args:
        file_path (str): Where to write
        content (str): What to write
        append (bool): If true, append instead of overwriting
    """
    path = Path(os.path.expanduser(file_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return {"written": str(path), "bytes": len(content.encode())}


def list_dir(path: str = "."):
    """List the contents of a directory (directories get a trailing /).

    Args:
        path (str): Directory to list
    """
    p = Path(os.path.expanduser(path))
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    entries = sorted(e.name + "/" if e.is_dir() else e.name for e in p.iterdir())
    return {"path": str(p), "entries": entries}


def cron_add(name: str, schedule: str, instruction: str):
    """Add a scheduled cron job that runs a headless Jarvis turn.

    The job will execute the instruction at the given schedule.

    Args:
        name (str): Job name (kebab-case, used as filename)
        schedule (str): Cron schedule expression (e.g., "0 2 * * *" for 2am daily)
        instruction (str): Natural language instruction for Jarvis to execute
    """
    from jarvis.jobs import cron_add as _cron_add
    return _cron_add(name, schedule, instruction)


def cron_remove(name: str):
    """Remove a scheduled cron job by name."""
    from jarvis.jobs import cron_remove as _cron_remove
    return _cron_remove(name)


def cron_list():
    """List all scheduled cron jobs."""
    from jarvis.jobs import cron_list as _cron_list
    return _cron_list()


def cron_enable(name: str):
    """Enable a disabled cron job."""
    from jarvis.jobs import cron_enable as _cron_enable
    return _cron_enable(name)


def cron_disable(name: str):
    """Disable a cron job without removing it."""
    from jarvis.jobs import cron_disable as _cron_disable
    return _cron_disable(name)


def build_registry():
    registry = TurboMCP(name="jarvis")
    for func in (run_command, write_file, list_dir, cron_add, cron_remove, cron_list, cron_enable, cron_disable):
        registry.register(func)
    # From contextaware: read_file, calculate, get_current_time, get_system_info,
    # internet_search, webpage_scrape (the web ones need the [web] extra).
    registry.register_default(all=True)
    return registry
