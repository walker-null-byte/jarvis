"""Routines: cron + English parsing, store, scheduler runs, migration, tools and API."""

import json
import time
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from jarvis import routines
from jarvis.llm import LLM
from jarvis.routine_schedule import (
    ScheduleError, cron_next, describe, next_after, next_runs, parse_cron, parse_schedule, upcoming,
    validate_schedule,
)
from jarvis.routines import RoutineStore
from tests.test_webui import _config, env  # noqa: F401

NOW = datetime(2026, 11, 25, 10, 0)  # a Wednesday


def dt(*args):
    return datetime(*args)


# ---------- cron ----------

def test_cron_basic_fields_and_next_runs():
    assert next_runs("*/15 * * * *", NOW, 3) == [dt(2026, 11, 25, 10, 15), dt(2026, 11, 25, 10, 30), dt(2026, 11, 25, 10, 45)]
    assert next_runs("0 9 * * *", NOW, 2) == [dt(2026, 11, 26, 9, 0), dt(2026, 11, 27, 9, 0)]
    assert next_runs("30 10 * * *", NOW, 1) == [dt(2026, 11, 25, 10, 30)]
    assert next_runs("0 10 * * *", NOW, 1) == [dt(2026, 11, 26, 10, 0)]  # strictly after


def test_cron_lists_ranges_steps_and_names():
    spec = parse_cron("0,30 8-10/2 * jan,mar-may mon-fri")
    assert spec.minutes == {0, 30} and spec.hours == {8, 10}
    assert spec.months == {1, 3, 4, 5} and spec.weekdays == {1, 2, 3, 4, 5}
    assert parse_cron("5/20 * * * *").minutes == {5, 25, 45}  # a/n runs to the end of the range
    assert parse_cron("0 0 * * 7").weekdays == {0}  # 7 is Sunday
    assert parse_cron("0 0 * * SUN,Sat").weekdays == {0, 6}


def test_cron_weekday_range_next():
    # Wednesday 10:00 -> next weekday 09:00 is Thursday; after Friday it skips the weekend
    assert next_runs("0 9 * * 1-5", NOW, 1) == [dt(2026, 11, 26, 9, 0)]
    assert next_runs("0 9 * * 1-5", dt(2026, 11, 27, 10, 0), 1) == [dt(2026, 11, 30, 9, 0)]


def test_cron_vixie_day_of_month_or_day_of_week():
    # both restricted: the 1st OR any Monday
    runs = next_runs("0 9 1 * mon", dt(2026, 11, 25, 12, 0), 3)
    assert runs == [dt(2026, 11, 30, 9, 0), dt(2026, 12, 1, 9, 0), dt(2026, 12, 7, 9, 0)]
    # a star in either field means AND (the star matches everything)
    spec = parse_cron("0 9 * * mon")
    assert spec.days_star and not spec.weekdays_star
    assert not spec.day_matches(dt(2026, 11, 25))  # Wednesday
    assert spec.day_matches(dt(2026, 11, 30))


def test_cron_month_end_and_leap_day():
    assert next_runs("0 0 31 * *", dt(2026, 4, 1), 1) == [dt(2026, 5, 31, 0, 0)]
    assert next_runs("0 0 29 2 *", dt(2026, 1, 1), 1) == [dt(2028, 2, 29, 0, 0)]
    assert cron_next(parse_cron("0 0 31 2 *"), dt(2026, 1, 1)) is None  # never happens


def test_cron_aliases():
    assert parse_cron("@daily") == parse_cron("0 0 * * *")
    assert next_runs("@hourly", NOW, 1) == [dt(2026, 11, 25, 11, 0)]


@pytest.mark.parametrize("bad", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *",
                                 "* * * 13 *", "* * * * 8", "*/0 * * * *", "a * * * *", "5-1 * * * *", ", * * * *"])
def test_cron_rejects_bad_expressions(bad):
    with pytest.raises(ScheduleError):
        parse_cron(bad)


