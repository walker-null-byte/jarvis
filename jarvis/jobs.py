"""Cron delegation — tools for managing scheduled jobs."""

import os
import sys
import datetime
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from jarvis.config import load_config, mind_dir
from jarvis.redaction import redact_text, redact_value


def _jobs_dir(config):
    return mind_dir(config) / "jobs"


def _status_path(config, name):
    return _jobs_dir(config) / f"{name}.status.json"


def get_job_status(name, config=None):
    """Return the persisted run status for a job, or a never-run default."""
    config = config or load_config()
    path = _status_path(config, name)
    if not path.exists():
        return {
            "name": name,
            "status": "never",
            "last_run": None,
            "last_finished": None,
            "last_result": None,
            "run_count": 0,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "name": name,
            "status": "unknown",
            "last_run": None,
            "last_finished": None,
            "last_result": None,
            "run_count": 0,
        }
    return data


def set_job_status(name, status, result=None, config=None, started=None):
    """Persist a running/succeeded/failed job state and update its Markdown view."""
    config = config or load_config()
    previous = get_job_status(name, config)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    started = started or previous.get("last_run") or now
    run_count = int(previous.get("run_count") or 0)
    if status == "running":
        run_count += 1
    safe_result = redact_value(result) if result is not None else None
    if isinstance(safe_result, str):
        safe_result = redact_text(safe_result)[-1500:]
    data = {
        "name": name,
        "status": status,
        "last_run": started,
        "last_finished": None if status == "running" else now,
        "last_result": safe_result,
        "run_count": run_count,
    }
    path = _status_path(config, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    _update_job_markdown_status(config, name, data)
    return data


def _update_job_markdown_status(config, name, data):
    path = _jobs_dir(config) / f"{name}.md"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        updates = {
            "- last_run:": f"- last_run: {data.get('last_run') or 'never'}",
            "- last_result:": f"- last_result: {json.dumps(data.get('last_result'), ensure_ascii=False)}",
            "- last_status:": f"- last_status: {data.get('status', 'unknown')}",
            "- run_count:": f"- run_count: {data.get('run_count', 0)}",
        }
        found = set()
        for index, line in enumerate(lines):
            for prefix, replacement in updates.items():
                if line.strip().startswith(prefix):
                    lines[index] = replacement
                    found.add(prefix)
        for prefix in updates:
            if prefix not in found:
                lines.append(updates[prefix])
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError:
        return


def _backend(config):
    return config.get("jobs", {}).get("backend", "private-file")


class SchedulerBackend:
    """Small adapter for the configured scheduler without hiding its limits."""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.name = _backend(self.config)

    def available(self):
        if self.name == "private-file":
            return True
        if self.name == "system":
            return shutil.which("crontab") is not None
        return False

    def report(self, installed=None):
        available = self.available()
        if self.name == "private-file":
            state = "recorded-only"
            active = False
            recorded = installed is not False
            message = "Stored locally; no system scheduler is running this entry"
        elif not available:
            state = "unavailable"
            active = False
            recorded = False
            message = "The crontab executable is not available"
        elif installed is None:
            state = "available"
            active = False
            recorded = False
            message = "System scheduler is available; per-job installation was not checked"
        else:
            state = "installed" if installed else "not-installed"
            active = bool(installed)
            recorded = bool(installed)
            message = "Installed in the user crontab" if installed else "Not installed in the user crontab"
        return {
            "backend": self.name,
            "available": available,
            "installed": active,
            "recorded": recorded,
            "state": state,
            "message": message,
        }

    def read(self):
        if self.name == "system":
            if not self.available():
                return []
            try:
                result = subprocess.run(
                    ["crontab", "-l"], capture_output=True, text=True, check=False,
                )
            except OSError:
                return []
            if result.returncode not in (0, 1):
                return []
            return result.stdout.splitlines()
        if self.name != "private-file":
            return []
        f = _crontab_file(self.config)
        try:
            if not f.exists():
                return []
            return f.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def write(self, lines):
        if not self.available():
            return False
        if self.name == "system":
            try:
                result = subprocess.run(
                    ["crontab", "-"], input="\n".join(lines) + "\n",
                    capture_output=True, text=True, check=False,
                )
            except OSError:
                return False
            return result.returncode == 0
        if self.name != "private-file":
            return False
        f = _crontab_file(self.config)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            temporary = f.with_suffix(f"{f.suffix}.tmp")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            temporary.replace(f)
            return True
        except OSError:
            return False

    def install(self, command):
        lines = self.read()
        if command not in lines:
            lines.append(command)
        return self.write(lines)

    def remove(self, name):
        lines = self.read()
        filtered = [line for line in lines if f"run_job.py {name}" not in line]
        if len(filtered) == len(lines):
            return True
        return self.write(filtered)


def _scheduler_report(config, installed=None):
    return SchedulerBackend(config).report(installed)


def _validate_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name or ""):
        return "Job name must be 1–64 characters using letters, numbers, '-' or '_'"
    return ""


