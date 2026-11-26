"""Agent Builder: TOML rendering, strict checks, builder tools, staging/install, test runner, routes."""

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from jarvis.runtime import profile_for
from jarvis.specialists import builder as builder_mod
from jarvis.specialists.builder import (KNOWN_ICONS, bump_patch, check_draft, file_changes, guide_section,
                                        manifest_changes, render_toml)
from jarvis.specialists.manager import AgentError
from jarvis.specialists.tool_runner import run_tool_subprocess
from tests.test_specialists import MINI, write_plugin
from tests.test_webui import Env

BUILTIN = Path(__file__).resolve().parent.parent / "jarvis" / "agents_builtin"
JARVIS_TOOLS = ["web_search", "fetch_page", "read_file", "get_current_time"]
PROMPT = "You are a test agent. Follow the user's instructions carefully, step by step, and never invent facts."

GOOD_TOOL = '''
def register(ctx):
    @ctx.tool
    def lookup(word: str) -> dict:
        """Look a word up. Use it when the user asks what a word means."""
        key = ctx.settings.get("API_KEY")
        try:
            response = ctx.http.get("https://example.com/define", params={"q": word}, headers={"Authorization": key})
            response.raise_for_status()
        except Exception as error:
            return {"error": str(error)}
        return {"word": word}
'''


def manifest(**changes):
    data = {"schema": 1, "id": "t-agent", "name": "T Agent", "version": "0.1.0", "description": "x", "author": "You",
            "icon": "book", "agent": {"prompt": "prompt.md"},
            "permissions": {"network": False, "commands": False, "files": "data"}}
    data.update(changes)
    return data


def folder_for(tmp_path, data=None, tools=None, prompt=PROMPT, extra=None):
    folder = tmp_path / "plugin"
    folder.mkdir(exist_ok=True)
    data = manifest() if data is None else data
    (folder / "agent.toml").write_text(render_toml(data), encoding="utf-8")
    if prompt is not None:
        (folder / "prompt.md").write_text(prompt, encoding="utf-8")
    for name, text in {**(tools or {}), **(extra or {})}.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def problems_of(tmp_path, data=None, **kwargs):
    folder = folder_for(tmp_path, data, **kwargs)
    return check_draft(folder, manifest() if data is None else data, JARVIS_TOOLS)["problems"]


# ---------- render_toml ----------

def test_render_toml_round_trips_lead_finder():
    data = tomllib.loads((BUILTIN / "lead-finder" / "agent.toml").read_text(encoding="utf-8"))
    assert tomllib.loads(render_toml(data)) == data
    assert render_toml(data).startswith("schema = 1\nid = \"lead-finder\"")


def test_render_toml_edge_cases():
    nasty = 'He said "hi"\\ and\nleft\té中\U0001f600 """ end"'
    data = {"schema": 1, "name": nasty, "description": '"', "tags": [], "settings": [],
            "agent": {"prompt": "prompt.md", "greeting": 'multi\nline "x"\r\nwith\\slash\n\x7f', "suggestions": []},
            "permissions": {}, "records": [{"name": "t", "fields": [{"name": "a", "type": "text", "default": "x\ny"}],
                                            "unique": []}],
            "actions": [{"id": "a", "prompt": "line1\nline2\n", "inputs": []}, {"id": "b", "prompt": "\nstarts blank"}],
            "ratio": 0.5, "big": 12345678901234567890, "neg": -3, "flag": True, "weird key": {"inner key": [1, 2.5]},
            "long": ["x" * 40, "y" * 40, "z" * 40]}
    assert tomllib.loads(render_toml(data)) == data


@pytest.mark.parametrize("bad", [{"a": None}, {"a": object()}, {"a": float("nan")}, {1: "x"}, {"a": "\ud800"}])
def test_render_toml_rejects_non_json(bad):
    with pytest.raises((ValueError, TypeError)):
        render_toml(bad)


# ---------- strict check ----------

def test_a_good_draft_is_ready(tmp_path):
    data = manifest(permissions={"network": True, "commands": False, "files": "data"},
                    settings=[{"key": "API_KEY", "label": "Key", "type": "secret", "required": True}])
    folder = folder_for(tmp_path, data, tools={"tools/dict.py": GOOD_TOOL})
    result = check_draft(folder, data, JARVIS_TOOLS)
    assert result["ok"], result["problems"]
    assert result["tools"][0]["tools"] == ["lookup"] and "network" in result["tools"][0]["uses"]
    assert any("fill in: Key" in n for n in result["notes"]) and any("Trust" in n for n in result["notes"])


def test_check_manifest_issues_and_warnings(tmp_path):
    found = "\n".join(problems_of(tmp_path, manifest(flavour="x", agent={"prompt": "prompt.md", "mood": 1})))
    assert "flavour: unknown key" in found and "agent.mood: unknown key" in found  # warnings are problems too
    found = "\n".join(problems_of(tmp_path, manifest(settings=[{"key": "n", "type": "colour"}])))
    assert "settings[0].type: 'colour'" in found


def test_check_core_tools_icon_prompt_and_action(tmp_path):
    data = manifest(icon="nope", agent={"prompt": "prompt.md", "core_tools": ["web_search", "teleport"]},
                    actions=[{"id": "go", "prompt": "short"}])
    found = "\n".join(problems_of(tmp_path, data, prompt="too short"))
    assert "'teleport' is not a Jarvis tool" in found and "web_search" in found.split("Valid names:")[1]
    assert "icon: 'nope' is not a known icon" in found
    assert "prompt.md: is only 9 characters" in found
    assert "actions → go: the prompt is too short" in found


def test_check_icon_svg_replaces_known_icon(tmp_path):
    data = manifest(icon="custom-thing")
    assert problems_of(tmp_path, data, extra={"icon.svg": "<svg></svg>"}) == []
    found = problems_of(tmp_path, data, extra={"icon.svg": "<svg onload='x()'></svg>"})
    assert any("icon.svg" in p for p in found)


def test_known_icons_cover_the_agents_page():
    assert {"agents", "target", "mail", "book", "lock", "zap"} <= KNOWN_ICONS