def test_cron_skips_nonexistent_dst_minutes(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        if datetime.fromtimestamp(datetime(2026, 3, 8, 7, 30).timestamp()).hour != 3:  # 02:30 EST jumps to 03:30 EDT
            pytest.skip("time zone database unavailable")
        # 02:30 does not exist on 2026-03-08, so the next run is the 9th
        assert next_runs("30 2 * * *", dt(2026, 3, 7, 12, 0), 2) == [dt(2026, 3, 9, 2, 30), dt(2026, 3, 10, 2, 30)]
    finally:
        monkeypatch.undo()
        time.tzset()


# ---------- schedule dicts ----------

def test_validate_and_next_after_kinds():
    assert validate_schedule({"kind": "interval", "every_minutes": "5"}) == {"kind": "interval", "every_minutes": 5}
    assert next_after({"kind": "interval", "every_minutes": 5}, NOW) == NOW + timedelta(minutes=5)
    once = {"kind": "once", "at": "2026-12-01T09:00:00"}
    assert next_after(once, NOW) == dt(2026, 12, 1, 9, 0)
    assert next_after(once, dt(2026, 12, 2)) is None
    assert upcoming({"kind": "interval", "every_minutes": 60}, NOW, 3)[-1] == NOW + timedelta(hours=3)
    for bad in ({"kind": "nope"}, {"kind": "interval", "every_minutes": 0}, {"kind": "once", "at": "soon"},
                {"kind": "cron", "cron": "x"}, "text"):
        with pytest.raises(ScheduleError):
            validate_schedule(bad)


# ---------- English ----------

@pytest.mark.parametrize("text,cron,description", [
    ("every weekday at 9", "0 9 * * 1-5", "Weekdays at 09:00"),
    ("weekdays at 9:30 am", "30 9 * * 1-5", "Weekdays at 09:30"),
    ("every morning", "0 8 * * *", "Daily at 08:00"),
    ("every evening", "0 18 * * *", "Daily at 18:00"),
    ("daily", "0 9 * * *", "Daily at 09:00"),
    ("every day at 5:45pm", "45 17 * * *", "Daily at 17:45"),
    ("every day at noon", "0 12 * * *", "Daily at 12:00"),
    ("weekly on friday at 5pm", "0 17 * * 5", "Fridays at 17:00"),
    ("every monday at 8", "0 8 * * 1", "Mondays at 08:00"),
    ("every mon, wed and fri at 07:15", "15 7 * * 1,3,5", "Mon, Wed, Fri at 07:15"),
    ("every weekend at 10am", "0 10 * * 0,6", "Weekends at 10:00"),
    ("on the 1st at 8", "0 8 1 * *", "Monthly on the 1st at 08:00"),
    ("monthly on the 15th at 18:00", "0 18 15 * *", "Monthly on the 15th at 18:00"),
    ("hourly", "0 * * * *", "Every hour, on the hour"),
    ("0 9 * * 1-5", "0 9 * * 1-5", "Weekdays at 09:00"),
    ("12am daily", "0 0 * * *", "Daily at 00:00"),
])
def test_english_cron_phrases(text, cron, description):
    schedule, text_description = parse_schedule(text, NOW)
    assert schedule == {"kind": "cron", "cron": cron}
    assert text_description == description == describe(schedule)


def test_english_intervals_and_once():
    assert parse_schedule("every 15 minutes", NOW)[0] == {"kind": "interval", "every_minutes": 15}
    assert parse_schedule("every 2 hours", NOW)[1] == "Every 2 hours"
    assert parse_schedule("every hour", NOW)[0]["every_minutes"] == 60
    assert parse_schedule("every minute", NOW)[1] == "Every minute"
    assert parse_schedule("in 20 minutes", NOW)[0] == {"kind": "once", "at": "2026-11-25T10:20:00"}
    assert parse_schedule("in an hour", NOW)[0]["at"] == "2026-11-25T11:00:00"
    assert parse_schedule("tomorrow at 8", NOW)[0]["at"] == "2026-11-26T08:00:00"
    assert parse_schedule("at 2026-12-01 09:00", NOW)[0]["at"] == "2026-12-01T09:00:00"
    assert parse_schedule("at 18:30", NOW)[0]["at"] == "2026-11-25T18:30:00"
    assert parse_schedule("at 9am", NOW)[0]["at"] == "2026-11-26T09:00:00"  # already past today
    assert parse_schedule("friday at 5pm", NOW)[0]["at"] == "2026-11-27T17:00:00"
    assert "Once on Fri 27 Nov 2026" in parse_schedule("friday at 5pm", NOW)[1]


@pytest.mark.parametrize("bad", ["", "whenever", "every banana", "at 25:00", "tomorrow at 13pm",
                                 "at 2020-01-01 09:00", "on the 40th at 9", "99 * * * *"])
def test_english_errors_list_examples(bad):
    with pytest.raises(ScheduleError) as error:
        parse_schedule(bad, NOW)
    assert str(error.value)
    if bad == "whenever":
        assert "every weekday" in str(error.value)


def test_describe_cron_variants():
    assert describe({"kind": "cron", "cron": "*/10 * * * *"}) == "Every 10 minutes"
    assert describe({"kind": "cron", "cron": "15 * * * *"}) == "Every hour at :15"
    assert describe({"kind": "cron", "cron": "0 */6 * * *"}) == "Every 6 hours"
    assert describe({"kind": "cron", "cron": "0 9 1,15 * *"}) == "Monthly on the 1st, 15th at 09:00"
    assert describe({"kind": "cron", "cron": "0 9 * 6 *"}).startswith("Cron:")
    assert describe({"kind": "interval", "every_minutes": 1440}) == "Every day"
    assert describe({"kind": "interval", "every_minutes": 90}) == "Every 90 minutes"


# ---------- store ----------

PROMPT = {"kind": "prompt", "prompt": "Check disk space"}


@pytest.fixture
def store(tmp_path):
    return RoutineStore(_config(tmp_path))


def test_store_create_update_delete(store):
    routine = store.create("Disk check", {"kind": "cron", "cron": "0 9 * * *"}, PROMPT)
    assert routine["enabled"] and routine["next_run"] and routine["runs"] == []
    assert (store.dir / f"{routine['id']}.json").exists()
    assert store.find("disk CHECK")["id"] == routine["id"]
    paused = store.update(routine["id"], enabled=False)
    assert paused["next_run"] is None
    moved = store.update(routine["id"], enabled=True, schedule={"kind": "interval", "every_minutes": 30})
    assert datetime.fromisoformat(moved["next_run"]) > datetime.now()
    store.delete(routine["id"])
    assert store.list() == []
    with pytest.raises(KeyError):
        store.get(routine["id"])


@pytest.mark.parametrize("kwargs", [
    {"name": "", "schedule": {"kind": "interval", "every_minutes": 5}, "target": PROMPT},
    {"name": "x", "schedule": {"kind": "bad"}, "target": PROMPT},
    {"name": "x", "schedule": {"kind": "interval", "every_minutes": 5}, "target": {"kind": "prompt", "prompt": " "}},
    {"name": "x", "schedule": {"kind": "interval", "every_minutes": 5}, "target": {"kind": "agent_action", "agent": "a"}},
    {"name": "x", "schedule": {"kind": "interval", "every_minutes": 5}, "target": PROMPT, "on_missed": "maybe"},
])
def test_store_rejects_invalid(store, kwargs):
    with pytest.raises((ValueError, ScheduleError)):
        store.create(**kwargs)


def test_store_ids_are_safe_paths(store):
    with pytest.raises(KeyError):
        store.get("../../etc/passwd")
    assert RoutineStore(store.config).list() == []


def test_run_history_keeps_50_newest_first(store):
    routine = store.create("x", {"kind": "interval", "every_minutes": 5}, PROMPT)
    for index in range(60):
        store.add_run(routine["id"], {"id": str(index), "started": now(index), "status": "ok"})
    runs = store.get(routine["id"])["runs"]
    assert len(runs) == 50 and runs[0]["id"] == "59"


def now(offset=0):
    return (datetime(2026, 1, 1) + timedelta(minutes=offset)).isoformat()


def test_unread_tracks_seen_and_notify(store):
    loud = store.create("loud", {"kind": "interval", "every_minutes": 5}, PROMPT)
    quiet = store.create("quiet", {"kind": "interval", "every_minutes": 5}, PROMPT, notify=False)
    for routine in (loud, quiet):
        store.add_run(routine["id"], {"id": "r", "started": now(), "finished": now_text(-5), "status": "ok"})
    assert [item["name"] for item in store.unread()] == ["loud"]
    store.mark_seen()
    assert store.unread() == []


# ---------- migration ----------

JOB = """# Job: nightly
Schedule: 30 2 * * *
Created: 2026-01-01 00:00:00

## Instruction
Back up the notes.

## Status
- enabled: false
- last_run: never
"""


def test_migration_is_once_and_keeps_old_files(store):
    jobs = routines.mind_dir(store.config) / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "nightly.md").write_text(JOB)
    (jobs / "nightly.status.json").write_text(json.dumps({
        "status": "succeeded", "last_run": "2026-02-01T02:30:00", "last_finished": "2026-02-01T02:31:00",
        "last_result": "backed up"}))
    (jobs / "broken.md").write_text("# Job: broken\nSchedule: nope\n\n## Instruction\nx\n")
    assert routines.migrate_jobs(store) == ["nightly"]
    routine = store.get("nightly")
    assert routine["schedule"] == {"kind": "cron", "cron": "30 2 * * *"}
    assert routine["target"]["prompt"] == "Back up the notes." and routine["enabled"] is False
    assert routine["runs"][0]["summary"] == "backed up" and routine["on_missed"] == "skip"
    assert (jobs / "nightly.md").exists()
    marker = json.loads((store.dir / "_migrated.json").read_text())
    assert marker["jobs"] == {"nightly": "nightly"} and "broken" in marker["skipped"]
    store.delete("nightly")
    assert routines.migrate_jobs(store) == []  # idempotent, even after deleting
    assert store.list() == []