def _validate_schedule(schedule):
    fields = (schedule or "").split()
    if len(fields) != 5 or any(re.search(r"[;&|`$<>\\\n]", field) for field in fields):
        return "Schedule must be a five-field cron expression without shell characters"
    return ""


def cron_add(name: str, schedule: str, instruction: str, config=None):
    """Add a cron job that runs a headless Jarvis turn.

    Args:
        name (str): Job name (kebab-case, used as filename)
        schedule (str): Cron schedule expression (e.g., "0 2 * * *" for 2am daily)
        instruction (str): Natural language instruction for Jarvis to execute
        config: Config dict (optional)

    Returns:
        dict: Result with job path or error
    """
    config = config or load_config()
    if error := _validate_name(name):
        return {"error": error}
    if error := _validate_schedule(schedule):
        return {"error": error}
    if not instruction.strip():
        return {"error": "Instruction cannot be empty"}
    jobs = _jobs_dir(config)
    jobs.mkdir(parents=True, exist_ok=True)

    job_path = jobs / f"{name}.md"
    if job_path.exists():
        return {"error": f"Job '{name}' already exists"}

    content = f"""# Job: {name}
Schedule: {schedule}
Created: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}

## Instruction
{instruction}

## Status
- enabled: true
- last_run: never
- last_result: null
- last_status: never
- run_count: 0
"""
    tmp = job_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(job_path)

    # Add crontab entry
    python = os.path.abspath(sys.executable)
    project_root = Path(__file__).resolve().parent.parent
    run_job = project_root / "jarvis" / "run_job.py"
    cmd = (
        f"{schedule} {shlex.quote(python)} {shlex.quote(str(run_job))} {shlex.quote(name)} "
        f">> {shlex.quote(str(jobs / f'{name}.log'))} 2>&1"
    )

    installed = _add_crontab_entry(cmd, config)
    result = {"created": str(job_path), "schedule": schedule}
    result["scheduler"] = _scheduler_report(config, installed)
    if not installed:
        result["error"] = (
            "Could not install the system crontab entry"
            if _backend(config) == "system"
            else "Could not record the private scheduler entry"
        )
    return result


def cron_remove(name: str, config=None):
    """Remove a cron job."""
    config = config or load_config()
    if error := _validate_name(name):
        return {"error": error}
    job_path = _jobs_dir(config) / f"{name}.md"
    if not job_path.exists():
        return {"error": f"Job '{name}' not found"}

    job_path.unlink()
    status_path = _status_path(config, name)
    if status_path.exists():
        status_path.unlink()
    removed = _remove_crontab_entry(name, config)
    return {"removed": name, "scheduler": _scheduler_report(config, removed)}


def _next_run(schedule):
    """Best-effort next run time for a cron schedule (requires croniter if available)."""
    try:
        from croniter import croniter
        base = datetime.datetime.now()
        itr = croniter(schedule, base)
        return itr.get_next(datetime.datetime).isoformat(timespec="seconds")
    except Exception:
        return None