def test_check_secret_looking_setting_must_be_secret(tmp_path):
    data = manifest(settings=[{"key": "SERPER_API_KEY", "type": "text"}, {"key": "keyword", "type": "text"},
                              {"key": "githubToken", "type": "secret"}])
    found = problems_of(tmp_path, data)
    assert len(found) == 1 and "SERPER_API_KEY: looks like a secret" in found[0]


@pytest.mark.parametrize("code, expect", [
    ("def register(ctx:\n    pass\n", "syntax error on line 1"),
    ("def helper():\n    pass\n", "needs a top-level `def register(ctx):`"),
    ("def register(ctx):\n    pass\n", "defines no tool"),
    ("def register(ctx):\n    @ctx.tool\n    def go(x: str):\n        return 1\n", "add a docstring"),
    ("import notarealmodule123\ndef register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n",
     "module 'notarealmodule123' is not installed"),
    ("from . import _missing\ndef register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n",
     "needs a helper file tools/_missing.py"),
    ("def register(ctx):\n    @ctx.tool\n    def records_add(x: str):\n        'd'\n", "'records_add' is reserved"),
    ("def register(ctx):\n    @ctx.tool(name='web_search')\n    def go(x: str):\n        'd'\n",
     "Jarvis already has a tool called 'web_search'"),
    ("def register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n        return ctx.http.get(x)\n",
     "uses the network (uses ctx.http) but permissions.network is false"),
    ("import requests\n", "uses the network"),
    ("import subprocess\ndef register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n",
     "runs system commands"),
    ("def register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n        key = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n",
     "looks like an API key"),
    ("TOKEN = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8'\ndef register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n",
     "assigned to 'TOKEN'"),
])
def test_check_python_tool_problems(tmp_path, code, expect):
    found = "\n".join(problems_of(tmp_path, manifest(), tools={"tools/t.py": code}))
    assert expect in found, found


def test_check_duplicate_tool_names_and_helpers(tmp_path):
    one = "def register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n"
    found = problems_of(tmp_path, manifest(), tools={"tools/a.py": one, "tools/b.py": one,
                                                      "tools/_h.py": "import json\n"})
    assert len(found) == 1 and "also used in tools/a.py" in found[0]  # _h.py needs no register
    ok = "from . import _h\ndef register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n"
    (tmp_path / "ok").mkdir()
    assert problems_of(tmp_path / "ok", manifest(), tools={"tools/a.py": ok, "tools/_h.py": "X = 1\n"}) == []


def test_check_never_executes_code(tmp_path):
    marker = tmp_path / "ran"
    code = f"open({str(marker)!r}, 'w')\ndef register(ctx):\n    @ctx.tool\n    def go(x: str):\n        'd'\n"
    assert problems_of(tmp_path, manifest(), tools={"tools/t.py": code}) == []
    assert not marker.exists()


# ---------- changes ----------

def test_manifest_changes_lines():
    old = manifest(settings=[{"key": "x", "type": "text"}],
                   records=[{"name": "leads", "fields": [{"name": "status", "type": "select", "options": ["a"]},
                                                         {"name": "old", "type": "text"}]}],
                   actions=[{"id": "find", "label": "Find", "prompt": "p"}])
    new = manifest(version="0.1.1", settings=[],
                   records=[{"name": "leads", "fields": [{"name": "status", "type": "select", "options": ["a", "b"]},
                                                         {"name": "new", "type": "text"}]}],
                   actions=[{"id": "find", "label": "Find", "prompt": "p"}, {"id": "go", "label": "Find leads", "prompt": "q"}],
                   permissions={"network": True, "commands": False, "files": "data"})
    lines = manifest_changes(old, new)
    assert "Changed version 0.1.0 → 0.1.1" in lines and "Added action 'Find leads'" in lines
    assert "Changed field leads.status (options)" in lines and "Added field leads.new" in lines
    assert "Removed field leads.old" in lines and "Removed setting 'x'" in lines
    assert "Changed permission network: false → true" in lines


def test_file_changes_diffs(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "prompt.md").write_text("one\ntwo\n")
    (tmp_path / "b" / "prompt.md").write_text("one\nthree\n")
    (tmp_path / "a" / "gone.md").write_text("x")
    (tmp_path / "b" / "new.py").write_text("y" * 9000)
    changes = {c["path"]: c for c in file_changes(tmp_path / "a", tmp_path / "b")}
    assert changes["prompt.md"]["status"] == "changed" and "+three" in changes["prompt.md"]["diff"]
    assert changes["gone.md"]["status"] == "removed" and changes["new.py"]["status"] == "added"
    assert len(changes["new.py"]["diff"]) < 4200 and changes["new.py"]["diff"].endswith("(diff cut)")


def test_bump_patch():
    assert bump_patch("0.1.0") == "0.1.1" and bump_patch("1.2") == "1.2.1" and bump_patch("1.0.9-beta") == "1.0.10-beta"


def test_guide_topics_ship_with_the_package():
    for topic in ("overview", "settings", "records", "actions", "exports", "templates", "tools", "permissions",
                  "examples", "checklist"):
        assert guide_section(topic)["text"].startswith(f"## {topic}")
    assert "topics" in guide_section("nope") and "ctx.http" in guide_section("tools")["text"]
    pyproject = (Path(builder_mod.__file__).parents[2] / "pyproject.toml").read_text()
    assert '"*" = ["*.md", "*.toml"]' in pyproject


def test_guide_examples_pass_the_strict_check(tmp_path):
    """The JSON in the guide is what the tools accept: build the guide's example agent and check it."""
    env_data = manifest(
        icon="book", permissions={"network": True, "commands": False, "files": "data"},
        settings=[{"key": "BOOKS_API_KEY", "label": "Books API key", "type": "secret"}],
        records=[{"name": "books", "label": "Books", "unique": ["title", "author"], "title_field": "title", "fields": [
            {"name": "title", "type": "text", "required": True}, {"name": "author", "type": "text"},
            {"name": "status", "type": "select", "options": ["want", "reading", "done"], "default": "want", "editable": True},
            {"name": "rating", "type": "number", "min": 1, "max": 5, "editable": True}]}],
        actions=[{"id": "suggest", "label": "Suggest books", "icon": "sparkle", "records": "books",
                  "inputs": [{"key": "count", "label": "How many", "type": "number", "default": 3, "min": 1, "max": 10},
                             {"key": "mood", "label": "Mood", "type": "text"}],
                  "prompt": "Suggest {count} books. Mood: {mood}. Have {records.books.count}. records_query(where={\"status\": \"done\"})"}],
        exports=[{"id": "csv", "label": "Books (CSV)", "format": "csv", "records": "books"},
                 {"id": "report", "label": "Report", "format": "template", "template": "exports/report.md"}])
    guide = builder_mod.GUIDE_FILE.read_text(encoding="utf-8")
    tool = guide.split("```python\n")[1].split("```")[0]
    template = guide.split("## templates")[1].split("```\n")[1]
    folder = folder_for(tmp_path, env_data, tools={"tools/books.py": tool}, extra={"exports/report.md": template})
    result = check_draft(folder, env_data, JARVIS_TOOLS)
    assert result["ok"], result["problems"]