def test_legacy_cron_lines_reported_not_changed(store, monkeypatch, tmp_path):
    entry = "* * * * * /py /x/jarvis/run_job.py nightly >> /x/log 2>&1"
    monkeypatch.setattr(routines, "_read_system_crontab", lambda: ["# comment", entry, "0 1 * * * other"])
    store.config["jobs"]["crontab_file"] = str(tmp_path / "none")
    assert routines.legacy_cron_lines(store) == [entry]
    store.set_state(dismissed_cron=[entry])
    assert routines.legacy_cron_lines(store) == []


# ---------- scheduler ----------

def _wait(predicate, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def scheduler(env):  # noqa: F811
    sched = env.state.routines
    sched.tick = 0.05
    yield sched
    sched.stop(timeout=2)


def test_manual_run_creates_routine_session_and_history(env, scheduler):
    routine = scheduler.store.create("Hello", {"kind": "interval", "every_minutes": 60}, PROMPT)
    events = []
    env.jarvis.events.subscribe(lambda e: events.append(e) if e.kind.startswith("routine") else None)
    assert scheduler.run_now(routine["id"]) == {"started": True}
    assert _wait(lambda: scheduler.store.get(routine["id"])["runs"] and
                 scheduler.store.get(routine["id"])["runs"][0]["status"] == "ok")
    run = scheduler.store.get(routine["id"])["runs"][0]
    assert run["summary"] == "Hello from fake" and run["duration"] >= 0 and run["trigger"] == "manual"
    meta = env.jarvis.sessions.get(run["session_id"])
    assert meta["kind"] == "routine" and meta["owner"] == routine["id"] and meta["title"].startswith("Hello — ")
    kinds = [e.kind for e in events]
    assert "routine_run_started" in kinds and "routine_run_finished" in kinds and "routines_changed" in kinds
    finished = next(e for e in events if e.kind == "routine_run_finished")
    assert finished.data["routine_id"] == routine["id"] and finished.data["run"]["status"] == "ok"
    assert scheduler.store.unread()[0]["routine_id"] == routine["id"]


def test_tick_runs_due_routine_and_reschedules(env, scheduler):
    routine = scheduler.store.create("Due", {"kind": "interval", "every_minutes": 30}, PROMPT)
    scheduler.store.patch(routine["id"], next_run=now_text(-5))  # 5 seconds ago: on time, not missed
    scheduler.tick_once()
    assert _wait(lambda: scheduler.store.get(routine["id"])["runs"] and
                 scheduler.store.get(routine["id"])["runs"][0]["status"] == "ok")
    after = scheduler.store.get(routine["id"])
    assert after["runs"][0]["trigger"] == "schedule"
    assert datetime.fromisoformat(after["next_run"]) > datetime.now() + timedelta(minutes=29)
    scheduler.tick_once()  # not due again
    time.sleep(0.2)
    assert len(scheduler.store.get(routine["id"])["runs"]) == 1


def now_text(seconds):
    return (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_missed_run_once_catches_up_and_skip_reschedules(env, scheduler):
    catch = scheduler.store.create("Catch", {"kind": "interval", "every_minutes": 30}, PROMPT, on_missed="run_once")
    skip = scheduler.store.create("Skip", {"kind": "interval", "every_minutes": 30}, PROMPT, on_missed="skip")
    for routine in (catch, skip):
        scheduler.store.patch(routine["id"], next_run=now_text(-3600))
    scheduler.tick_once()
    assert _wait(lambda: scheduler.store.get(catch["id"])["runs"] and
                 scheduler.store.get(catch["id"])["runs"][0]["status"] == "ok")
    assert scheduler.store.get(catch["id"])["runs"][0]["trigger"] == "catchup"
    skipped = scheduler.store.get(skip["id"])
    assert skipped["runs"] == [] and datetime.fromisoformat(skipped["next_run"]) > datetime.now()


def test_once_routine_disables_itself_after_running(env, scheduler):
    at = (datetime.now() + timedelta(seconds=1)).isoformat(timespec="seconds")
    routine = scheduler.store.create("Once", {"kind": "once", "at": at}, PROMPT)
    scheduler.store.patch(routine["id"], next_run=now_text(-1))
    scheduler.tick_once()
    assert _wait(lambda: scheduler.store.get(routine["id"])["runs"])
    after = scheduler.store.get(routine["id"])
    assert after["enabled"] is False and after["next_run"] is None


def test_never_runs_same_routine_twice_and_caps_concurrency(env, scheduler):
    import threading
    gate = threading.Event()

    env.gate = gate
    made = [scheduler.store.create(f"R{i}", {"kind": "interval", "every_minutes": 60}, PROMPT) for i in range(3)]
    try:
        assert scheduler.run_now(made[0]["id"])["started"]
        assert "already running" in scheduler.run_now(made[0]["id"])["error"]
        assert scheduler.run_now(made[1]["id"])["started"]
        assert "try again" in scheduler.run_now(made[2]["id"])["error"]  # cap of 2
        assert scheduler.running_ids() == {made[0]["id"], made[1]["id"]}
    finally:
        gate.set()
    assert _wait(lambda: not scheduler.running_ids())


def test_failed_run_is_recorded_and_scheduler_survives(env, scheduler, monkeypatch):
    def boom(llm_self, messages, tools=None, on_text=None, **kwargs):
        raise RuntimeError("model offline")
    monkeypatch.setattr(LLM, "chat", boom)
    routine = scheduler.store.create("Fails", {"kind": "interval", "every_minutes": 60}, PROMPT)
    scheduler.run_now(routine["id"])
    assert _wait(lambda: scheduler.store.get(routine["id"])["runs"] and
                 scheduler.store.get(routine["id"])["runs"][0]["status"] == "error")
    assert "model offline" in scheduler.store.get(routine["id"])["runs"][0]["error"]
    scheduler.store.patch(routine["id"], next_run="not a date")
    scheduler.store.patch(routine["id"], enabled=True)
    scheduler.start()  # the loop survives a broken record
    time.sleep(0.3)
    assert scheduler.running


def test_unattended_approval_is_denied_and_recorded(env, scheduler, monkeypatch):
    calls = {"n": 0}

    def chat(llm_self, messages, tools=None, on_text=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "run_command", "arguments": json.dumps({"command": "sudo echo hi"})}}]}
        return {"role": "assistant", "content": "I could not do it."}

    monkeypatch.setattr(LLM, "chat", chat)
    monkeypatch.setattr(scheduler, "_ui_connected", lambda: False)
    routine = scheduler.store.create("Needs approval", {"kind": "interval", "every_minutes": 60}, PROMPT)
    scheduler.run_now(routine["id"])
    assert _wait(lambda: scheduler.store.get(routine["id"])["runs"] and
                 scheduler.store.get(routine["id"])["runs"][0]["status"] != "running", timeout=10)
    run = scheduler.store.get(routine["id"])["runs"][0]
    assert run["status"] == "needs_approval" and "needed approval:" in run["error"]
    assert "sudo echo hi" in run["error"]


