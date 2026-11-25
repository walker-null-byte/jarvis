"""Background shell commands: start one, read its output later, kill it.

`run_command(background=true)` hands the command to `JOBS.start`; the model then
polls with `command_output(job_id)` or stops it with `command_kill(job_id)`.
Every process is killed when Jarvis exits.
"""

import atexit
import collections
import os
import signal
import subprocess
import threading
import time

MAX_JOBS = 10
MAX_LINES = 2000        # output lines kept per job (oldest dropped)


class _Job:
    def __init__(self, job_id, command, process):
        self.id = job_id
        self.command = command
        self.process = process
        self.started = time.time()
        self.finished = None
        self.killed = False
        self.lines = collections.deque(maxlen=MAX_LINES)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:  # stdout and stderr are merged
            self.lines.append(line.rstrip("\n"))
        self.process.wait()
        self.finished = time.time()

    @property
    def status(self):
        if self.process.poll() is None:
            return "running"
        return "killed" if self.killed else "exited"


class BackgroundJobs:
    def __init__(self):
        self._jobs = {}
        self._next = 1
        self._lock = threading.Lock()

    def start(self, command):
        """Start `command` detached from the caller; returns {"job_id", ...} or {"error"}."""
        with self._lock:
            running = sum(1 for j in self._jobs.values() if j.status == "running")
            if running >= MAX_JOBS:
                return {"error": f"Too many background commands running ({MAX_JOBS}); kill one first"}
            job_id = f"job_{self._next}"
            self._next += 1
        try:
            process = subprocess.Popen(
                command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1, start_new_session=True)
        except OSError as e:
            return {"error": str(e)}
        with self._lock:
            self._jobs[job_id] = _Job(job_id, command, process)
        return {"job_id": job_id, "status": "running",
                "note": "Running in the background. Check it with call_tool(name='command_output', "
                        "arguments={'job_id': ...}); stop it with command_kill."}

    def output(self, job_id, tail=200):
        job = self._jobs.get(job_id)
        if not job:
            return {"error": f"No such job: {job_id}", "jobs": self.list()}
        try:
            tail = max(1, int(tail))
        except (TypeError, ValueError):
            tail = 200
        lines = list(job.lines)
        end = job.finished or time.time()
        return {"job_id": job.id, "status": job.status, "exit_code": job.process.poll(),
                "seconds": round(end - job.started, 1), "command": job.command[:200],
                "total_lines": len(lines), "output": "\n".join(lines[-tail:])}

    def kill(self, job_id):
        job = self._jobs.get(job_id)
        if not job:
            return {"error": f"No such job: {job_id}", "jobs": self.list()}
        if job.process.poll() is not None:
            return {"job_id": job_id, "status": job.status, "exit_code": job.process.returncode}
        job.killed = True
        self._signal(job, signal.SIGTERM)
        try:
            job.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._signal(job, signal.SIGKILL)
            job.process.wait()
        return {"job_id": job_id, "status": "killed", "exit_code": job.process.returncode}

    def list(self):
        return [{"job_id": j.id, "status": j.status, "command": j.command[:80]} for j in self._jobs.values()]

    def kill_all(self):
        for job in list(self._jobs.values()):
            if job.process.poll() is None:
                job.killed = True
                self._signal(job, signal.SIGKILL)

    @staticmethod
    def _signal(job, sig):
        try:
            os.killpg(job.process.pid, sig)  # the whole process group (shell + children)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                job.process.send_signal(sig)
            except OSError:
                pass


JOBS = BackgroundJobs()
atexit.register(JOBS.kill_all)


def command_output(job_id: str, tail: int = 200):
    """Status, exit code and the last lines of output of a background command.

    Args:
        job_id (str): The job_id returned by run_command(background=true)
        tail (int): How many trailing output lines to return
    """
    return JOBS.output(job_id, tail)


def command_kill(job_id: str):
    """Stop a background command started with run_command(background=true).

    Args:
        job_id (str): The job_id returned by run_command
    """
    return JOBS.kill(job_id)


command_output.read_only = True
