"""Agents plugins: manifest validation, records, templating, import/export, tools, routes."""

import io
import json
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from jarvis import keystore
from jarvis.specialists import ImportProblem, ManifestError, load_manifest
from jarvis.specialists import manifest as manifest_mod
from jarvis.specialists import packaging
from jarvis.specialists.records import RecordTable
from jarvis.specialists.templating import (TemplateError, parse_template, render_prompt, render_template,
                                           to_csv, to_markdown)
from tests.test_webui import Env

BUILTIN = Path(__file__).resolve().parent.parent / "jarvis" / "agents_builtin"

MINI = """schema = 1
id = "mini"
name = "Mini"
version = "1.0.0"
description = "A tiny test agent."

[agent]
prompt = "prompt.md"

[[settings]]
key = "topic"
label = "Topic"
type = "text"
required = true

[[settings]]
key = "API_KEY"
type = "secret"

[[records]]
name = "items"
unique = ["url"]
fields = [
  { name = "title", type = "text", required = true },
  { name = "url", type = "url" },
  { name = "score", type = "number", min = 1, max = 5 },
  { name = "state", type = "select", options = ["new", "done"], default = "new", editable = true },
]

[[actions]]
id = "go"
label = "Go"
inputs = [
  { key = "what", type = "text", required = true },
  { key = "count", type = "number", default = 2, min = 1, max = 9 },
]
prompt = "Do {what} x{count} about {settings.topic}; have {records.items.count}. Keep {braces}."

[[exports]]
id = "csv"
format = "csv"
records = "items"

[[exports]]
id = "sheet"
format = "template"
template = "exports/sheet.md"
"""

SHEET = "# {{agent.name}}\n{{#each items state=new}}- {{@number}} {{title}}{{#if url}} <{{url}}>{{/if}}\n{{/each}}Total {{count items}}\n"

TOOL = '''
def register(ctx):
    @ctx.tool
    def shout(text: str) -> dict:
        """Upper-case some text and save it as an item."""
        ctx.records("items").add([{"title": text.upper()}])
        return {"text": text.upper(), "topic": ctx.settings.get("topic"), "key": bool(ctx.settings.get("API_KEY"))}
'''