def test_interrupted_runs_are_closed_on_start(env, scheduler):
    routine = scheduler.store.create("Stale", {"kind": "interval", "every_minutes": 60}, PROMPT)
    scheduler.store.add_run(routine["id"], {"id": "old", "started": now(), "status": "running"})
    scheduler.start()
    run = scheduler.store.get(routine["id"])["runs"][0]
    assert run["status"] == "error" and "Interrupted" in run["error"]


def test_stop_is_clean(env, scheduler):
    scheduler.start()
    assert scheduler.running
    scheduler.stop(timeout=2)
    assert not scheduler.running and routines.active_scheduler() is None


# ---------- tools ----------

def test_tools_create_list_update_delete_and_aliases(env, scheduler):
    tools = routines.build_routine_tools(env.jarvis.config)
    scheduler.start()
    created = tools["routine_create"]("Email check", "every weekday at 9", "Check my email")
    assert created["created"]["schedule"] == "Weekdays at 09:00" and len(created["created"]["next_runs"]) == 3
    assert "warning" not in created
    bad = tools["routine_create"]("Bad", "whenever", "x")
    assert "every weekday" in bad["error"]
    assert tools["routine_create"]("No prompt", "daily", "")["error"]
    listing = tools["routine_list"]()["routines"]
    assert [r["name"] for r in listing] == ["Email check"]
    routine_id = listing[0]["id"]
    assert tools["routine_update"]("email check", schedule="every 30 minutes")["updated"]["schedule"] == "Every 30 minutes"
    assert tools["routine_update"](routine_id, enabled=False)["updated"]["enabled"] is False
    assert "error" in tools["routine_update"]("nope", name="x")
    assert tools["cron_list"]()["routines"][0]["id"] == routine_id
    assert tools["cron_add"]("old style", "0 2 * * *", "Back up")["created"]["schedule"] == "Daily at 02:00"
    assert tools["cron_disable"]("old style")["updated"]["enabled"] is False
    assert tools["routine_run_now"](routine_id)["started"] is True
    assert _wait(lambda: not scheduler.running_ids())
    assert tools["routine_delete"](routine_id) == {"deleted": "Email check"}
    assert tools["cron_remove"]("old style") == {"deleted": "old style"}