def cron_list(config=None):
    """List all cron jobs."""
    config = config or load_config()
    jobs = _jobs_dir(config)
    if not jobs.exists():
        return {"jobs": []}

    scheduler = SchedulerBackend(config)
    scheduler_lines = scheduler.read()
    result = []
    for job_file in jobs.glob("*.md"):
        content = job_file.read_text()
        lines = content.splitlines()
        schedule = ""
        enabled = False
        for line in lines:
            if line.startswith("Schedule:"):
                schedule = line.replace("Schedule:", "").strip()
            elif line.strip().startswith("- enabled:"):
                enabled = "true" in line.lower()
        installed = any(f"run_job.py {job_file.stem}" in line for line in scheduler_lines)
        result.append({
            "name": job_file.stem,
            "schedule": schedule,
            "next_run": _next_run(schedule) if enabled and schedule else None,
            "enabled": enabled,
            "path": str(job_file),
            "scheduler": scheduler.report(installed),
            "status": get_job_status(job_file.stem, config),
        })
    return {"jobs": result}


def cron_status(name: str, config=None):
    """Return one job's definition, scheduler state, and last run result."""
    config = config or load_config()
    job_path = _jobs_dir(config) / f"{name}.md"
    if not job_path.exists():
        return {"error": f"Job '{name}' not found"}
    for item in cron_list(config)["jobs"]:
        if item["name"] == name:
            return item
    return {"error": f"Job '{name}' could not be read"}


def cron_enable(name: str, config=None):
    """Enable a cron job."""
    return _set_job_enabled(name, True, config)


def cron_disable(name: str, config=None):
    """Disable a cron job."""
    return _set_job_enabled(name, False, config)


def _set_job_enabled(name, enabled, config):
    config = config or load_config()
    job_path = _jobs_dir(config) / f"{name}.md"
    if not job_path.exists():
        return {"error": f"Job '{name}' not found"}

    content = job_path.read_text()
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("- enabled:"):
            lines[i] = f"- enabled: {str(enabled).lower()}"
            break
    job_path.write_text("\n".join(lines) + "\n")

    if enabled:
        installed = _add_crontab_entry_for_job(job_path, config)
    else:
        installed = _remove_crontab_entry(name, config)

    return {"job": name, "enabled": enabled, "scheduler": _scheduler_report(config, installed)}


# ---------- crontab helpers ----------

def _crontab_file(config=None):
    jobs = (config or {}).get("jobs", {})
    configured = jobs.get("crontab_file", "~/.jarvis_crontab")
    return Path(os.path.expanduser(configured))


def _read_crontab(config=None):
    return SchedulerBackend(config).read()


def _write_crontab(lines, config=None):
    return SchedulerBackend(config).write(lines)


def _add_crontab_entry(cmd, config=None):
    return SchedulerBackend(config).install(cmd)


def _remove_crontab_entry(name, config=None):
    return SchedulerBackend(config).remove(name)


def _add_crontab_entry_for_job(job_path, config=None):
    content = job_path.read_text()
    name = job_path.stem
    schedule = ""
    enabled = True
    for line in content.splitlines():
        if line.startswith("Schedule:"):
            schedule = line.replace("Schedule:", "").strip()
        elif "- enabled:" in line:
            enabled = "true" in line.lower()

    if schedule and enabled:
        python = os.path.abspath(sys.executable)
        project_root = Path(__file__).resolve().parent.parent
        run_job = project_root / "jarvis" / "run_job.py"
        cmd = (
            f"{schedule} {shlex.quote(python)} {shlex.quote(str(run_job))} {shlex.quote(name)} "
            f">> {shlex.quote(str(job_path.parent / f'{name}.log'))} 2>&1"
        )
        _remove_crontab_entry(name, config)
        return _add_crontab_entry(cmd, config)
    return False
