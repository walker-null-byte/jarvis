"""Jarvis's built-in tools, assembled into a ContextAware registry.

Learned tools (phase 4) will be hot-loaded into the same registry from tools/.
"""

import os
import subprocess
from pathlib import Path

from contextaware import ContextAware


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


def build_registry():
    registry = ContextAware(name="jarvis")
    for func in (run_command, write_file, list_dir):
        registry.register(func)
    # From contextaware: read_file, calculate, get_current_time, get_system_info,
    # internet_search, webpage_scrape (the web ones need the [web] extra).
    registry.register_default(all=True)
    return registry