def test_tools_warn_without_scheduler_and_hide_aliases(tmp_path):
    from jarvis.tool_exposure import ALIAS_TOOLS
    config = _config(tmp_path)
    tools = routines.build_routine_tools(config)
    assert routines.active_scheduler() is None
    assert "warning" in tools["routine_create"]("A", "daily", "x")
    assert "error" in tools["routine_run_now"]("A")
    assert ALIAS_TOOLS == {n for n in tools if n.startswith("cron_")}


def test_registry_lists_routine_tools_not_aliases(tmp_path):
    from jarvis.registry import ToolRegistry
    from jarvis.tool_exposure import ToolExposure
    config = _config(tmp_path)
    registry = ToolRegistry(config)
    exposure = ToolExposure(registry, config, tmp_path)
    assert "routine_list" in exposure.hidden_names() and "cron_add" not in exposure.hidden_names()
    assert "routine_create" in exposure.core_names()
    assert "cron_add" in registry.tool_names()  # still callable
    assert "Unknown tool" not in str(exposure.run("cron_list", {}))


# ---------- API ----------

def test_api_crud_preview_and_run(env):
    client = env.client
    preview = client.post("/api/routines/preview", json={"schedule": "every weekday at 9"}).json()
    assert preview["description"] == "Weekdays at 09:00" and len(preview["next_runs"]) == 3
    error = client.post("/api/routines/preview", json={"schedule": "whenever"})
    assert error.status_code == 200 and "every weekday" in error.json()["error"]
    assert client.post("/api/routines/preview", json={"schedule": {"kind": "cron", "cron": "bad"}}).json()["error"]

    created = client.post("/api/routines", json={"name": "Disk", "schedule": "every 20 minutes", "prompt": "Check disks"})
    assert created.status_code == 201
    body = created.json()
    assert body["schedule_text"] == "Every 20 minutes" and body["last_status"] == "never"
    assert client.post("/api/routines", json={"name": "x", "schedule": "nonsense", "prompt": "p"}).status_code == 400
    assert client.post("/api/routines", json={"name": "x"}).status_code == 400

    listing = client.get("/api/routines").json()
    assert [r["id"] for r in listing["routines"]] == [body["id"]] and listing["unread"] == 0
    patched = client.patch(f"/api/routines/{body['id']}", json={"enabled": False, "notify": False}).json()
    assert patched["enabled"] is False and patched["next_run"] is None and patched["notify"] is False
    assert client.patch("/api/routines/missing", json={"name": "x"}).status_code == 404
    assert client.patch(f"/api/routines/{body['id']}", json={"schedule": {"kind": "nope"}}).status_code == 400

    assert client.post(f"/api/routines/{body['id']}/run").status_code == 202
    assert _wait(lambda: client.get(f"/api/routines/{body['id']}/runs").json()["runs"]
                 and client.get(f"/api/routines/{body['id']}/runs").json()["runs"][0]["status"] == "ok")
    assert client.post("/api/routines/missing/run").status_code == 404
    assert client.delete(f"/api/routines/{body['id']}").json() == {"deleted": body["id"]}
    assert client.get(f"/api/routines/{body['id']}/runs").status_code == 404
    assert client.get("/api/routines").json()["routines"] == []


