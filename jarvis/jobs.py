"""Cron delegation — tools for managing scheduled jobs."""

import os
import sys
import datetime
from pathlib import Path

from jarvis.config import load_config, mind_dir


def _jobs_dir(config):
    return mind_dir(config) / "jobs"


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
"""
    job_path.write_text(content, encoding="utf-8")

    # Add crontab entry
    python = os.path.abspath(sys.executable)
    project_root = Path(__file__).resolve().parent.parent
    run_job = project_root / "jarvis" / "run_job.py"
    cmd = f"{schedule} {python} {run_job} {name} >> {jobs / f'{name}.log'} 2>&1"

    _add_crontab_entry(cmd)
    return {"created": str(job_path), "schedule": schedule}


def cron_remove(name: str, config=None):
    """Remove a cron job."""
    config = config or load_config()
    job_path = _jobs_dir(config) / f"{name}.md"
    if not job_path.exists():
        return {"error": f"Job '{name}' not found"}

    job_path.unlink()
    _remove_crontab_entry(name)
    return {"removed": name}


def cron_list(config=None):
    """List all cron jobs."""
    config = config or load_config()
    jobs = _jobs_dir(config)
    if not jobs.exists():
        return {"jobs": []}

    result = []
    for job_file in jobs.glob("*.md"):
        content = job_file.read_text()
        lines = content.splitlines()
        schedule = ""
        for line in lines:
            if line.startswith("Schedule:"):
                schedule = line.replace("Schedule:", "").strip()
                break
        result.append({"name": job_file.stem, "schedule": schedule, "path": str(job_file)})
    return {"jobs": result}


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
        _add_crontab_entry_for_job(job_path)
    else:
        _remove_crontab_entry(name)

    return {"job": name, "enabled": enabled}


# ---------- crontab helpers ----------

def _crontab_file():
    return Path.home() / ".jarvis_crontab"


def _read_crontab():
    f = _crontab_file()
    if not f.exists():
        return []
    return f.read_text().splitlines()


def _write_crontab(lines):
    f = _crontab_file()
    f.write_text("\n".join(lines) + "\n")


def _add_crontab_entry(cmd):
    lines = _read_crontab()
    lines.append(cmd)
    _write_crontab(lines)


def _remove_crontab_entry(name):
    lines = _read_crontab()
    lines = [l for l in lines if f"run_job.py {name}" not in l]
    _write_crontab(lines)


def _add_crontab_entry_for_job(job_path):
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
        cmd = f"{schedule} {python} {run_job} {name} >> {job_path.parent / f'{name}.log'} 2>&1"
        _add_crontab_entry(cmd)