# ---------- the service: tools, drafts, staging ----------

@pytest.fixture
def benv(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    write_plugin(builtin / "mini")
    env = Env(tmp_path, monkeypatch)
    from jarvis.specialists import manager as manager_mod
    monkeypatch.setattr(manager_mod, "BUILTIN_DIR", builtin)
    env.state.agents.builtin_dir = builtin
    return env


def wait_idle(env, draft_id=None, timeout=5):
    end = time.time() + timeout
    while time.time() < end and any(env.state.runtimes.busy(s) for s in env.state.runtimes.ids()):
        time.sleep(0.02)


def new_draft(env, request="Track my books", name=""):
    draft = env.state.builder.start(request, name)
    wait_idle(env)
    return draft


def fill(env, draft_id, network=False):
    """Make the draft valid through the same tools the model uses."""
    t = env.state.builder.tools(draft_id)
    t["draft_set"]({"description": "Tracks books.", "icon": "book", "permissions": {"network": network}})
    t["draft_upsert"]("records", {"name": "books", "label": "Books", "unique": ["title"], "fields": [
        {"name": "title", "type": "text", "required": True}, {"name": "status", "type": "select",
                                                              "options": ["want", "done"], "default": "want"}]})
    t["draft_upsert"]("actions", {"id": "suggest", "label": "Suggest", "records": "books",
                                  "prompt": "Suggest three books and save them with records_add."})
    t["draft_upsert"]("exports", {"id": "csv", "label": "CSV", "format": "csv", "records": "books"})
    return t["draft_write_file"]("prompt.md", PROMPT)


def test_start_creates_a_least_privilege_draft_and_chat(benv):
    draft = new_draft(benv, "An agent that tracks books I want to read")
    assert draft["mode"] == "new" and draft["target"] is None and len(draft["id"]) == 16
    assert draft["manifest"]["permissions"] == {"network": False, "commands": False, "files": "data"}
    assert draft["manifest"]["id"] and draft["name"] and draft["version"] == "0.1.0" and draft["icon"] == "agents"
    assert [f["path"] for f in draft["files"]] == ["agent.toml", "prompt.md"]
    assert draft["check"]["ok"] is False and any("prompt.md" in p for p in draft["check"]["problems"])
    assert draft["session_id"] and benv.state.builder.get(draft["id"])["busy"] is False
    history = benv.state.runtimes.get(draft["session_id"]).history
    assert history[0]["content"] == "An agent that tracks books I want to read"
    assert benv.state.builder.chat(draft["id"], "more") == draft["session_id"]


def test_builder_session_profile_and_hidden_from_chat_list(benv):
    draft = new_draft(benv)
    jarvis = benv.state.runtimes.get(draft["session_id"])
    profile = jarvis.profile
    assert profile.kind == "builder" and profile.owner == draft["id"] and profile.allow_spawn is False
    assert profile.max_tool_rounds == 80 and set(profile.core_tools) == {"web_search", "fetch_page"}
    assert {"draft_show", "draft_set", "draft_upsert", "draft_remove", "draft_upsert_field", "draft_remove_field",
            "draft_write_file", "draft_edit_file", "draft_read_file", "draft_delete_file", "draft_check",
            "builder_guide", "agent_example", "draft_test_tool"} == set(profile.extra_tools)
    assert "run_command" in profile.hidden_tools and "write_file" in profile.hidden_tools
    assert "Check: 1 problem" in profile.system_prompt  # live state, not a snapshot
    benv.state.builder.tools(draft["id"])["draft_write_file"]("prompt.md", PROMPT)
    assert "prompt.md (" in profile.system_prompt and "Check: READY" in profile.system_prompt
    listed = benv.client.get("/api/sessions").json()["sessions"]
    assert draft["session_id"] not in {s["id"] for s in listed}
    assert draft["session_id"] in {s["id"] for s in benv.client.get("/api/sessions?kind=builder").json()["sessions"]}
    # tool schemas are derived from the signatures
    tools = jarvis.exposure.tools()
    names = {t["function"]["name"] for t in tools}
    assert "draft_upsert" in names and "builder_guide" in names


def test_profile_factory_rebuilds_for_a_stored_session(benv):
    draft = new_draft(benv)
    benv.state.runtimes.close(draft["session_id"])
    meta = benv.state.runtimes._sessions().get(draft["session_id"])
    assert meta["kind"] == "builder" and meta["owner"] == draft["id"]
    profile = profile_for(meta)
    assert profile.kind == "builder" and "draft_upsert" in profile.extra_tools
    assert benv.state.runtimes.get(draft["session_id"]).profile.owner == draft["id"]
    gone = profile_for({"kind": "builder", "owner": "0" * 16})
    assert "discarded or installed" in gone.system_prompt


def test_draft_set_upsert_remove_and_field_ops(benv):
    draft = new_draft(benv, "Track books", "Book Club")
    t = benv.state.builder.tools(draft["id"])
    original_id = draft["manifest"]["id"]
    assert original_id == "book-club"
    reply = t["draft_set"]({"id": "hacked", "schema": 2, "description": "d", "agent": {"greeting": "hi"}})
    assert "ignored id" in reply["summary"]
    manifest_now = benv.state.builder.manifest(draft["id"])
    assert manifest_now["id"] == original_id and manifest_now["schema"] == 1 and manifest_now["agent"]["greeting"] == "hi"
    assert manifest_now["agent"]["prompt"] == "prompt.md"
    assert t["draft_set"]({"colour": "red"})["error"].startswith("can't set 'colour'")
    t["draft_set"]({"agent": {"greeting": None}})
    assert "greeting" not in benv.state.builder.manifest(draft["id"])["agent"]

    t["draft_upsert"]("records", {"name": "books", "fields": [{"name": "title", "type": "text"}]})
    t["draft_upsert"]("tables", {"name": "movies", "fields": [{"name": "title", "type": "text"}]})
    t["draft_upsert"]("records", {"name": "zero", "fields": [{"name": "a", "type": "text"}]}, position=0)
    assert [r["name"] for r in benv.state.builder.manifest(draft["id"])["records"]] == ["zero", "books", "movies"]
    reply = t["draft_upsert"]("actions", {"label": "Find books", "prompt": "Find some books to read."})
    assert reply["summary"] == "Added action 'find-books'"
    assert "Replaced" in t["draft_upsert"]("actions", {"id": "find-books", "prompt": "Find some books now."})["summary"]
    assert t["draft_upsert"]("nonsense", {})["error"].startswith("unknown section")
    assert "needs a 'key'" in t["draft_upsert"]("settings", {"label": "x"})["error"]

    t["draft_upsert_field"]("books", {"name": "rating", "type": "number"})
    t["draft_upsert_field"]("books", {"name": "title", "type": "text", "required": True})
    fields = benv.state.builder.manifest(draft["id"])["records"][1]["fields"]
    assert [f["name"] for f in fields] == ["title", "rating"] and fields[0]["required"] is True
    assert "no records table" in t["draft_upsert_field"]("nope", {"name": "a"})["error"]
    t["draft_upsert"]("records", {"name": "keyed", "unique": ["k"], "title_field": "k", "fields": [
        {"name": "k", "type": "text"}, {"name": "v", "type": "text"}]})
    assert "no field" in t["draft_remove_field"]("keyed", "zzz")["error"]
    t["draft_remove_field"]("keyed", "k")
    keyed = benv.state.builder.manifest(draft["id"])["records"][-1]
    assert [f["name"] for f in keyed["fields"]] == ["v"] and "unique" not in keyed and "title_field" not in keyed
    assert t["draft_remove"]("records", "movies")["summary"].startswith("Removed table")
    assert "there is no table 'movies'" in t["draft_remove"]("records", "movies")["error"]
    # every change is rendered into agent.toml
    toml = tomllib.loads((benv.state.builder.plugin_dir(draft["id"]) / "agent.toml").read_text())
    assert toml == benv.state.builder.manifest(draft["id"])


def test_id_follows_the_name_only_while_it_was_guessed(benv):
    guessed = new_draft(benv, "An agent that tracks my reading list")
    assert guessed["manifest"]["id"] == "tracks-reading-list" and guessed["name"] == "Tracks Reading List"
    benv.state.builder.tools(guessed["id"])["draft_set"]({"name": "Reading Log"})
    assert benv.state.builder.manifest(guessed["id"])["id"] == "reading-log"
    chosen = new_draft(benv, "x y z", "Fixed Name")
    benv.state.builder.tools(chosen["id"])["draft_set"]({"name": "Other"})
    assert benv.state.builder.manifest(chosen["id"])["id"] == "fixed-name"


def test_file_rules_and_edit_semantics(benv):
    draft = new_draft(benv)
    t = benv.state.builder.tools(draft["id"])
    for bad in ("../x.md", "tools/../../x.py", "/etc/passwd", "tools/a b.py", "notes.txt", "skills/x/y.md", "tools\\a.py"):
        assert "not an allowed file path" in t["draft_write_file"](bad, "x")["error"], bad
    for name in ("agent.toml", "manifest.json"):
        assert "generated by Jarvis" in t["draft_write_file"](name, "x")["error"]
    assert "limit is 200 KB" in t["draft_write_file"]("README.md", "x" * (200 * 1024 + 1))["error"]
    assert "scripts" in t["draft_write_file"]("icon.svg", "<svg onload='x'></svg>")["error"]
    assert t["draft_write_file"]("skills/tips.md", "# Tips\nbe kind\n")["summary"].startswith("Wrote")
    assert t["draft_write_file"]("skills/tips.md", "# Tips\nbe kind\nbe kind\n")["summary"].startswith("Updated")
    assert t["draft_read_file"]("skills/tips.md", line_numbers=True)["content"].startswith("1\t# Tips")
    assert t["draft_read_file"]("agent.toml")["content"].startswith("schema = 1")
    assert "no file" in t["draft_read_file"]("../../meta.json")["error"]

    t["draft_write_file"]("README.md", "one two\ntwo two\n")
    assert "appears 3 times" in t["draft_edit_file"]("README.md", "two", "2")["error"]
    assert "not found" in t["draft_edit_file"]("README.md", "three", "3")["error"]
    assert "identical" in t["draft_edit_file"]("README.md", "one", "one")["error"]
    assert t["draft_edit_file"]("README.md", "one two", "1 2")["summary"].startswith("Edited")
    assert t["draft_edit_file"]("README.md", "two", "2", replace_all=True)["summary"].endswith("(2 replacements)")
    assert t["draft_read_file"]("README.md")["content"] == "1 2\n2 2\n"
    assert "required" in t["draft_delete_file"]("prompt.md")["error"]
    assert t["draft_delete_file"]("README.md")["summary"] == "Deleted README.md"
    assert "no file" in t["draft_delete_file"]("README.md")["error"]

    for n in range(40):
        t["draft_write_file"](f"skills/s{n}.md", "x")
    assert "maximum" in t["draft_write_file"]("skills/one-too-many.md", "x")["error"]


def test_events_and_results_carry_problems(benv):
    events = []
    benv.state.agents.events.subscribe(lambda e: events.append(e))
    draft = new_draft(benv)
    t = benv.state.builder.tools(draft["id"])
    reply = t["draft_set"]({"description": "d"})
    assert reply["ok"] is False and reply["summary"] == "Updated description"
    assert any("prompt.md" in p for p in reply["problems"])
    changed = [e for e in events if e.kind == "agent_draft_changed"]
    assert changed and changed[-1].data == {"draft_id": draft["id"]} and changed[-1].session_id == draft["session_id"]
    bad = t["draft_write_file"]("tools/t.py", "def register(ctx:\n")
    assert bad["ok"] is False and any("syntax error" in p for p in bad["problems"])
    for n in range(20):
        t["draft_upsert"]("settings", {"key": f"K{n}", "type": "text", "options": ["x"]})
    many = t["draft_check"]()
    assert len(many["problems"]) > 15
    shown = t["draft_upsert"]("settings", {"key": "last", "type": "text"})
    assert len(shown["problems"]) == 15 and shown["more_problems"] > 0


def test_guide_and_example_tools(benv):
    draft = new_draft(benv)
    t = benv.state.builder.tools(draft["id"])
    assert t["builder_guide"]("records")["text"].startswith("## records")
    assert "overview" in t["builder_guide"]("")["topics"]
    listing = t["agent_example"]("mini")
    assert "agent.toml" in {f["path"] for f in listing["files"]}
    assert 'id = "mini"' in t["agent_example"]("mini", "agent.toml")["content"]
    assert "has no file" in t["agent_example"]("mini", "../x")["error"]
    assert "no agent 'zzz'" in t["agent_example"]("zzz")["error"]


def stage_and_install(env, draft_id, mode=None, trust=False):
    card = env.state.builder.stage(draft_id)
    return card, env.state.agents.install(card["staging"], mode or ("upgrade" if card["mode"] == "upgrade" else "new"), trust)


def test_stage_refuses_problems_then_installs_and_deletes_the_draft(benv):
    draft = new_draft(benv, "Track books", "Book Tracker")
    with pytest.raises(AgentError) as info:
        benv.state.builder.stage(draft["id"])
    assert info.value.status == 409 and any("prompt.md" in i for i in info.value.extra["issues"])
    fill(benv, draft["id"])
    card = benv.state.builder.stage(draft["id"])
    assert card["mode"] == "new" and card["conflict"] is False and card["id"] == "book-tracker"
    assert card["source"] == "Built with AI" and card["permissions"]["network"] is False
    assert card["changes"] == {"manifest": [], "files": []}  # nothing to compare a new agent with
    meta = json.loads((benv.state.agents.staging_dir / card["staging"] / "meta.json").read_text())
    assert meta["draft_id"] == draft["id"]
    session_id = draft["session_id"]
    detail = benv.state.agents.install(card["staging"], "new", False)
    assert detail["id"] == "book-tracker" and detail["actions"] == 1
    assert benv.state.builder.list() == []
    assert not (benv.state.builder.root / draft["id"]).exists()
    assert benv.state.runtimes.get(session_id) is None  # builder chat closed and removed
    with pytest.raises(AgentError) as gone:
        benv.state.builder.get(draft["id"])
    assert gone.value.status == 404


def test_imports_without_a_draft_do_not_touch_drafts(benv):
    draft = new_draft(benv)
    benv.state.agents.create("Plain", "d", "You are plain and simple, and you follow instructions precisely always.")
    assert [d["id"] for d in benv.state.builder.list()] == [draft["id"]]


def install_with_data(env):
    """An installed agent with a tool, a record and settings, for the edit tests."""
    toml = MINI.replace('id = "mini"', 'id = "inst"').replace(" Keep {braces}.", "")
    folder = write_plugin(env.state.agents.agents_dir / "inst", toml, extra={"prompt.md": PROMPT}, tool='''
def register(ctx):
    @ctx.tool
    def shout(text: str) -> dict:
        """Upper-case some text."""
        return {"text": text.upper()}
''')
    manager = env.state.agents
    manager.save_settings("inst", {"topic": "cats"})
    manager.trust("inst")
    manager.table("inst", "items").add([{"title": "Keep me"}])
    return folder


def test_edit_draft_upgrades_keeping_records_and_settings(benv):
    install_with_data(benv)
    manager, builder = benv.state.agents, benv.state.builder
    plugin_dir = manager.agents_dir / "inst"
    draft = builder.edit("inst")
    assert draft["mode"] == "edit" and draft["target"] == "inst"
    assert builder.edit("inst")["id"] == draft["id"]  # resumed, not duplicated
    t = builder.tools(draft["id"])
    t["draft_upsert_field"]("items", {"name": "note", "type": "text"})
    card = builder.stage(draft["id"])
    assert card["mode"] == "upgrade" and card["version"] == "1.0.1"  # patch bumped
    assert card["changes"]["manifest"] == ["Changed version 1.0.0 → 1.0.1", "Added field items.note"]
    assert card["changes"]["files"] == []
    assert card["conflict"] is False
    detail = manager.install(card["staging"], "upgrade", False)
    assert detail["version"] == "1.0.1" and detail["records"][0]["count"] == 1
    assert manager.settings_values("inst")["topic"] == "cats"
    assert manager.get("inst").trusted and plugin_dir.is_dir()  # same code: trust kept
    assert builder.list() == []


def test_edit_changing_python_drops_trust(benv):
    install_with_data(benv)
    manager, builder = benv.state.agents, benv.state.builder
    draft = builder.edit("inst")
    t = builder.tools(draft["id"])
    result = t["draft_edit_file"]("tools/shout.py", "text.upper()", "text.upper() + '!'")
    assert result["ok"], result
    card = builder.stage(draft["id"])
    assert [c["path"] for c in card["changes"]["files"]] == ["tools/shout.py"]
    assert "+        return {\"text\": text.upper() + '!'}" in card["changes"]["files"][0]["diff"]
    detail = manager.install(card["staging"], "upgrade", False)
    assert detail["needs_trust"] is True and detail["tools"] == []


def test_edit_conflict_flag_when_the_installed_agent_changed(benv):
    install_with_data(benv)
    manager, builder = benv.state.agents, benv.state.builder
    draft = builder.edit("inst")
    (manager.agents_dir / "inst" / "README.md").write_text("changed behind the builder's back")
    builder.tools(draft["id"])["draft_set"]({"description": "new"})
    assert builder.stage(draft["id"])["conflict"] is True


def test_editing_a_builtin_makes_a_custom_copy(benv):
    builder = benv.state.builder
    draft = builder.edit("mini")
    assert draft["mode"] == "new" and draft["target"] is None and draft["base"] == "mini"
    assert draft["manifest"]["id"] == "mini-2" and draft["name"] == "Mini (custom)" and draft["target_name"] == "Mini"
    assert builder.edit("mini")["id"] == draft["id"]
    assert not draft["check"]["ok"]  # the test fixture's prompt is short and has a stray {braces}
    tools = builder.tools(draft["id"])
    tools["draft_write_file"]("prompt.md", PROMPT)
    action = dict(draft["manifest"]["actions"][0], prompt="Do {what} x{count} about {settings.topic}; have {records.items.count}.")
    assert tools["draft_upsert"]("actions", action)["ok"]
    card, detail = stage_and_install(benv, draft["id"])
    assert card["mode"] == "new" and detail["id"] == "mini-2" and benv.state.agents.get("mini").builtin


def test_start_from_a_base_agent(benv):
    draft = benv.state.builder.start("Make it about dogs", base="mini")
    wait_idle(benv)
    assert draft["base"] == "mini" and draft["manifest"]["id"] == "mini-2"
    assert [a["id"] for a in draft["manifest"]["actions"]] == ["go"]
    assert "exports/sheet.md" in {f["path"] for f in draft["files"]}


def test_set_llm_and_discard(benv):
    draft = new_draft(benv)
    updated = benv.state.builder.set_llm(draft["id"], "gemini", "g-test")
    assert updated["llm"] == {"provider": "gemini", "model": "g-test"}
    jarvis = benv.state.runtimes.get(updated["session_id"])
    assert jarvis.config["llm"]["provider"] == "gemini" and jarvis.config["llm"]["model"] == "g-test"
    assert benv.state.builder.set_llm(draft["id"], "", "")["llm"] == {}
    sid = benv.state.builder.get(draft["id"])["session_id"]
    assert benv.state.builder.discard(draft["id"]) == {"discarded": draft["id"]}
    assert benv.state.runtimes.get(sid) is None and not (benv.state.builder.root / draft["id"]).exists()
    assert benv.state.agents.plugins() is not None  # .drafts is skipped by discovery


def test_drafts_are_skipped_by_plugin_discovery(benv):
    draft = new_draft(benv)
    fill(benv, draft["id"])
    assert (benv.state.builder.plugin_dir(draft["id"]) / "agent.toml").is_file()
    assert draft["manifest"]["id"] not in benv.state.agents.plugins()


# ---------- draft_test_tool: approval gating ----------

class FakePolicy:
    def __init__(self, answer=True):
        self.answer, self.requests = answer, []

    def request_approval(self, action, payload, session_id="", turn_id=""):
        self.requests.append((action, payload))
        return self.answer


TOOL_FOR_TEST = '''
def register(ctx):
    @ctx.tool
    def shout(text: str) -> dict:
        """Upper-case text and count it in the books table."""
        ctx.records("books").add([{"title": text.upper()}])
        return {"text": text.upper(), "topic": ctx.settings.get("TOPIC"), "key": ctx.settings.get("API_KEY")}
'''


def test_draft_test_tool_is_gated_per_code_hash(benv):
    draft = new_draft(benv, "Track books", "Tester")
    fill(benv, draft["id"])
    t = benv.state.builder.tools(draft["id"])
    t["draft_upsert"]("settings", {"key": "TOPIC", "type": "text", "default": "cats"})
    t["draft_upsert"]("settings", {"key": "API_KEY", "type": "secret"})
    assert t["draft_write_file"]("tools/shout.py", TOOL_FOR_TEST)["ok"], t["draft_check"]()
    policy = FakePolicy(answer=False)
    benv.state.builder._policy = lambda: policy
    assert t["draft_test_tool"]("shout", {"text": "hi"}) == {"error": "the user declined the test run"}
    assert policy.requests[0][0] == "run_draft_code" and policy.requests[0][1]["file"] == "tools/shout.py"
    assert "def register" in policy.requests[0][1]["code"]
    policy.answer = True
    reply = t["draft_test_tool"]("shout", {"text": "hi"}, {"API_KEY": "secret-value-123"})
    assert reply["ok"] is True, reply
    assert reply["result"] == {"text": "HI", "topic": "cats", "key": "••••"}  # defaults used, secret redacted
    assert len(policy.requests) == 2
    t["draft_test_tool"]("shout", {"text": "again"})  # same code hash: no new approval
    assert len(policy.requests) == 2
    t["draft_edit_file"]("tools/shout.py", "text.upper(), \"topic\"", "text.upper(), \"topic2\"")
    t["draft_edit_file"]("tools/shout.py", '"topic2"', '"topic"')
    t["draft_edit_file"]("tools/shout.py", "Upper-case text", "Upper-case some text")
    again = t["draft_test_tool"]("shout", {"text": "x"})
    assert again["ok"] and len(policy.requests) == 3  # code changed: asked again
    assert "the draft has no tool 'nope'" in t["draft_test_tool"]("nope", {})["error"]
    t["draft_write_file"]("tools/bad.py", "def register(ctx:\n")
    assert "fix the Python files first" in t["draft_test_tool"]("shout", {})["error"]


def test_draft_test_tool_without_a_policy_is_declined(benv):
    draft = new_draft(benv, "Track books", "Tester2")
    fill(benv, draft["id"])
    t = benv.state.builder.tools(draft["id"])
    t["draft_write_file"]("tools/shout.py", TOOL_FOR_TEST)
    benv.state.builder._policy = lambda: None
    assert t["draft_test_tool"]("shout", {"text": "hi"})["error"] == "the user declined the test run"


def test_draft_test_tool_uses_the_targets_saved_settings_and_records_copy(benv):
    install_with_data(benv)
    manager, builder = benv.state.agents, benv.state.builder
    draft = builder.edit("inst")
    t = builder.tools(draft["id"])
    t["draft_write_file"]("tools/count.py", '''
def register(ctx):
    @ctx.tool
    def count_items() -> dict:
        """Count the items and add one (the test copy only)."""
        ctx.records("items").add([{"title": "temp"}])
        return {"count": ctx.records("items").count(), "topic": ctx.settings.get("topic")}
''')
    builder._policy = lambda: FakePolicy(True)
    reply = t["draft_test_tool"]("count_items", {})
    assert reply["result"] == {"count": 2, "topic": "cats"}
    assert manager.table("inst", "items").count() == 1  # the real table is untouched


# ---------- tool_runner ----------

def runner_plugin(tmp_path, code, network=False):
    data = manifest(permissions={"network": network, "commands": False, "files": "data"},
                    records=[{"name": "items", "fields": [{"name": "title", "type": "text"}]}],
                    settings=[{"key": "TOPIC", "type": "text"}])
    return folder_for(tmp_path, data, tools={"tools/t.py": code})


def test_tool_runner_success_logs_stdout_and_settings(tmp_path):
    code = ("def register(ctx):\n    @ctx.tool\n    def hello(name: str, n: int = 1) -> dict:\n"
            "        '''Say hello.'''\n        print('printing')\n        ctx.log('logged ' + name)\n"
            "        return {'hi': name * n, 'topic': ctx.settings.get('TOPIC', 'dflt'), 'cwd': __import__('os').getcwd()}\n")
    reply = run_tool_subprocess(runner_plugin(tmp_path, code), "hello", {"name": "ab", "n": 2}, {"TOPIC": "t"})
    assert reply["ok"] is True and reply["result"]["hi"] == "abab" and reply["result"]["topic"] == "t"
    assert reply["logs"] == ["logged ab"] and reply["stdout"].strip() == "printing"
    assert "jarvis-tool-test" in reply["result"]["cwd"]


def test_tool_runner_exception_has_plugin_frames_only(tmp_path):
    code = ("def helper(x):\n    return 1 / x\n\n"
            "def register(ctx):\n    @ctx.tool\n    def boom(x: int) -> dict:\n        '''Fail.'''\n"
            "        return {'v': helper(x)}\n")
    reply = run_tool_subprocess(runner_plugin(tmp_path, code), "boom", {"x": 0})
    assert reply["ok"] is False and reply["error"] == "ZeroDivisionError: division by zero"
    assert reply["traceback"][-1].startswith("tools/t.py:2 in helper") and "1 / x" in reply["traceback"][-1]
    assert all(line.startswith("tools/t.py") for line in reply["traceback"])


def test_tool_runner_unknown_tool_and_load_error(tmp_path):
    code = "import json\nraise RuntimeError('broken at import')\n"
    reply = run_tool_subprocess(runner_plugin(tmp_path, code), "x", {})
    assert reply["ok"] is False and "no tool called 'x'" in reply["error"] and "broken at import" in reply["error"]


def test_tool_runner_timeout(tmp_path):
    code = ("import time\ndef register(ctx):\n    @ctx.tool\n    def slow() -> dict:\n        '''Sleep.'''\n"
            "        time.sleep(30)\n        return {}\n")
    started = time.time()
    reply = run_tool_subprocess(runner_plugin(tmp_path, code), "slow", {}, timeout=1.5)
    assert reply["ok"] is False and "did not finish within 1.5 seconds" in reply["error"]
    assert time.time() - started < 15


def test_tool_runner_network_false_stub_and_true(tmp_path):
    code = ("def register(ctx):\n    @ctx.tool\n    def net() -> dict:\n        '''Use http.'''\n"
            "        return {'client': type(ctx.http).__name__}\n"
            "    @ctx.tool\n    def web() -> dict:\n        '''Use ctx.call.'''\n"
            "        return ctx.call('web_search', query='x')\n")
    plugin = runner_plugin(tmp_path, code, network=False)
    reply = run_tool_subprocess(plugin, "net", {}, network=False)
    assert reply["result"] == {"client": "_NoNetwork"}
    blocked = run_tool_subprocess(plugin, "web", {}, network=False)
    assert blocked["ok"] is False and "network = false" in blocked["error"]
    open_net = run_tool_subprocess(plugin, "net", {}, network=True)
    assert open_net["result"] == {"client": "Client"}
    code2 = ("def register(ctx):\n    @ctx.tool\n    def get() -> dict:\n        '''Use http.'''\n"
             "        return {'x': ctx.http.get('http://example.invalid')}\n")
    (tmp_path / "two").mkdir()
    err = run_tool_subprocess(runner_plugin(tmp_path / "two", code2), "get", {}, network=False)
    assert err["ok"] is False and err["error"].startswith("PermissionError")


def test_tool_runner_records_use_a_temp_dir(tmp_path):
    code = ("def register(ctx):\n    @ctx.tool\n    def add(title: str) -> dict:\n        '''Add.'''\n"
            "        ctx.records('items').add([{'title': title}])\n"
            "        return {'count': ctx.records('items').count(), 'dir': str(ctx.data_dir)}\n")
    plugin = runner_plugin(tmp_path, code)
    real = tmp_path / "real"
    (real / "records").mkdir(parents=True)
    (real / "records" / "items.json").write_text(json.dumps({"schema": 1, "next_id": 2, "records": [
        {"id": "1", "title": "old", "created": "x", "updated": "x"}]}))
    empty = run_tool_subprocess(plugin, "add", {"title": "a"})
    assert empty["result"]["count"] == 1
    copied = run_tool_subprocess(plugin, "add", {"title": "b"}, records_from=real)
    assert copied["result"]["count"] == 2
    assert json.loads((real / "records" / "items.json").read_text())["next_id"] == 2  # untouched
    assert not Path(copied["result"]["dir"]).exists()  # temp dir removed


def test_tool_runner_output_is_capped_and_secrets_hidden(tmp_path):
    code = ("def register(ctx):\n    @ctx.tool\n    def big() -> dict:\n        '''Big.'''\n"
            "        print('x' * 50000)\n        return {'text': 'y' * 50000, 'leak': ctx.settings.get('TOPIC')}\n")
    reply = run_tool_subprocess(runner_plugin(tmp_path, code), "big", {}, {"TOPIC": "hunter2xyz"}, secrets=["hunter2xyz"])
    assert reply["ok"] and reply["result"]["truncated"] is True and len(reply["stdout"]) < 20100
    small = ("def register(ctx):\n    @ctx.tool\n    def leak() -> dict:\n        '''Leak.'''\n"
             "        return {'leak': ctx.settings.get('TOPIC')}\n")
    (tmp_path / "x").mkdir()
    reply = run_tool_subprocess(runner_plugin(tmp_path / "x", small), "leak", {}, {"TOPIC": "hunter2xyz"},
                                secrets=["hunter2xyz"])
    assert reply["result"] == {"leak": "••••"}


# ---------- routes ----------

def test_builder_routes(benv):
    client = benv.client
    assert client.get("/api/agent-drafts").json() == {"drafts": []}
    assert client.post("/api/agent-drafts", json={"request": "  "}).status_code == 400
    assert client.post("/api/agent-drafts", json={"request": "x", "base": "nope"}).status_code == 404
    draft = client.post("/api/agent-drafts", json={"request": "Track my books", "name": "Books"}).json()
    wait_idle(benv)
    assert draft["mode"] == "new" and draft["name"] == "Books" and draft["check"]["ok"] is False
    assert set(draft) >= {"id", "mode", "target", "base", "request", "session_id", "busy", "llm", "created", "updated",
                          "name", "icon", "version", "description", "manifest", "files", "check", "target_name"}
    listing = client.get("/api/agent-drafts").json()["drafts"]
    assert listing[0] == {"id": draft["id"], "mode": "new", "target": None, "target_name": "", "name": "Books",
                          "icon": "agents", "updated": listing[0]["updated"], "ok": False, "problem_count": 1, "busy": False}
    assert client.get(f"/api/agent-drafts/{draft['id']}").json()["id"] == draft["id"]
    assert client.get("/api/agent-drafts/zzzz").status_code == 404
    file = client.get(f"/api/agent-drafts/{draft['id']}/file", params={"path": "prompt.md"}).json()
    assert file["path"] == "prompt.md" and "has not written" in file["content"]
    assert client.get(f"/api/agent-drafts/{draft['id']}/file", params={"path": "../meta.json"}).status_code == 404
    assert client.post(f"/api/agent-drafts/{draft['id']}/chat", json={"text": ""}).status_code == 400
    assert client.post(f"/api/agent-drafts/{draft['id']}/chat", json={"text": "and a csv"}).status_code == 202
    wait_idle(benv)
    llm = client.post(f"/api/agent-drafts/{draft['id']}/llm", json={"provider": "gemini", "model": "g"}).json()
    assert llm["llm"] == {"provider": "gemini", "model": "g"}
    blocked = client.post(f"/api/agent-drafts/{draft['id']}/stage")
    assert blocked.status_code == 409 and blocked.json()["issues"] and "prompt.md" in blocked.json()["error"]
    fill(benv, draft["id"])
    card = client.post(f"/api/agent-drafts/{draft['id']}/stage").json()
    assert card["mode"] == "new" and card["changes"]["manifest"] == []
    installed = client.post(f"/api/agents/{card['id']}/install", json={"staging": card["staging"], "mode": "new"})
    assert installed.status_code == 200 and client.get("/api/agent-drafts").json() == {"drafts": []}


def test_busy_builder_returns_409_and_edit_route(benv):
    import threading
    gate = threading.Event()
    from jarvis.llm import LLM

    def slow(llm_self, messages, tools=None, on_text=None, **kwargs):
        gate.wait(5)
        return {"role": "assistant", "content": "ok"}
    benv_chat = LLM.chat
    LLM.chat = slow
    try:
        draft = benv.client.post("/api/agent-drafts", json={"request": "Track books"}).json()
        assert draft["busy"] is True
        assert benv.client.post(f"/api/agent-drafts/{draft['id']}/chat", json={"text": "x"}).status_code == 409
        assert benv.client.post(f"/api/agent-drafts/{draft['id']}/stage").status_code == 409
        assert benv.client.post(f"/api/agent-drafts/{draft['id']}/llm", json={"provider": "x"}).status_code == 409
        gate.set()
        wait_idle(benv)
    finally:
        LLM.chat = benv_chat
    edit = benv.client.post("/api/agents/mini/edit-draft", json={"request": "Add a field"})
    assert edit.status_code == 200 and edit.json()["base"] == "mini"
    assert benv.client.post("/api/agents/mini/edit-draft").json()["id"] == edit.json()["id"]
    assert benv.client.post("/api/agents/nope/edit-draft").status_code == 404
    assert benv.client.delete(f"/api/agent-drafts/{draft['id']}").json() == {"discarded": draft["id"]}
    assert benv.client.delete(f"/api/agent-drafts/{draft['id']}").status_code == 404


def test_icon_route_and_pending_approvals_in_history(benv):
    draft = new_draft(benv)
    url = f"/api/agent-drafts/{draft['id']}/icon.svg"
    assert benv.client.get(url).status_code == 404
    benv.state.builder.tools(draft["id"])["draft_write_file"]("icon.svg", "<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    reply = benv.client.get(url)
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("image/svg+xml")
    from jarvis.events import Event
    sid = draft["session_id"]
    benv.state.agents.events.emit(Event("approval_requested", sid, data={"request_id": "r1", "action": "run_draft_code", "payload": {}}))
    time.sleep(0.05)
    history = benv.client.get(f"/api/runtime/{sid}/history").json()
    assert [a["request_id"] for a in history["approvals"]] == ["r1"]
    other = benv.client.get(f"/api/runtime/{benv.state.builder.get(new_draft(benv)['id'])['session_id']}/history").json()
    assert other["approvals"] == []


def test_upsert_forgives_small_model_shapes(tmp_path):
    from jarvis.specialists.builder import AgentBuilder
    b = AgentBuilder.__new__(AgentBuilder)
    m = {}
    b._upsert_any(m, "", {"records": [{"name": "books", "fields": [{"name": "title"}]}]}, None)
    b._upsert_any(m, "", {"id": "suggest", "label": "Suggest", "prompt": "Suggest three books."}, None)
    b._upsert_any(m, "exports", [{"id": "csv", "format": "csv", "records": "books"}], None)
    b._upsert_any(m, "", {"key": "API_KEY", "type": "secret"}, None)
    assert [t["name"] for t in m["records"]] == ["books"]
    assert m["actions"][0]["id"] == "suggest" and m["exports"][0]["id"] == "csv"
    assert m["settings"][0]["key"] == "API_KEY"