def test_api_unread_seen_and_notice(env, monkeypatch, tmp_path):
    client = env.client
    body = client.post("/api/routines", json={"name": "N", "schedule": "every hour", "prompt": "p"}).json()
    env.state.routines.store.add_run(body["id"], {
        "id": "r", "started": now(), "finished": now_text(-5), "status": "ok", "summary": "hi"})
    assert client.get("/api/routines/unread").json()["count"] == 1
    assert client.get("/api/routines").json()["unread"] == 1
    assert client.post("/api/routines/seen").json() == {"count": 0}
    assert client.get("/api/routines/unread").json()["count"] == 0

    entry = "* * * * * /py /x/run_job.py nightly"
    monkeypatch.setattr(routines, "_read_system_crontab", lambda: [entry])
    env.jarvis.config["jobs"]["crontab_file"] = str(tmp_path / "none")
    notice = client.get("/api/routines").json()["notices"]["legacy_cron"]
    assert notice["lines"] == [entry] and "crontab -e" in notice["how_to_remove"]
    client.post("/api/routines/notices/dismiss")
    assert client.get("/api/routines").json()["notices"] == {}


def test_api_requires_token(env):
    from fastapi.testclient import TestClient
    anonymous = TestClient(env.app, base_url="http://127.0.0.1:7777")
    assert anonymous.get("/api/routines").status_code == 401


def test_run_now_pushes_back_interval_next_run(env, scheduler):
    routine = scheduler.store.create("Iv", {"kind": "interval", "every_minutes": 120}, PROMPT)
    scheduler.store.patch(routine["id"], next_run=now_text(-5))
    scheduler.run_now(routine["id"])
    assert datetime.fromisoformat(scheduler.store.get(routine["id"])["next_run"]) > datetime.now() + timedelta(minutes=100)
    assert _wait(lambda: not scheduler.running_ids())


def test_migrated_job_still_in_crontab_starts_paused(store, monkeypatch):
    jobs = routines.mind_dir(store.config) / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "hourly.md").write_text(
        "# Job: hourly\nSchedule: 0 * * * *\nEnabled: true\n\n## Instruction\nCheck disk.\n")
    (jobs / "other.md").write_text(
        "# Job: other\nSchedule: 0 * * * *\nEnabled: true\n\n## Instruction\nSay hi.\n")
    monkeypatch.setattr(routines, "_read_system_crontab",
                        lambda: ["0 * * * * python /x/jarvis/run_job.py hourly >> /x/hourly.log 2>&1"])
    routines.migrate_jobs(store)
    assert store.get("hourly")["enabled"] is False
    assert store.get("other")["enabled"] is True
