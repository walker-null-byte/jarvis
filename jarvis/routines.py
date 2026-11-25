"""Routines: recurring and timed tasks run by an in-process scheduler.

- `RoutineStore`   one JSON file per routine in mind/routines/ (the source of truth)
- `RoutineScheduler` a thread that wakes every 20 s and runs due routines (max 2 at once);
  each run is a normal Jarvis session of kind "routine"
- routine_* tools for the model (and hidden cron_* aliases)
- migration of the old mind/jobs/*.md cron jobs

Schedules (cron, English, describe) live in routine_schedule.py.
"""

import dataclasses
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .config import load_config, mind_dir
from .events import Event, EventBus
from .redaction import redact_text
from .routine_schedule import (  # noqa: F401  (re-exported for callers)
    ScheduleError, describe, next_after, next_runs, parse_cron, parse_schedule, upcoming,
    validate_schedule,
)

TICK_SECONDS = 20
MAX_CONCURRENT = 2
KEEP_RUNS = 50
MISSED_AFTER = 120  # a due routine this many seconds late counts as "missed" (Jarvis was off)
APPROVAL_NOTE = "needed approval: "
TARGET_KINDS = ("prompt", "agent_action", "agent_prompt")
HIDDEN_FOR_ROUTINES = {"routine_create", "routine_update", "routine_delete", "routine_run_now",
                       "cron_add", "cron_remove", "cron_enable", "cron_disable"}

UNATTENDED_PROMPT = """\
# Scheduled routine
You are running as the routine "{name}", started automatically at {when}. Nobody is watching.
Do the task, then finish with a short report (a few sentences) of what you did or found: it is
saved as the run summary. Do not ask questions. If something needs the user (an approval or
missing information), say exactly what in your report.
"""

_lock = threading.RLock()
_active = None  # the running RoutineScheduler, if any (tools use it)


def now_text(moment=None):
    return (moment or datetime.now()).replace(microsecond=0).isoformat()