def write_plugin(folder, toml=MINI, tool=None, extra=None):
    folder = Path(folder)
    (folder / "exports").mkdir(parents=True, exist_ok=True)
    (folder / "agent.toml").write_text(toml, encoding="utf-8")
    (folder / "prompt.md").write_text("You are Mini. Be brief.\n", encoding="utf-8")
    (folder / "exports" / "sheet.md").write_text(SHEET, encoding="utf-8")
    if tool:
        (folder / "tools").mkdir(exist_ok=True)
        (folder / "tools" / "shout.py").write_text(tool, encoding="utf-8")
    for name, text in (extra or {}).items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def zip_of(folder, prefix="mini/"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in Path(folder).rglob("*"):
            if path.is_file():
                archive.write(path, prefix + path.relative_to(folder).as_posix())
    return buffer.getvalue()


# ---------- manifest ----------

def test_builtin_lead_finder_is_valid():
    manifest, warnings = load_manifest(BUILTIN / "lead-finder", check_version=False)
    assert warnings == []
    assert manifest["id"] == "lead-finder"
    assert {a["id"] for a in manifest["actions"]} == {"find", "contacts", "drafts", "followups", "review"}
    assert {e["id"] for e in manifest["exports"]} == {"csv", "outreach", "json"}
    keys = {s["key"] for s in manifest["settings"]}
    assert {"offer", "ideal_lead", "sender_name", "calendar_link", "tone", "min_score",
            "SERPER_API_KEY", "BRAVE_API_KEY"} <= keys
    leads = manifest["records"][0]
    assert leads["unique"] == ["profile_url"] and leads["status_field"] == "status"


def test_manifest_errors_are_precise(tmp_path):
    bad = MINI.replace('type = "text"\nrequired = true', 'type = "colour"\nrequired = true', 1)
    bad = bad.replace('id = "mini"', 'id = "Mini Agent"').replace("schema = 1", "schema = 1\nflavour = 'x'")
    bad = bad.replace('{ name = "score", type = "number", min = 1, max = 5 }', '{ name = "id", type = "number" }')
    write_plugin(tmp_path, bad)
    with pytest.raises(ManifestError) as info:
        load_manifest(tmp_path)
    text = str(info.value)
    assert "agent.toml → id: 'Mini Agent' must be" in text
    assert "settings[0].type: 'colour' is not a valid type" in text
    assert "records[0].fields[2].name: 'id' is reserved" in text
    assert "flavour" not in text  # unknown keys are warnings only


def test_unknown_keys_warn_and_template_errors(tmp_path):
    write_plugin(tmp_path, MINI.replace("[agent]", "flavour = 'x'\n\n[agent]\nmood = 1"))
    _, warnings = load_manifest(tmp_path)
    assert {(w["key"], w["message"]) for w in warnings} >= {("flavour", "unknown key (ignored)"),
                                                           ("agent.mood", "unknown key (ignored)")}
    (tmp_path / "exports" / "sheet.md").write_text("{{#each items}} oops", encoding="utf-8")
    with pytest.raises(ManifestError, match="exports/sheet.md.*never closed"):
        load_manifest(tmp_path)


def test_missing_files_and_bad_toml(tmp_path):
    with pytest.raises(ManifestError, match="agent.toml: is missing"):
        load_manifest(tmp_path)
    (tmp_path / "agent.toml").write_text("schema = 1\nid = [", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid TOML"):
        load_manifest(tmp_path)
    write_plugin(tmp_path)
    (tmp_path / "prompt.md").unlink()
    with pytest.raises(ManifestError, match="prompt.md: is missing"):
        load_manifest(tmp_path)


def test_min_jarvis_version(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest_mod, "jarvis_version", lambda: "1.1.2")
    write_plugin(tmp_path, MINI.replace('version = "1.0.0"', 'version = "1.0.0"\nmin_jarvis = "9.0"'))
    with pytest.raises(ManifestError, match="needs Jarvis 9.0 or newer; you have 1.1.2"):
        load_manifest(tmp_path)
    write_plugin(tmp_path, MINI.replace('version = "1.0.0"', 'version = "1.0.0"\nmin_jarvis = "1.1.2"'))
    assert load_manifest(tmp_path)[0]["min_jarvis"] == "1.1.2"


# ---------- records ----------

def _table(tmp_path, changes=None):
    manifest, _ = load_manifest(write_plugin(tmp_path / "p"))
    return RecordTable(tmp_path / "data", manifest["records"][0],
                       on_change=(lambda t, c: changes.append((t, c))) if changes is not None else None)


def test_records_coerce_dedupe_and_merge(tmp_path):
    changes = []
    table = _table(tmp_path, changes)
    result = table.add([
        {"title": "One", "url": "www.example.com/a/", "score": "4", "state": "New"},
        {"title": "Dup", "url": "https://example.com/a", "score": 5},
        {"url": "https://x.org"},                        # missing required title
        {"title": "Bad score", "score": 9, "colour": "red"},
    ])
    assert (result["added"], result["updated"], result["skipped"]) == (2, 1, 1)
    problems = json.dumps(result["problems"])
    assert "'title' is required" in problems and "must be at most 5" in problems and "unknown field 'colour'" in problems
    records = table.all()
    assert records[0]["id"] == "1" and records[0]["url"] == "https://www.example.com/a/"
    assert records[0]["title"] == "Dup" and records[0]["score"] == 5 and records[0]["state"] == "new"
    assert records[1]["state"] == "new" and "score" not in records[1]
    # editable field set by the user is not overwritten by a later add
    table.update("1", {"state": "done"})
    table.add([{"title": "Dup", "url": "example.com/a", "state": "new"}])
    assert table.get("1")["state"] == "done"
    assert changes and changes[0] == ("items", "add")
    data = json.loads(table.path.read_text())
    assert data["next_id"] == 3


def test_records_query_update_delete(tmp_path):
    table = _table(tmp_path)
    table.add([{"title": "a", "url": "https://a.com"}, {"title": "b"}, {"title": "c", "state": "done"}])
    assert table.query({"url": ""})["total"] == 2
    assert [r["title"] for r in table.query({"state": ["DONE"]})["records"]] == ["c"]
    assert table.query(search="A.COM")["total"] == 1
    assert table.query(fields=["title"], limit=1)["records"] == [{"id": "1", "title": "a"}]
    with pytest.raises(ValueError, match="not one of"):
        table.update("1", {"state": "weird"})
    with pytest.raises(KeyError):
        table.update("99", {"title": "x"})
    assert table.delete("2") and not table.delete("2")
    assert table.count() == 2


# ---------- templating ----------

def test_prompt_templating_leaves_unknown_braces():
    text = render_prompt("Find {n} {who}; {settings.offer} have {records.leads.count}; json {\"a\": 1} {nope}",
                         {"n": 3, "who": ["a", "b"]}, {"offer": "x"}, {"leads": 7})
    assert text == 'Find 3 a, b; x have 7; json {"a": 1} {nope}'


def test_template_engine():
    source = "{{#each rows state=new,old}}{{@number}}.{{name}}{{#if mail}}<{{mail}}>{{else}}-{{/if}} {{/each}}|{{count rows}}|{{settings.x}}"
    out = render_template(source, {"settings": {"x": "S"}}, {"rows": [
        {"name": "A", "state": "new", "mail": "a@x"}, {"name": "B", "state": "done"}, {"name": "C", "state": "old"}]})
    assert out == "1.A<a@x> 2.C- |3|S"
    for bad, message in (("{{#each}}", "needs a name"), ("{{/if}}", "does not close"),
                         ("{{#if a}}", "never closed"), ("{{#loop x}}{{/loop}}", "unknown block"),
                         ("{{a b c}}", "cannot understand"), ("{{else}}", "outside")):
        with pytest.raises(TemplateError, match=message):
            parse_template(bad)
    assert render_template("{{__class__}}{{a.__dict__}}", {"a": {}}, {}) == ""  # never touches Python attributes


def test_csv_and_markdown_exports():
    table = {"label": "Items", "fields": [{"name": "title", "label": "Title"}, {"name": "note", "label": "Note"}]}
    csv_text = to_csv(table, [{"title": "=HYPERLINK(x)", "note": "a,b\nc", "created": "t"}])
    assert csv_text.startswith("﻿title,note,created,updated")
    assert "'=HYPERLINK(x)" in csv_text and '"a,b\nc"' in csv_text
    md = to_markdown(table, [{"title": "x|y", "note": "1\n2"}])
    assert "| x\\|y | 1 2 |" in md


# ---------- packaging ----------

def test_zip_safety(tmp_path):
    def make(entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    with pytest.raises(ImportProblem, match="outside the folder"):
        packaging.read_zip(make([("a/agent.toml", "x"), ("a/../../evil.py", "x")]))
    with pytest.raises(ImportProblem, match="absolute"):
        packaging.read_zip(make([("/etc/agent.toml", "x")]))
    link = zipfile.ZipInfo("a/link.md")
    link.external_attr = (0o120777 << 16)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a/agent.toml", "x")
        archive.writestr(link, "/etc/passwd")
    with pytest.raises(ImportProblem, match="symbolic link"):
        packaging.read_zip(buffer.getvalue())
    with pytest.raises(ImportProblem, match="not a Jarvis agent"):
        packaging.read_zip(make([("readme.md", "x")]))
    with pytest.raises(ImportProblem, match="not a valid .zip"):
        packaging.read_zip(b"nope")
    with pytest.raises(ImportProblem, match="several agents"):
        packaging.read_zip(make([("a/agent.toml", "x"), ("b/agent.toml", "x")]))
    files, skipped = packaging.read_zip(make([
        ("repo-main/agents/x/agent.toml", "t"), ("repo-main/agents/x/run.exe", "x"),
        ("repo-main/agents/x/.github/ci.yml", "x"), ("repo-main/agents/y/agent.toml", "t")]), "agents/x")
    assert list(files) == ["agent.toml"] and skipped == ["run.exe (file type not used by agents)"]
    with pytest.raises(ImportProblem, match="limit"):
        packaging.read_zip(b"0" * (packaging.MAX_BYTES + 1))


def test_github_urls():
    urls, sub = packaging.github_zip_urls("https://github.com/acme/agents")
    assert urls[0] == "https://codeload.github.com/acme/agents/zip/refs/heads/main" and sub == ""
    urls, sub = packaging.github_zip_urls("https://github.com/acme/agents/tree/dev/plugins/lead")
    assert urls[0] == "https://codeload.github.com/acme/agents/zip/refs/heads/dev" and sub == "plugins/lead"
    assert packaging.github_zip_urls("https://gitlab.com/a/b")[0] is None
    with pytest.raises(ImportProblem, match="GitHub link"):
        packaging.download("http://example.com/x.zip")


def test_svg_check():
    packaging.check_svg(b'<svg viewBox="0 0 24 24"><path d="M1 1"/></svg>')
    with pytest.raises(ImportProblem):
        packaging.check_svg(b'<svg onload="alert(1)"></svg>')


# ---------- the manager through the web routes ----------

@pytest.fixture
def agents_env(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    write_plugin(builtin / "mini")
    env = Env(tmp_path, monkeypatch)
    from jarvis.specialists import manager as manager_mod
    monkeypatch.setattr(manager_mod, "BUILTIN_DIR", builtin)
    return env


def test_list_detail_settings_and_secrets(agents_env):
    client = agents_env.client
    agents = client.get("/api/agents").json()["agents"]
    assert agents[0]["id"] == "mini" and agents[0]["builtin"] and agents[0]["missing_settings"] == ["topic"]
    bad = client.put("/api/agents/mini/settings", json={"values": {"topic": {"x": 1}, "nope": 1}})
    assert bad.status_code == 400 and set(bad.json()["fields"]) == {"topic", "nope"}
    saved = client.put("/api/agents/mini/settings", json={"values": {"topic": "cats"},
                                                          "secrets": {"API_KEY": "sk-secret-1234"}}).json()
    assert saved["values"] == {"topic": "cats"} and saved["missing"] == []
    assert saved["secrets"]["API_KEY"] == {"saved": True, "hint": "••••1234"}
    assert "sk-secret" not in json.dumps(saved)
    assert keystore.get("agent:mini:API_KEY") == "sk-secret-1234"
    client.put("/api/agents/mini/settings", json={"secrets": {"API_KEY": None}})
    assert keystore.get("agent:mini:API_KEY") is None
    detail = client.get("/api/agents/mini").json()
    assert detail["manifest"]["actions"][0]["id"] == "go" and "sk-secret" not in json.dumps(detail)


def test_profile_tools_prompt_and_hidden(agents_env):
    manager = agents_env.state.agents
    manager.save_settings("mini", {"topic": "cats"}, {"API_KEY": "sk-abcdef"})
    profile = manager.profile("mini")
    assert profile.kind == "agent" and profile.owner == "mini" and profile.replace_persona
    assert set(profile.extra_tools) == {"records_add", "records_update", "records_query", "records_delete"}
    schema = profile.extra_tools["records_add"].input_schema
    item = schema["properties"]["items"]["items"]
    assert item["properties"]["state"]["enum"] == ["new", "done"] and item["required"] == ["title"]
    assert {"run_command", "write_file", "edit_file"} <= profile.hidden_tools
    assert "topic (Topic): cats" in profile.system_prompt and "API_KEY: saved" in profile.system_prompt
    assert "sk-abcdef" not in profile.system_prompt
    assert "`items` (Items): title (text, required)" in profile.system_prompt
    added = profile.extra_tools["records_add"](items=[{"title": "x", "url": "a.com"}, {"title": "y", "url": "A.com/"}])
    assert added["added"] == 1 and added["updated"] == 1 and "table now has 1" in added["summary"]
    assert profile.extra_tools["records_update"](id=1, fields={"state": "nah"})["error"].startswith("state:")
    assert profile.extra_tools["records_query"](where={"zzz": 1})["error"].startswith("unknown field")


def test_profile_schema_reaches_the_model(agents_env, monkeypatch):
    """The records tools' generated schema is what the LLM receives."""
    from jarvis.agent import Jarvis
    manager = agents_env.state.agents
    jarvis = Jarvis(agents_env.jarvis.config, None, agents_env.jarvis.events, manager.profile("mini"))
    tools = {t["function"]["name"]: t["function"] for t in jarvis.exposure.tools()}
    assert "records_add" in tools and "run_command" not in tools
    assert tools["records_add"]["parameters"]["properties"]["items"]["items"]["properties"]["state"]["enum"] == ["new", "done"]


def test_actions_render_validate_and_run(agents_env):
    client = agents_env.client
    response = client.post("/api/agents/mini/actions/go", json={"inputs": {"what": "x"}})
    assert response.status_code == 400 and "settings first" in response.json()["error"]
    client.put("/api/agents/mini/settings", json={"values": {"topic": "cats"}})
    response = client.post("/api/agents/mini/actions/go", json={"inputs": {"count": "lots"}})
    assert response.status_code == 400 and set(response.json()["fields"]) == {"what", "count"}
    response = client.post("/api/agents/mini/actions/go", json={"inputs": {"count": 42, "what": "x"}})
    assert response.json()["fields"] == {"count": "must be at most 9 (got 42)"}
    preview = client.post("/api/agents/mini/actions/go/preview", json={"inputs": {"what": "runs"}}).json()
    assert preview["prompt"] == "Do runs x2 about cats; have 0. Keep {braces}."
    sid = client.post("/api/agents/mini/actions/go", json={"inputs": {"what": "runs"}}).json()["session_id"]
    runtimes = agents_env.state.runtimes
    for _ in range(100):
        if not runtimes.busy(sid):
            break
        time.sleep(0.02)
    history = client.get(f"/api/runtime/{sid}/history").json()
    first = history["history"][0]["content"]
    assert first.startswith("⚡ Go\nwhat: runs · count: 2\n\nDo runs x2")
    assert history["session"]["kind"] == "agent" and history["session"]["owner"] == "mini"
    listed = client.get("/api/sessions", params={"kind": "agent", "owner": "mini"}).json()["sessions"]
    assert [s["id"] for s in listed] == [sid] and listed[0]["title"].startswith("Go: runs")
    assert agents_env.state.agents.last_run("mini")["action"] == "go"
    # the profile factory rebuilds an agent profile for a reloaded session
    runtimes.close(sid)
    assert runtimes.get(sid).profile.owner == "mini"
    again = client.post("/api/agents/mini/chat", json={"text": "hi", "session_id": sid})
    assert again.json()["session_id"] == sid


def test_records_routes_and_exports(agents_env):
    client, manager = agents_env.client, agents_env.state.agents
    events = []
    agents_env.jarvis.events.subscribe(lambda e: events.append(e) if e.kind == "agent_records_changed" else None)
    manager.table("mini", "items").add([{"title": "A", "url": "https://a.com"}, {"title": "B", "state": "done"}])
    assert events and events[-1].data == {"agent_id": "mini", "table": "items", "change": "add"}
    listing = client.get("/api/agents/mini/records/items").json()
    assert listing["total"] == 2 and listing["table"]["name"] == "items"
    assert client.patch("/api/agents/mini/records/items/1", json={"fields": {"state": "done"}}).json()["state"] == "done"
    assert client.patch("/api/agents/mini/records/items/1", json={"fields": {"state": "x"}}).status_code == 400
    assert client.patch("/api/agents/mini/records/items/9", json={"fields": {"state": "new"}}).status_code == 404
    client.patch("/api/agents/mini/records/items/1", json={"fields": {"state": "new"}})
    csv_response = client.get("/api/agents/mini/exports/csv")
    assert csv_response.headers["content-disposition"].startswith('attachment; filename="mini-csv-')
    assert csv_response.content.decode("utf-8").startswith("﻿title,url,score,state")
    sheet = client.get("/api/agents/mini/exports/sheet").text
    assert sheet == "# Mini\n- 1 A <https://a.com>\nTotal 2\n"
    assert client.get("/api/agents/mini/exports/nope").status_code == 404
    assert client.delete("/api/agents/mini/records/items/2").json() == {"deleted": "2"}
    assert client.get("/api/agents/mini/records/nope").status_code == 404


def test_import_review_install_trust_export_roundtrip(agents_env, tmp_path):
    client, manager = agents_env.client, agents_env.state.agents
    source = write_plugin(tmp_path / "src", MINI.replace('id = "mini"', 'id = "shouter"'), tool=TOOL,
                          extra={"skills/tips.md": "# Tips\nBe loud."})
    review = client.post("/api/agents/import", files={"file": ("s.zip", zip_of(source, "shouter/"), "application/zip")}).json()
    assert review["id"] == "shouter" and review["py_files"] == ["tools/shout.py"] and review["existing"] is None
    assert review["skills"] == ["tips"]
    detail = client.post("/api/agents/shouter/install", json={"staging": review["staging"]}).json()
    assert detail["needs_trust"] and detail["tools"] == []
    profile = manager.profile("shouter")
    assert "shout" not in profile.extra_tools and "agent_skill" in profile.extra_tools
    assert profile.extra_tools["agent_skill"](name="shouter/tips").startswith("# Tips")
    detail = client.post("/api/agents/shouter/trust", json={"trusted": True}).json()
    assert detail["trusted"] and detail["tools"] == ["shout"] and detail["tool_errors"] == []
    manager.save_settings("shouter", {"topic": "dogs"}, {"API_KEY": "sk-zzzzzz"})
    out = manager.profile("shouter").extra_tools["shout"]("hey")
    assert out == {"text": "HEY", "topic": "dogs", "key": True}
    assert manager.table("shouter", "items").all()[0]["title"] == "HEY"
    # changing the code revokes trust
    tool_file = manager.get("shouter").path / "tools" / "shout.py"
    tool_file.write_text(TOOL + "\n# changed\n", encoding="utf-8")
    assert manager.get("shouter").needs_trust
    manager.trust("shouter")
    # export with data, then re-import as an upgrade (keeps data) and as a copy (restores data)
    exported = client.get("/api/agents/shouter/export.zip", params={"data": 1})
    assert exported.headers["content-disposition"] == 'attachment; filename="shouter-1.0.0-with-data.zip"'
    names = zipfile.ZipFile(io.BytesIO(exported.content)).namelist()
    assert "shouter/data/records/items.json" in names and "shouter/tools/shout.py" in names
    assert not any("settings" in n or "trust" in n for n in names)
    assert b"sk-zzzzzz" not in exported.content
    review = client.post("/api/agents/import", files={"file": ("x.zip", exported.content, "application/zip")}).json()
    assert review["existing"]["version"] == "1.0.0" and review["data"] == {"items": 1}
    assert client.post("/api/agents/shouter/install", json={"staging": review["staging"]}).status_code == 409
    copy = client.post("/api/agents/shouter/install", json={"staging": review["staging"], "mode": "copy",
                                                            "trust": True}).json()
    assert copy["id"] == "shouter-2" and copy["name"] == "Mini (copy)"
    assert copy["trusted"] and copy["records"][0]["count"] == 1
    review = client.post("/api/agents/import", files={"file": ("x.zip", exported.content, "application/zip")}).json()
    upgraded = client.post("/api/agents/shouter/install", json={"staging": review["staging"], "mode": "upgrade"}).json()
    assert upgraded["records"][0]["count"] == 1 and upgraded["needs_trust"] is False  # same code: still trusted
    # remove
    assert client.delete("/api/agents/shouter-2").json() == {"removed": "shouter-2"}
    assert not manager.data_dir("shouter-2").exists()
    assert client.delete("/api/agents/mini").status_code == 400  # built-in


def test_import_errors_are_readable(agents_env, tmp_path):
    client = agents_env.client
    source = write_plugin(tmp_path / "bad", MINI.replace('type = "text"\nrequired', 'type = "nope"\nrequired', 1))
    response = client.post("/api/agents/import", files={"file": ("b.zip", zip_of(source), "application/zip")})
    body = response.json()
    assert response.status_code == 400 and "settings[0].type: 'nope' is not a valid type" in body["error"]
    assert body["issues"] == ["agent.toml → settings[0].type: 'nope' is not a valid type (use one of: "
                              "text, textarea, number, boolean, select, multiselect, url, secret)"]
    response = client.post("/api/agents/import", files={"file": ("b.zip", b"junk", "application/zip")})
    assert response.json()["error"] == "Can't import: this is not a valid .zip file"
    response = client.post("/api/agents/import", json={"url": "ftp://x"})
    assert "GitHub link" in response.json()["error"]
    response = client.post("/api/agents/import", json={"path": str(source)})
    assert response.status_code == 400
    assert not list((agents_env.state.agents.staging_dir).glob("*"))  # failed imports leave nothing behind


def test_folder_import_create_and_duplicate(agents_env, tmp_path):
    client, manager = agents_env.client, agents_env.state.agents
    source = write_plugin(tmp_path / "folder", MINI.replace('id = "mini"', 'id = "from-disk"'))
    review = client.post("/api/agents/import", json={"path": str(source)}).json()
    assert review["id"] == "from-disk" and review["py_files"] == []
    client.post("/api/agents/from-disk/install", json={"staging": review["staging"]})
    created = client.post("/api/agents", json={"name": "Poem Writer!", "description": "Writes poems",
                                               "prompt": "You write short poems."}).json()
    assert created["id"] == "poem-writer" and created["builtin"] is False
    assert "You write short poems." in manager.profile("poem-writer").system_prompt
    assert client.post("/api/agents", json={"name": "x"}).json()["fields"] == {"prompt": "is required"}
    manager.save_settings("mini", {"topic": "birds"})
    dup = client.post("/api/agents/mini/duplicate").json()
    assert dup["id"] == "mini-2" and dup["name"] == "Mini (copy)" and not dup["builtin"]
    assert manager.settings_values("mini-2")["topic"] == "birds"
    ids = [a["id"] for a in client.get("/api/agents").json()["agents"]]
    assert ids[0] == "mini" and set(ids) == {"mini", "from-disk", "poem-writer", "mini-2"}


def test_broken_installed_plugin_is_listed(agents_env):
    manager = agents_env.state.agents
    folder = manager.agents_dir / "broken"
    folder.mkdir(parents=True)
    (folder / "agent.toml").write_text("schema = 2\n", encoding="utf-8")
    listing = agents_env.client.get("/api/agents").json()
    assert listing["broken"][0]["folder"] == "broken" and "schema" in listing["broken"][0]["message"]


def test_plugin_tool_errors_are_reported(agents_env, tmp_path):
    manager = agents_env.state.agents
    folder = write_plugin(manager.agents_dir / "oops", MINI.replace('id = "mini"', 'id = "oops"'),
                          tool="def register(ctx):\n    raise RuntimeError('boom')\n")
    (folder / "tools" / "empty.py").write_text("x = 1\n", encoding="utf-8")
    manager.trust("oops")
    detail = manager.detail("oops")
    assert {e["file"] for e in detail["tool_errors"]} == {"tools/shout.py", "tools/empty.py"}
    assert "RuntimeError: boom (line 2)" in json.dumps(detail["tool_errors"])


def test_small_model_guards(agents_env):
    """Placeholders are refused, quoted-empty filters work, query-language `search` falls back to `where`."""
    manager = agents_env.state.agents
    tools = manager.profile("mini").extra_tools
    tools["records_add"](items=[{"title": "a"}, {"title": "b", "state": "done"}])
    refused = tools["records_update"](id="1", fields={"title": "Hi [Name], loved [Specific Detail]"})
    assert "placeholder [Name]" in refused["error"]
    assert tools["records_query"](where={"url": '""'})["total"] == 2
    result = tools["records_query"](where={"state": "done"}, search="state:done AND title:b")
    assert result["total"] == 1 and "plain text" in result["note"]
