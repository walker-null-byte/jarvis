"""Routines routes: list, create, edit, delete, run now, history, schedule preview, unread."""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..routine_schedule import ScheduleError, describe, parse_schedule, upcoming, validate_schedule
from ..routines import legacy_cron_lines
from .state import WebState, check_id, fail, web

router = APIRouter(prefix="/api")


class RoutineBody(BaseModel):
    name: str | None = None
    schedule: dict | str | None = None   # a schedule dict, or English / cron text
    target: dict | None = None
    prompt: str | None = None            # shortcut for target {"kind": "prompt", "prompt": ...}
    enabled: bool | None = None
    on_missed: str | None = None
    notify: bool | None = None


class PreviewBody(BaseModel):
    schedule: dict | str


def _schedule(value):
    """A schedule dict from a dict or English/cron text; errors become 400."""
    try:
        return parse_schedule(value)[0] if isinstance(value, str) else validate_schedule(value)
    except ScheduleError as error:
        fail(400, str(error))


def _fields(body: RoutineBody):
    fields = {}
    for key in ("name", "enabled", "on_missed", "notify"):
        if getattr(body, key) is not None:
            fields[key] = getattr(body, key)
    if body.schedule is not None:
        fields["schedule"] = _schedule(body.schedule)
    if body.target is not None:
        fields["target"] = body.target
    elif body.prompt is not None:
        fields["target"] = {"kind": "prompt", "prompt": body.prompt}
    return fields


def _public(state, routine):
    return state.routines.store.public(routine, state.routines.running_ids())


@router.get("/routines")
def list_routines(state: WebState = Depends(web)):
    scheduler = state.routines
    notices = {}
    lines = legacy_cron_lines(scheduler.store)
    if lines:
        notices["legacy_cron"] = {
            "lines": lines,
            "how_to_remove": "Run `crontab -e` and delete just these lines. Jarvis never edits your crontab.",
        }
    return {
        "routines": scheduler.listing(),
        "notices": notices,
        "scheduler_running": scheduler.running,
        "unread": len(scheduler.store.unread()),
    }


@router.post("/routines/preview")
def preview(body: PreviewBody, state: WebState = Depends(web)):
    """The next 3 run times for a schedule; a problem is reported in `error` (HTTP 200)."""
    try:
        schedule = parse_schedule(body.schedule)[0] if isinstance(body.schedule, str) else validate_schedule(body.schedule)
    except ScheduleError as error:
        return {"error": str(error), "next_runs": []}
    runs = upcoming(schedule, datetime.now(), 3)
    return {"schedule": schedule, "description": describe(schedule),
            "next_runs": [moment.isoformat(timespec="seconds") for moment in runs]}


@router.get("/routines/unread")
def unread(state: WebState = Depends(web)):
    items = state.routines.store.unread()
    return {"count": len(items), "items": items[:20]}


@router.post("/routines/seen")
def mark_seen(state: WebState = Depends(web)):
    state.routines.store.mark_seen()
    return {"count": 0}


@router.post("/routines/notices/dismiss")
def dismiss_notice(state: WebState = Depends(web)):
    store = state.routines.store
    dismissed = set(store.state().get("dismissed_cron", []))
    store.set_state(dismissed_cron=sorted(dismissed | set(legacy_cron_lines(store))))
    return {"dismissed": True}


@router.post("/routines", status_code=201)
def create_routine(body: RoutineBody, state: WebState = Depends(web)):
    fields = _fields(body)
    if "schedule" not in fields or "target" not in fields:
        fail(400, "A routine needs a name, a schedule and something to do")
    try:
        routine = state.routines.store.create(
            fields.pop("name", ""), fields.pop("schedule"), fields.pop("target"), **fields)
    except (ValueError, ScheduleError) as error:
        fail(400, str(error))
    return _public(state, routine)


@router.patch("/routines/{routine_id}")
def update_routine(routine_id: str, body: RoutineBody, state: WebState = Depends(web)):
    check_id(routine_id, "routine id")
    try:
        routine = state.routines.store.update(routine_id, **_fields(body))
    except KeyError:
        fail(404, "Routine not found")
    except (ValueError, ScheduleError) as error:
        fail(400, str(error))
    return _public(state, routine)


@router.delete("/routines/{routine_id}")
def delete_routine(routine_id: str, state: WebState = Depends(web)):
    check_id(routine_id, "routine id")
    try:
        state.routines.store.delete(routine_id)
    except KeyError:
        fail(404, "Routine not found")
    return {"deleted": routine_id}


@router.post("/routines/{routine_id}/run", status_code=202)
def run_routine(routine_id: str, state: WebState = Depends(web)):
    check_id(routine_id, "routine id")
    result = state.routines.run_now(routine_id)
    if not result["started"]:
        fail(404 if "not found" in result["error"].lower() else 409, result["error"])
    return result


@router.get("/routines/{routine_id}/runs")
def routine_runs(routine_id: str, state: WebState = Depends(web)):
    check_id(routine_id, "routine id")
    try:
        routine = state.routines.store.get(routine_id)
    except KeyError:
        fail(404, "Routine not found")
    return {"routine": {"id": routine["id"], "name": routine["name"]}, "runs": routine.get("runs", [])}
