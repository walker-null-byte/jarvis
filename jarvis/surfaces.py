"""Optional detached windows for persisted Jarvis artifacts."""

import subprocess
import sys
from pathlib import Path


class SurfaceManager:
    """Launch and track small native Python artifact windows.

    The artifact remains usable in the TUI if a desktop display or Tk is not
    available.  Windows are deliberately launched from a narrow artifact
    format, never from model-authored shell or Python code.
    """

    def __init__(self, artifact_root):
        self.artifact_root = Path(artifact_root)
        self._processes = {}

    @staticmethod
    def _tk_available():
        # A real import, not find_spec: tkinter's Python files can exist while
        # the C runtime (libtk8.6.so) is missing, and only importing catches that.
        try:
            import tkinter  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _tk_install_hint():
        return ("tkinter is not usable on this system (missing Tk runtime). "
                "Install it, e.g. on Arch/Garuda: 'sudo pacman -S tk' "
                "(Debian/Ubuntu: 'sudo apt install python3-tk').")

    def open(self, artifact_id):
        artifact_path = self.artifact_root / f"{artifact_id}.json"
        if not artifact_path.exists():
            return {"window_opened": False, "error": "Artifact not found"}
        if not self._tk_available():
            return {"window_opened": False, "error": self._tk_install_hint(),
                    "artifact_id": artifact_id}
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "jarvis.surface_window", str(artifact_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            return {"window_opened": False, "error": str(e), "artifact_id": artifact_id}
        # Liveness check: if the child dies within 2s, surface the real error
        # instead of reporting a bogus success (this is how the old silent
        # Tk-crash bug went unnoticed).
        try:
            _, err = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._processes[artifact_id] = process
            return {"window_opened": True, "artifact_id": artifact_id,
                    "pid": process.pid}
        if process.returncode != 0:
            detail = err.decode(errors="replace")[-500:].strip()
            return {"window_opened": False,
                    "error": f"window process exited with code {process.returncode}: {detail}",
                    "artifact_id": artifact_id}
        # Exited 0 immediately: window opened and closed, or nothing rendered.
        self._processes[artifact_id] = process
        return {"window_opened": True, "artifact_id": artifact_id,
                "pid": process.pid, "note": "window exited immediately"}

    def close(self, artifact_id):
        process = self._processes.pop(artifact_id, None)
        if not process:
            return {"window_closed": False, "artifact_id": artifact_id}
        if process.poll() is None:
            process.terminate()
        return {"window_closed": True, "artifact_id": artifact_id}

    def close_all(self):
        for artifact_id in list(self._processes):
            self.close(artifact_id)