def _slug(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "routine"
    return f"{base}-{uuid.uuid4().hex[:4]}"


def _excerpt(text, limit=1500):
    """The start of a reply with its line breaks kept (so markdown still renders)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut if cut > limit // 2 else limit].rstrip() + "\n\n…"


def _short(text, limit=200):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---------- target normalisation ----------

def validate_target(target):
    """A normalised target dict or ScheduleError-style ValueError."""
    if not isinstance(target, dict):
        raise ValueError("The target must be an object")
    kind = target.get("kind")
    if kind == "prompt":
        prompt = str(target.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("Write what Jarvis should do (the prompt is empty)")
        return {"kind": "prompt", "prompt": prompt[:8000]}
    agent = str(target.get("agent", "")).strip()
    if kind == "agent_action":
        action = str(target.get("action", "")).strip()
        inputs = target.get("inputs") or {}
        if not agent or not action:
            raise ValueError("Choose an agent and one of its actions")
        if not isinstance(inputs, dict):
            raise ValueError("Action inputs must be an object")
        return {"kind": "agent_action", "agent": agent, "action": action, "inputs": inputs}
    if kind == "agent_prompt":
        prompt = str(target.get("prompt", "")).strip()
        if not agent or not prompt:
            raise ValueError("Choose an agent and write what it should do")
        return {"kind": "agent_prompt", "agent": agent, "prompt": prompt[:8000]}
    raise ValueError(f"Target kind must be one of: {', '.join(TARGET_KINDS)}")


def target_text(target):
    """One line describing the target for lists."""
    kind = target.get("kind")
    if kind == "agent_action":
        return f"{target.get('agent')} → {target.get('action')}"
    if kind == "agent_prompt":
        return f"{target.get('agent')}: {_short(target.get('prompt'), 80)}"
    return _short(target.get("prompt"), 100)


# ---------- store ----------

class RoutineStore:
    """Routines as JSON files. Every method reads fresh, so several stores can share a folder."""

    def __init__(self, config=None, on_change=None):
        self.config = config or load_config()
        self.dir = mind_dir(self.config) / "routines"
        self.on_change = on_change

    # -- files
    def _path(self, routine_id):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", routine_id or "") or routine_id.startswith("."):
            raise KeyError(routine_id)
        return self.dir / f"{routine_id}.json"

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("id") else None

    def _write(self, routine):
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(routine["id"])
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(routine, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _changed(self):
        if self.on_change:
            self.on_change()

    # -- reading
    def list(self):
        with _lock:
            found = [r for p in sorted(self.dir.glob("*.json")) if not p.name.startswith("_")
                     for r in [self._read(p)] if r]
        return sorted(found, key=lambda r: (r.get("created", ""), r["id"]))

    def get(self, routine_id):
        """A routine by id (KeyError when unknown)."""
        with _lock:
            routine = self._read(self._path(routine_id))
        if routine is None:
            raise KeyError(routine_id)
        return routine

    def find(self, ref):
        """A routine by id, or by name (case-insensitive). KeyError when unknown or ambiguous."""
        try:
            return self.get(ref)
        except KeyError:
            pass
        matches = [r for r in self.list() if r.get("name", "").lower() == str(ref).strip().lower()]
        if len(matches) != 1:
            raise KeyError(ref)
        return matches[0]

    # -- writing
    def create(self, name, schedule, target, enabled=True, on_missed="run_once", notify=True, routine_id=None):
        name = str(name or "").strip()
        if not name:
            raise ValueError("Give the routine a name")
        schedule, target = validate_schedule(schedule), validate_target(target)
        if on_missed not in ("run_once", "skip"):
            raise ValueError("on_missed must be run_once or skip")
        moment = now_text()
        routine = {
            "id": routine_id or _slug(name), "name": name[:80], "enabled": bool(enabled),
            "schedule": schedule, "target": target, "on_missed": on_missed, "notify": bool(notify),
            "created": moment, "last_run": None, "next_run": None, "runs": [],
        }
        routine["next_run"] = self._compute_next(routine)
        with _lock:
            self._write(routine)
        self._changed()
        return routine

    def _compute_next(self, routine, after=None):
        if not routine.get("enabled"):
            return None
        moment = next_after(routine["schedule"], after or datetime.now())
        return now_text(moment) if moment else None

    def update(self, routine_id, **fields):
        """Change user-editable fields (name, enabled, schedule, target, on_missed, notify)."""
        with _lock:
            routine = self.get(routine_id)
            if "name" in fields:
                name = str(fields["name"] or "").strip()
                if not name:
                    raise ValueError("Give the routine a name")
                routine["name"] = name[:80]
            if "schedule" in fields:
                routine["schedule"] = validate_schedule(fields["schedule"])
            if "target" in fields:
                routine["target"] = validate_target(fields["target"])
            if "on_missed" in fields:
                if fields["on_missed"] not in ("run_once", "skip"):
                    raise ValueError("on_missed must be run_once or skip")
                routine["on_missed"] = fields["on_missed"]
            if "notify" in fields:
                routine["notify"] = bool(fields["notify"])
            if "enabled" in fields:
                routine["enabled"] = bool(fields["enabled"])
            if {"schedule", "enabled"} & set(fields):
                routine["next_run"] = self._compute_next(routine)
            self._write(routine)
        self._changed()
        return routine

    def patch(self, routine_id, quiet=False, **fields):
        """Scheduler bookkeeping: set arbitrary fields (next_run, last_run, enabled) without validation."""
        with _lock:
            routine = self.get(routine_id)
            routine.update(fields)
            self._write(routine)
        if not quiet:
            self._changed()
        return routine

    def delete(self, routine_id):
        with _lock:
            self.get(routine_id)
            self._path(routine_id).unlink(missing_ok=True)
        self._changed()

    # -- run history (newest first)
    def add_run(self, routine_id, run):
        with _lock:
            routine = self.get(routine_id)
            routine["runs"] = [run, *routine.get("runs", [])][:KEEP_RUNS]
            routine["last_run"] = run.get("started")
            self._write(routine)

    def update_run(self, routine_id, run_id, **fields):
        with _lock:
            routine = self.get(routine_id)
            for run in routine.get("runs", []):
                if run.get("id") == run_id:
                    run.update(fields)
                    break
            self._write(routine)
            return routine

    # -- notification state
    def _state_path(self):
        return self.dir / "_state.json"

    def state(self):
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_state(self, **values):
        with _lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            data = {**self.state(), **values}
            temporary = self._state_path().with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self._state_path())

    def unread(self):
        """Finished runs (of routines that notify) since the Routines view was last opened."""
        seen = self.state().get("seen", "")
        out = []
        for routine in self.list():
            if not routine.get("notify", True):
                continue
            for run in routine.get("runs", []):
                if run.get("finished") and run["finished"] > seen:
                    out.append({"routine_id": routine["id"], "name": routine["name"], "run": run})
        return sorted(out, key=lambda item: item["run"]["finished"], reverse=True)

    def mark_seen(self):
        self.set_state(seen=now_text())

    def public(self, routine, running=()):
        """The routine as the API shows it: stored fields plus derived text."""
        runs = routine.get("runs", [])
        last = runs[0] if runs else None
        return {
            **routine,
            "schedule_text": describe(routine["schedule"]),
            "target_text": target_text(routine["target"]),
            "last_status": ("running" if routine["id"] in running else (last or {}).get("status", "never")),
            "last_summary": (last or {}).get("summary", ""),
            "running": routine["id"] in running,
            "runs": runs[:5],
            "run_count": len(runs),
        }


# ---------- migration of mind/jobs/*.md ----------

def _parse_job_file(path):
    """(schedule text, instruction, enabled) from an old job file."""
    schedule, instruction, enabled, section = "", [], True, ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("Schedule:"):
            schedule = line.split(":", 1)[1].strip()
        elif line.startswith("## "):
            section = line[3:].strip().lower()
        elif section == "instruction":
            instruction.append(line)
        elif line.strip().startswith("- enabled:"):
            enabled = "true" in line.lower()
    return schedule, "\n".join(instruction).strip(), enabled


def migrate_jobs(store):
    """Turn mind/jobs/*.md into routines, once per job. Old files stay. Returns names migrated."""
    jobs_dir = mind_dir(store.config) / "jobs"
    if not jobs_dir.is_dir():
        return []
    marker = store.dir / "_migrated.json"
    with _lock:
        try:
            done = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            done = {}
        done.setdefault("jobs", {})
        done.setdefault("skipped", {})
        moved = []
        cron_text = "\n".join(_installed_job_lines(store))
        for path in sorted(jobs_dir.glob("*.md")):
            name = path.stem
            if name in done["jobs"] or name in done["skipped"]:
                continue
            schedule, instruction, enabled = _parse_job_file(path)
            if re.search(rf"run_job\.py\s+{re.escape(name)}(\s|$)", cron_text, re.M):
                enabled = False  # cron still runs it: start paused so it never runs twice
            try:
                routine = store.create(
                    name, {"kind": "cron", "cron": schedule}, {"kind": "prompt", "prompt": instruction},
                    enabled=enabled, on_missed="skip", routine_id=_unique_id(store, name))
            except (ValueError, ScheduleError) as error:
                done["skipped"][name] = str(error)
                continue
            _import_job_status(store, routine, jobs_dir / f"{name}.status.json")
            done["jobs"][name] = routine["id"]
            moved.append(name)
        if moved or not marker.exists() or done["skipped"]:
            store.dir.mkdir(parents=True, exist_ok=True)
            done["migrated_at"] = now_text()
            marker.write_text(json.dumps(done, indent=2) + "\n", encoding="utf-8")
    return moved


def _unique_id(store, name):
    candidate = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:60] or "job"
    taken = {r["id"] for r in store.list()}
    while candidate in taken:
        candidate = f"{candidate[:56]}-{uuid.uuid4().hex[:3]}"
    return candidate


def _import_job_status(store, routine, status_path):
    """Keep the old job's last result as one history entry."""
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not status.get("last_run") or status.get("status") in (None, "never", "unknown", "running"):
        return
    ok = status.get("status") == "succeeded"
    store.add_run(routine["id"], {
        "id": uuid.uuid4().hex[:10], "started": status["last_run"],
        "finished": status.get("last_finished") or status["last_run"], "duration": 0,
        "status": "ok" if ok else "error", "trigger": "migrated", "session_id": "",
        "summary": _short(str(status.get("last_result") or ""), 200),
        "error": "" if ok else _short(str(status.get("last_result") or ""), 200),
    })


def _read_system_crontab():
    """Lines of the user's crontab (empty when crontab is missing). Read only."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return result.stdout.splitlines() if result.returncode == 0 else []


def _installed_job_lines(store):
    """Every run_job.py line in the system or private crontab (dismissed or not)."""
    lines = list(_read_system_crontab())
    private = Path(str((store.config.get("jobs") or {}).get("crontab_file", "~/.jarvis_crontab"))).expanduser()
    try:
        lines += private.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    return [line for line in lines if "run_job.py" in line and not line.lstrip().startswith("#")]


def legacy_cron_lines(store):
    """Old Jarvis job entries still in a crontab (they would run next to the new scheduler)."""
    lines = list(_read_system_crontab())
    private = Path(str((store.config.get("jobs") or {}).get("crontab_file", "~/.jarvis_crontab"))).expanduser()
    try:
        lines += private.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    dismissed = set(store.state().get("dismissed_cron", []))
    return [line for line in dict.fromkeys(lines)
            if "run_job.py" in line and not line.lstrip().startswith("#") and line not in dismissed]


# ---------- runs ----------

@dataclasses.dataclass
class RunResult:
    status: str = "ok"      # ok | error | needs_approval | cancelled
    summary: str = ""
    error: str = ""


class RoutineScheduler:
    """Runs due routines. `state` is the WebState (runtimes, agents, hub, events)."""

    def __init__(self, state=None, config=None, tick=None, max_concurrent=None):
        self.state = state
        self.config = config or (state.jarvis.config if state else load_config())
        settings = self.config.get("routines", {})
        self.tick = float(tick or settings.get("tick_seconds", TICK_SECONDS))
        self.max_concurrent = int(max_concurrent or settings.get("max_concurrent", MAX_CONCURRENT))
        self.events = state.jarvis.events if state else EventBus()
        self.store = RoutineStore(self.config, on_change=self._emit_changed)
        self._stop = threading.Event()
        self._thread = None
        self._running = {}  # routine id -> {"session_id", "started"}
        self._workers = set()
        self._guard = threading.Lock()

    # ---------- lifecycle ----------

    def start(self):
        """Migrate old jobs, clean up interrupted runs and start the scheduler thread (idempotent)."""
        global _active
        if self._thread and self._thread.is_alive():
            return
        if self.config.get("routines", {}).get("enabled", True) is False:
            return
        try:
            migrate_jobs(self.store)
        except Exception as error:  # a broken old job must not stop the scheduler
            self._log(f"job migration failed: {error}")
        self._mark_interrupted()
        self._stop.clear()
        _active = self
        self._thread = threading.Thread(target=self._loop, name="routine-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        """Stop ticking, cancel running routines and wait briefly for their threads."""
        global _active
        self._stop.set()
        with self._guard:
            sessions = [info["session_id"] for info in self._running.values() if info.get("session_id")]
            workers = list(self._workers)
        for session_id in sessions:
            try:
                self.state.runtimes.cancel(session_id)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout)
        deadline = time.time() + timeout
        for worker in workers:
            worker.join(max(0.0, deadline - time.time()))
        if _active is self:
            _active = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def running_ids(self):
        with self._guard:
            return set(self._running)

    def _log(self, message):
        print(f"[routines] {message}", file=sys.stderr, flush=True)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as error:  # never let the thread die
                self._log(f"tick failed: {error}")
            self._stop.wait(self.tick)

    def _mark_interrupted(self):
        for routine in self.store.list():
            for run in routine.get("runs", []):
                if run.get("status") == "running":
                    self.store.update_run(routine["id"], run["id"], status="error", finished=now_text(),
                                          error="Interrupted: Jarvis stopped while this was running")

    # ---------- ticking ----------

    def tick_once(self, now=None):
        """Start every due routine (respecting the concurrency cap)."""
        now = now or datetime.now()
        for routine in self.store.list():
            if not routine.get("enabled"):
                continue
            if not routine.get("next_run"):
                due = self.store._compute_next(routine, now)
                if due:
                    self.store.patch(routine["id"], quiet=True, next_run=due)
                continue
            if datetime.fromisoformat(routine["next_run"]) > now:
                continue
            late = (now - datetime.fromisoformat(routine["next_run"])).total_seconds()
            missed = late > max(MISSED_AFTER, self.tick * 3)
            self._due(routine, now, missed)

    def _due(self, routine, now, missed):
        """Reschedule a due routine, then run it (unless it is a missed one set to skip)."""
        routine_id = routine["id"]
        once = routine["schedule"]["kind"] == "once"
        following = None if once else self.store._compute_next(routine, now)
        if missed and routine.get("on_missed") == "skip":
            self.store.patch(routine_id, next_run=following, **({"enabled": False} if once else {}))
            return
        with self._guard:
            if routine_id in self._running or len(self._running) >= self.max_concurrent:
                return  # try again next tick; next_run stays due
        self.store.patch(routine_id, quiet=True, next_run=following, **({"enabled": False} if once else {}))
        self._spawn(routine_id, "catchup" if missed else "schedule")

    def run_now(self, routine_id):
        """Start a run immediately. Returns {"started": True} or {"started": False, "error": ...}."""
        try:
            self.store.get(routine_id)
        except KeyError:
            return {"started": False, "error": "Routine not found"}
        with self._guard:
            if routine_id in self._running:
                return {"started": False, "error": "This routine is already running"}
            if len(self._running) >= self.max_concurrent:
                return {"started": False, "error": "Two routines are already running; try again shortly"}
        routine = self.store.get(routine_id)
        if routine.get("enabled") and routine["schedule"]["kind"] == "interval":
            # an interval counts from the latest run, so a manual run pushes the next one back
            self.store.patch(routine_id, quiet=True, next_run=self.store._compute_next(routine, datetime.now()))
        self._spawn(routine_id, "manual")
        return {"started": True}

    def _spawn(self, routine_id, trigger):
        with self._guard:
            if routine_id in self._running:
                return
            self._running[routine_id] = {"session_id": "", "started": now_text()}
        worker = threading.Thread(target=self._work, args=(routine_id, trigger), daemon=True,
                                  name=f"routine-{routine_id}")
        with self._guard:
            self._workers.add(worker)
        worker.start()

    # ---------- one run ----------

    def _work(self, routine_id, trigger):
        started = datetime.now()
        run = {"id": uuid.uuid4().hex[:10], "started": now_text(started), "finished": None, "duration": 0,
               "status": "running", "trigger": trigger, "session_id": "", "summary": "", "error": ""}
        try:
            routine = self.store.get(routine_id)
            self.store.add_run(routine_id, run)
            self._emit("routine_run_started", routine_id, run, routine)
            self._emit_changed()
            result = self._execute(routine, run)
        except KeyError:  # deleted while starting
            result = None
        except Exception as error:
            result = RunResult("error", error=_short(redact_text(str(error)) or type(error).__name__, 300))
        finally:
            with self._guard:
                self._running.pop(routine_id, None)
                self._workers.discard(threading.current_thread())
        if result is None:
            return
        run.update(status=result.status, summary=result.summary, error=result.error,
                   finished=now_text(), duration=round((datetime.now() - started).total_seconds(), 1))
        try:
            self.store.update_run(routine_id, run["id"], **{k: run[k] for k in (
                "status", "summary", "error", "finished", "duration", "session_id")})
        except KeyError:
            return
        self._emit("routine_run_finished", routine_id, run, routine)
        self._emit_changed()

    def _execute(self, routine, run):
        """Run the target in a fresh routine session and wait for it. Returns a RunResult."""
        state = self.state
        prompt, profile = self._prompt_and_profile(routine, run)
        jarvis = state.runtimes.create(profile)
        session_id = run["session_id"] = jarvis.session_id
        with self._guard:
            if routine["id"] in self._running:
                self._running[routine["id"]]["session_id"] = session_id
        self.store.update_run(routine["id"], run["id"], session_id=session_id)
        self._emit_changed()

        needed, errors = {}, []

        def watch(event):
            if event.session_id != session_id:
                return
            data = event.data or {}
            if event.kind == "approval_requested":
                payload = data.get("payload") or {}
                needed[data.get("request_id")] = str(payload.get("command") or data.get("action") or "an action")
                if not self._ui_connected():  # nobody to ask: deny so the run never hangs
                    jarvis.approvals.resolve(data.get("request_id"), False)
            elif event.kind == "approval_finished" and data.get("approved"):
                needed.pop(data.get("request_id"), None)
            elif event.kind == "error":
                errors.append(str(data.get("message", "")))

        self.events.subscribe(watch)
        try:
            reply = jarvis.ask(prompt)
        finally:
            self.events.unsubscribe(watch)
            state.runtimes.close(session_id)
        if self._stop.is_set():
            return RunResult("cancelled", error="Jarvis was shutting down")
        notes = [APPROVAL_NOTE + _short(command, 160) for command in needed.values()]
        if errors and not (reply or "").strip():
            return RunResult("error", error=_short(redact_text(errors[-1]), 300))
        status = "needs_approval" if notes else "ok"
        return RunResult(status, _excerpt(redact_text(reply or "")), "; ".join(notes))

    def _prompt_and_profile(self, routine, run):
        from .runtime import AgentProfile
        target = routine["target"]
        title = f"{routine['name']} — {datetime.now():%Y-%m-%d %H:%M}"
        unattended = UNATTENDED_PROMPT.format(name=routine["name"], when=f"{datetime.now():%Y-%m-%d %H:%M}")
        profile = AgentProfile(kind="routine", owner=routine["id"], title=title,
                               hidden_tools=set(HIDDEN_FOR_ROUTINES))
        if target["kind"] == "prompt":
            profile.system_prompt = unattended
            return target["prompt"], profile
        agents = self.state.agents  # specialists.py; raises ImportError until that feature exists
        base = agents.profile(target["agent"])
        profile = dataclasses.replace(
            base, kind="routine", owner=routine["id"], title=title,
            system_prompt=f"{base.system_prompt}\n\n{unattended}".strip(),
            hidden_tools=set(base.hidden_tools) | HIDDEN_FOR_ROUTINES)
        if target["kind"] == "agent_action":
            return agents.render_action(target["agent"], target["action"], target.get("inputs") or {}), profile
        return target["prompt"], profile

    def _ui_connected(self):
        hub = getattr(self.state, "hub", None)
        return bool(getattr(hub, "_clients", None))

    # ---------- events ----------

    def _emit(self, kind, routine_id, run, routine):
        self.events.emit(Event(kind, run.get("session_id") or "routines", data={
            "routine_id": routine_id, "name": routine.get("name", ""), "notify": routine.get("notify", True),
            "run": dict(run)}))

    def _emit_changed(self):
        self.events.emit(Event("routines_changed", "routines", data={}))

    # ---------- helpers for routes and tools ----------

    def listing(self):
        migrate_jobs(self.store)
        running = self.running_ids()
        return [self.store.public(r, running) for r in self.store.list()]


def active_scheduler():
    return _active


# profile factory so a reloaded routine session keeps its role (best effort)
def _routine_profile(owner, meta):
    from .runtime import AgentProfile
    return AgentProfile(kind="routine", owner=owner, hidden_tools=set(HIDDEN_FOR_ROUTINES))


def _register_factory():
    from .runtime import register_profile_factory
    register_profile_factory("routine", _routine_profile)


_register_factory()


# ---------- daemon ----------

def run_daemon(config):
    """Headless mode (`jarvis --daemon`): just the scheduler, until SIGINT/SIGTERM."""
    import signal
    from .agent import Jarvis
    from .webui.state import WebState

    state = WebState(Jarvis(config), token="")
    scheduler = state.routines
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    scheduler.start()
    count = len(scheduler.store.list())
    print(f"Jarvis routines daemon running: {count} routine(s), checking every {scheduler.tick:g}s", flush=True)
    stop.wait()
    scheduler.stop()
    state.jarvis.close()


# ---------- tools for the model ----------

def _store_for(config):
    """The active scheduler's store (so changes announce themselves) or a plain one."""
    return _active.store if _active else RoutineStore(config)


def _brief(routine):
    runs = upcoming(routine["schedule"], n=3)
    return {
        "id": routine["id"], "name": routine["name"], "enabled": routine["enabled"],
        "schedule": describe(routine["schedule"]), "what": target_text(routine["target"]),
        "next_runs": [f"{moment:%a %Y-%m-%d %H:%M}" for moment in runs],
        "last_status": (routine["runs"][0]["status"] if routine.get("runs") else "never"),
    }


def build_routine_tools(config=None):
    """The routine_* tools plus the old cron_* aliases (name -> callable)."""
    config = config or load_config()

    def routine_create(name: str, schedule: str, prompt: str = "", agent: str = "", action: str = "",
                       inputs: dict | None = None):
        """Create a recurring or one-off routine that Jarvis runs by itself on a schedule.

        Use it for reminders and repeating tasks ("every weekday at 9 check my email").
        The result shows the understood schedule and the next 3 run times: tell the user
        and confirm they are right.

        Args:
            name (str): Short title, e.g. "Morning email check"
            schedule (str): English ("every weekday at 9", "every 30 minutes", "in 20 minutes",
                "tomorrow at 8", "on the 1st at 8") or a 5-field cron expression
            prompt (str): What Jarvis should do when it runs (a full instruction)
            agent (str): Optional agent id to run instead of plain Jarvis
            action (str): Optional action id of that agent (then inputs are used, not the prompt)
            inputs (dict): Inputs for the agent action
        """
        try:
            parsed, _ = parse_schedule(schedule)
            if agent and action:
                target = {"kind": "agent_action", "agent": agent, "action": action, "inputs": inputs or {}}
            elif agent:
                target = {"kind": "agent_prompt", "agent": agent, "prompt": prompt}
            else:
                target = {"kind": "prompt", "prompt": prompt}
            store = _store_for(config)
            routine = store.create(name, parsed, target)
        except (ScheduleError, ValueError) as error:
            return {"error": str(error)}
        result = {"created": _brief(routine),
                  "note": "Tell the user the schedule and next run times so they can confirm."}
        if not _active:
            result["warning"] = ("The scheduler only runs while Jarvis is open in web mode (jarvis --web) "
                                 "or running as a service (jarvis --daemon).")
        return result

    def routine_list():
        """List all routines with their schedule, next runs and last status."""
        store = _store_for(config)
        return {"routines": [_brief(r) for r in store.list()]}

    def routine_update(id: str, name: str = "", schedule: str = "", prompt: str = "",
                       enabled: bool | None = None, notify: bool | None = None):
        """Change a routine (by id or exact name): rename, new schedule, new prompt, pause or resume.

        Args:
            id (str): Routine id or name
            name (str): New name (optional)
            schedule (str): New schedule in English or cron (optional)
            prompt (str): New instruction (optional; plain Jarvis routines only)
            enabled (bool): false pauses it, true resumes (optional)
            notify (bool): Show a notification when it finishes (optional)
        """
        store = _store_for(config)
        try:
            routine = store.find(id)
            fields = {}
            if name:
                fields["name"] = name
            if schedule:
                fields["schedule"] = parse_schedule(schedule)[0]
            if prompt:
                fields["target"] = {**routine["target"], "prompt": prompt}
            if enabled is not None:
                fields["enabled"] = enabled
            if notify is not None:
                fields["notify"] = notify
            routine = store.update(routine["id"], **fields)
        except KeyError:
            return {"error": f"No routine “{id}”. Use routine_list to see them."}
        except (ScheduleError, ValueError) as error:
            return {"error": str(error)}
        return {"updated": _brief(routine)}

    def routine_delete(id: str):
        """Delete a routine (by id or exact name) and its run history.

        Args:
            id (str): Routine id or name
        """
        store = _store_for(config)
        try:
            routine = store.find(id)
            store.delete(routine["id"])
        except KeyError:
            return {"error": f"No routine “{id}”. Use routine_list to see them."}
        return {"deleted": routine["name"]}

    def routine_run_now(id: str):
        """Run a routine right now (in the background) without changing its schedule.

        Args:
            id (str): Routine id or name
        """
        if not _active:
            return {"error": "The scheduler is not running; start Jarvis with --web or --daemon to run routines."}
        try:
            routine = _active.store.find(id)
        except KeyError:
            return {"error": f"No routine “{id}”. Use routine_list to see them."}
        return {**_active.run_now(routine["id"]), "routine": routine["name"]}

    # ---- hidden cron_* aliases (old names and arguments keep working)
    def cron_add(name: str, schedule: str, instruction: str):
        """Alias of routine_create (kept for old prompts)."""
        return routine_create(name, schedule, instruction)

    def cron_remove(name: str):
        """Alias of routine_delete."""
        return routine_delete(name)

    def cron_list():
        """Alias of routine_list."""
        return routine_list()

    def cron_status(name: str):
        """Alias of routine_list for one routine."""
        store = _store_for(config)
        try:
            return _brief(store.find(name))
        except KeyError:
            return {"error": f"No routine “{name}”"}

    def cron_enable(name: str):
        """Alias of routine_update(enabled=true)."""
        return routine_update(name, enabled=True)

    def cron_disable(name: str):
        """Alias of routine_update(enabled=false)."""
        return routine_update(name, enabled=False)

    return {
        "routine_create": routine_create, "routine_list": routine_list, "routine_update": routine_update,
        "routine_delete": routine_delete, "routine_run_now": routine_run_now,
        "cron_add": cron_add, "cron_remove": cron_remove, "cron_list": cron_list,
        "cron_status": cron_status, "cron_enable": cron_enable, "cron_disable": cron_disable,
    }


def register_routine_tools(registry, config=None):
    """Add the routine tools to a registry. The cron_* aliases stay callable but
    ToolExposure.ALIAS_TOOLS keeps them out of every tool listing."""
    for name, func in build_routine_tools(config).items():
        registry.register(func, name=name, description=func.__doc__)
