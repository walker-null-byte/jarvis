# Jarvis 1.1.2 — architecture

1.1.2 adds a stronger agent harness, saved API keys, a Claude-style sidebar, and three new
features: **Agents** (specialised, importable agent plugins), **Routines** (recurring tasks,
replacing the old cron jobs) and **Discussions** (agenda-driven group chats between several
agents, which may use different providers).

The rule throughout: keep the code simple, debuggable modules. No frameworks and no build
step. The vanilla ES-module frontend uses the `el()` helper and never `innerHTML` for model
or user text.

---

## 0. Shared foundation (everything else builds on this)

### 0.1 Many agents in one process — `jarvis/runtime.py`

Today the web UI has exactly one `Jarvis` instance (`WebState.jarvis`). Several 1.1.2
features need more than one agent loop at a time: Agents chats, Discussions, Routines and
subagents. So `Jarvis` gains an optional **profile**:

```python
@dataclass
class AgentProfile:
    kind: str = "chat"          # chat | agent | discussion | routine | subagent
    owner: str = ""             # agent plugin id / discussion id / routine id
    title: str = ""             # session title for new sessions
    system_prompt: str = ""     # extra system-prompt section (role, workflow), appended after the persona
    replace_persona: bool = False  # True: system_prompt replaces the character card persona
    llm: dict | None = None     # overrides merged over config["llm"] (provider/model/base_url/api_key_env)
    core_tools: list = field(default_factory=list)   # extra tool names exposed directly
    extra_tools: dict = field(default_factory=dict)  # name -> callable, visible only to this agent
    hidden_tools: set = field(default_factory=set)   # tool names this agent must not see/use
    max_tool_rounds: int | None = None
    persist: bool = True        # False: no session files (ephemeral subagents)
    allow_spawn: bool = True    # False for subagents (depth limit 1)
```

`Jarvis(config, session_id=None, event_bus=None, profile=None)`:
- Uses its own deep copy of `config`, with `profile.llm` merged into `config["llm"]`, and its
  own `LLM`.
- Its `ToolExposure` adds `profile.core_tools` and `profile.extra_tools` to the fixed tools
  array, and filters out `hidden_tools` everywhere: the array, `find_tools` and `call_tool`.
- The session is created with `kind` and `owner` in its meta (see 0.3).

Helpers in `runtime.py`:
- `llm_overrides(provider, model="")` returns the `config["llm"]` override dict for a preset
  (base_url, api_key_env, provider, model). It falls back to the preset's `default_model`,
  then to the model remembered for that provider. Discussions and Agents call this to give
  each participant its own provider.
- `class Runtimes`: owned by `WebState`. It holds extra `Jarvis` instances keyed by
  session_id, all sharing one `EventBus`:
  - `get(session_id)` loads an instance lazily, with the profile rebuilt from the session meta
    via a registered **profile factory**:
    `register_profile_factory(kind, fn(owner, session_meta) -> AgentProfile)`.
  - `create(profile)` makes a new instance.
  - `start_turn(session_id, text)` runs the turn in a worker thread and returns False if that
    instance is busy.
  - `cancel(session_id)`.
  - `find_approval(request_id)` finds the instance holding the request; used by
    `/api/approvals/{id}`.

  The main chat stays `WebState.jarvis`. Each instance is busy on its own, so an Agents run
  can proceed while you chat.

### 0.2 Per-agent tool context (approvals, cancel)

`ToolRegistry.set_approval_handler` and `set_cancel_event` are currently **global**. With
several agents running, the last one would win. They become **context-local**: a
`contextvars.ContextVar` holds `(approvals, cancel_event, agent)` for the current tool call.
`Jarvis._run_tool` sets it, and the policy and command runner read it, falling back to the
old global for CLI and tests. Code that runs tools in a thread pool must use
`contextvars.copy_context().run(...)`.

### 0.3 Sessions carry a kind

`SessionStore.create(title, character, kind="chat", owner="")` stores `kind` and `owner` in
the meta. `list()` and `search()` accept `kind=` (default `"chat"`, so Recents shows normal
chats only). `GET /api/sessions?kind=agent&owner=lead-finder` lists one agent's chats. Old
sessions without a kind are `"chat"`.

### 0.4 Events are routed by session

Every `Event` already has `session_id`. The frontend must dispatch each event to the view
showing that session:
- The main `Chat` ignores events for other sessions.
- Views subscribe with `bus.on(fn)` and filter by `session_id` (or by `discussion_id` in the
  data).
- An approval request for a session that isn't on screen still shows a toast with
  Approve/Deny, so background agents never hang silently.

### 0.5 Saved API keys — `jarvis/keystore.py`

- The file is `$XDG_CONFIG_HOME/jarvis/keys.json`, with the dir at 0700 and the file at 0600,
  the same place and rules as `chatgpt_auth.json`. The env var `JARVIS_KEYS_FILE` overrides
  the path, and tests always set it.
- `get(name)`, `set(name, value)`, `delete(name)`, `names()` (never values), `hint(name)`
  (the last 4 characters).
- Names are env-var style (`GEMINI_API_KEY`). Plugin secrets are namespaced
  `agent:<id>:<KEY>`.
- Key resolution (`resolve_key`): session literal, then **saved key**, then environment
  variable, then `"not-needed"`.
- Settings UI: below the key field there is a "Remember on this computer" checkbox, **on by
  default**. When a saved key exists the status line reads "Saved key ••••abcd", with a
  **Forget** button. `/api/providers` reports `saved: bool` per key env and never values.
- Saved values are redacted anywhere they could surface: tool results, logs, exports. Add
  them to `redaction.py`'s dynamic secret list.

### 0.6 Sidebar navigation + view router (Claude-style)

The left rail becomes:

```
[orb] JARVIS
+ New chat            Ctrl+N
⌕ Search              Ctrl+K
◎ Agents
⁂ Discussions
⟳ Routines            (badge: unread run results)
⚒ Tools               (tools, skills, MCP servers)
──────────────
Recents ▾
  …chat sessions…
```

- Hash router in `static/js/router.js`, with routes `#/chat/<sid>`, `#/agents`,
  `#/agents/<id>`, `#/discussions`, `#/discussions/<id>`, `#/routines`, `#/tools`. Back and
  forward work. The default is the current chat.
- View contract in `static/js/views.js`: `registerView(name, { mount(container, params, ctx),
  unmount() })`. A view renders into `#center` in place of the chat; the chat DOM is hidden,
  not destroyed. `ctx` = `{ api, bus, toast, navigate }`.
- Each feature lives in its own files: `view_agents.js`, `view_discussions.js`,
  `view_routines.js`, `view_tools.js`, plus CSS in `css/views.css`.
- The right drawer keeps Characters, Mind, Memory, Voice, Settings and Activity. The drawer's
  Tools & Skills and Jobs tabs move into the Tools and Routines views.
- At phone width (≤ 760px) the rail is an overlay, as today, and nav items close it on click.

### 0.7 Backend route modules

`server.py` includes `routes_agents.py`, `routes_discussions.py`, `routes_routines.py` and
`routes_tools.py`. Each defines `router = APIRouter(prefix="/api")` and uses the `web`
dependency from `state.py`. Feature state lives on `WebState`:
- `state.runtimes`
- `state.agents` (`AgentManager`)
- `state.discussions` (`DiscussionManager`)
- `state.routines` (`RoutineScheduler`)

Each is created lazily via a property, so tests that don't need one don't pay for it.

---

## 1. Harness upgrades

What the strongest harnesses do, and what Jarvis adopts:

| Harness | Idea | Jarvis 1.1.2 |
|---|---|---|
| Claude Code | Read with line ranges, exact-string Edit, Grep/Glob tools, parallel read-only tools, subagents with their own context, MCP client with deferred tool loading, large outputs spilled to files | All of these |
| Codex CLI | `apply_patch` for multi-file edits, MCP servers in config, subagents (max_depth 1, capped threads), OS sandbox | MCP config, subagent caps; `edit_file` instead of a patch format (easier for small local models); sandbox listed under follow-ups |
| DeepSeek Harness | Everything is a plugin (model adapter, tools, session log, loop); "code mode" (the model writes code that calls tools, so only the final result enters context); thinking interleaved with tool calls | `reasoning_content` echoed for DeepSeek tool turns; code mode listed under follow-ups |
| Anthropic API | `defer_loading` tool search: keep 3–5 hot tools loaded, discover the rest | This is Jarvis's existing `find_tools`/`call_tool`, kept and extended to MCP and plugin tools |

### 1.1 Parallel tool calls
When a model reply contains several tool calls:
- Calls to **read-only** tools run concurrently: `read_file`, `grep`, `glob`, `list_dir`,
  `recall`, `find_tools`, `read_skill`, web search/fetch, `spawn_agent`, and MCP tools whose
  annotations say `readOnlyHint`. They use a `ThreadPoolExecutor(max_workers=agent.max_parallel_tools=6)`
  under the copied context.
- Everything else runs sequentially, in order, after the parallel batch, because approvals
  are serial.
- Results are appended to history in the **original call order**.
- `tool_started` and `tool_finished` carry `call_id`, and the UI matches cards by `call_id`.

Setting: `agent.parallel_tools = true`.

### 1.2 Coding/file tools (core, always visible)
- `read_file(path, offset=1, limit=2000)`: numbered lines (`  12→text`), plus
  `{total_lines, truncated}`. Binary files are refused.
- `edit_file(path, old_string, new_string, replace_all=false)`: exact match. It fails with a
  helpful message if there are 0 or more than 1 matches. It goes through the same policy as
  `write_file`.
- `grep(pattern, path=".", glob="", ignore_case=false, context=0, output="content"|"files"|"count", max_results=200)`:
  uses ripgrep when it's on PATH, with a pure-Python fallback. Respects `allowed_roots`.
- `glob(pattern, path=".")`: newest first, capped at 500.

New default `core_tools`:

```
run_command, read_file, edit_file, write_file, grep, glob, remember, recall
```

`list_dir` stays available via search. Configs still holding the old default list are
upgraded automatically.

### 1.3 Large outputs spill to disk
A tool result over 8,000 characters is saved to `mind/tool_outputs/<turn>-<n>.txt`, and the
model gets the head (5,000 characters) plus the tail (2,000) and a note: "Full output (N
chars) saved to PATH; page through it with read_file(offset, limit) or grep." Files older
than 7 days are pruned at startup. This keeps context small without losing data.

### 1.4 Background commands
- `run_command(command, timeout=60, background=false)`: `background=true` returns
  `{job_id}` immediately.
- `command_output(job_id, tail=200)` returns status, exit code and the last lines.
- `command_kill(job_id)` stops it.

Use these for builds, servers and long scripts. They follow the same policy and approvals
as `run_command`.

### 1.5 MCP client — `jarvis/mcp_client.py`
- Config file `mind/mcp.json` uses the **Claude Desktop format**, so users can paste the
  snippet from any MCP server's README:
  ```json
  {"mcpServers": {
     "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}},
     "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${MY_KEY}"}},
     "off":    {"command": "...", "disabled": true}
  }}
  ```
  `${NAME}` is filled from the keystore, then the environment.
- It uses the official `mcp` Python SDK (added as a dependency) for stdio and
  streamable-HTTP transports. One background asyncio thread owns all sessions. Servers
  start in the background at startup and never block the UI or first message.
- Tools are registered into the registry as `mcp__<server>__<tool>`, with the server's
  description and input schema. They are **hidden by default** (reached via `find_tools`),
  so 50 MCP tools cost no context.
- Approvals: tools annotated `readOnlyHint: true` run without asking. All others ask the
  first time per server per session (approve once / always for this server / deny). A
  server marked `"trust": true` never asks. `destructiveHint` tools always ask.
- Tools view → **MCP servers** section:
  - list: name, status (connecting / connected · N tools / failed + error), enable toggle,
    restart, remove
  - "Add server": paste JSON (a single server or a full `mcpServers` object) or fill a small
    form
  - live status via `mcp_status` events
- API: `GET/POST /api/mcp/servers`, `PATCH/DELETE /api/mcp/servers/{name}`,
  `POST /api/mcp/servers/{name}/restart`.

### 1.6 Subagents — `spawn_agent`
`spawn_agent(task, agent="general", context="")` is a core tool, but only when the profile
has `allow_spawn`:
- It runs a fresh `Jarvis` with `kind="subagent"`, `allow_spawn=False` (depth 1), its own
  history, and `max_tool_rounds` 30.
- It returns the subagent's final answer, capped at 6,000 characters, plus
  `{tool_calls, seconds}`.
- `agent` may be an installed Agents plugin id, so the main chat can delegate to the Lead
  Finder.
- Parallel tool calls make several spawns run concurrently, capped at 3 at once.
- The subagent's session is persisted (`kind="subagent"`, owner = parent session id) so it
  can be inspected. The tool card links to it.
- Use it for broad research or exploration whose raw output would bloat the main context.

### 1.7 Tool budget
`max_tool_rounds` default goes from 20 to 50. When the budget runs out, the wrap-up reply is
followed by a **Continue** button in the chat. It sends "Continue where you left off." as a
normal message.

### 1.8 DeepSeek thinking + tools
On the deepseek preset, when the assistant message that carried tool_calls had reasoning,
send it back as `reasoning_content` within the same turn, as the DeepSeek thinking-mode docs
require. Strip it from earlier turns.

---

## 2. Agents — specialised agent plugins

An **Agent** is a self-contained folder that turns Jarvis into a specialist: its own role
prompt, settings (static inputs), actions (buttons with dynamic inputs), structured records
(tables it fills), exporters, optional Python tools and skills, and its own chat. It can
still use every Jarvis tool.

### 2.1 Folder format (`schema = 1`)
```
lead-finder/
  agent.toml        manifest (required)
  prompt.md         role + workflow + rules (required)
  README.md         shown in the About tab
  icon.svg          optional; otherwise `icon` names a built-in icon
  tools/*.py        optional plugin tools: def register(ctx): ...
  skills/*.md       optional skills, readable with read_skill("lead-finder/<name>")
  exports/*.md      optional export templates
```

Where things live:
- Built-in agents ship in `jarvis/agents_builtin/<id>/` and are read-only. "Duplicate"
  copies one into `mind/agents/<id>/` to customise.
- Installed agents live in `mind/agents/<id>/`.
- User data lives apart, in `mind/agent_data/<id>/`, so re-importing or upgrading a plugin
  never loses it: `settings.json`, `records/<table>.json`, `runs.jsonl`. Secrets go in the
  keystore as `agent:<id>:<KEY>` and are **never** exported.

### 2.2 `agent.toml`
```toml
schema = 1
id = "lead-finder"            # [a-z0-9-], unique
name = "Lead Finder"
version = "0.1.0"
description = "Finds high-quality leads and drafts personal outreach."
author = "Jarvis"
icon = "target"
min_jarvis = "1.1.2"

[agent]
prompt = "prompt.md"
greeting = "Tell me who you're looking for, or press Find leads."
provider = ""                 # empty = whatever the user has selected; else a preset id
model = ""
core_tools = ["web_search", "fetch_page"]   # tools shown directly (plugin or Jarvis tools)
max_tool_rounds = 60

[permissions]                 # declared up front; shown before trusting an import
network = true
commands = false              # false: run_command is hidden from this agent
files = "data"                # "data" (own data dir only) | "workspace" | "none"

[[settings]]                  # static inputs, set once (Settings tab)
key = "offer"
label = "What are you promoting?"
type = "textarea"             # text|textarea|number|boolean|select|multiselect|url|secret
required = true
help = "One or two sentences: the work, event or service."

[[settings]]
key = "SERPER_API_KEY"
label = "Serper.dev API key (optional, better Google results)"
type = "secret"

[[records]]                   # structured tables the agent fills; shown in the Workspace
name = "leads"
label = "Leads"
unique = ["profile_url"]      # de-duplication key(s)
title_field = "name"
fields = [
  { name = "name", type = "text", required = true },
  { name = "platform", type = "select", options = ["instagram", "linkedin", "other"] },
  { name = "profile_url", type = "url" },
  { name = "fit_score", type = "number", min = 1, max = 5 },
  { name = "status", type = "select", default = "new", editable = true,
    options = ["new", "contacted", "replied", "call_link_sent", "booked", "not_interested"] },
]

[[actions]]                   # buttons; dynamic inputs asked at run time
id = "find"
label = "Find leads"
description = "Search Instagram and LinkedIn for people who fit."
inputs = [
  { key = "niche", label = "Who are you looking for?", type = "text", required = true },
  { key = "count", label = "How many", type = "number", default = 10 },
]
prompt = """Find {count} high-quality leads: {niche}. ..."""   # {input}, {settings.key}, {records.leads.count}

[[exports]]
id = "csv"
label = "Leads (CSV)"
records = "leads"
format = "csv"                # csv | json | markdown | template
[[exports]]
id = "outreach"
label = "Outreach sheet"
format = "template"
template = "exports/outreach.md"   # {{#each leads}}…{{/each}}, {{field}}; tiny built-in engine
```

### 2.3 Plugin tool API (`tools/*.py`)
```python
def register(ctx):
    @ctx.tool
    def find_contact(name: str, hint: str = "") -> dict:
        """Search the web for a person's public email/phone/website."""
        key = ctx.settings.get("SERPER_API_KEY")  # secrets resolved from the keystore
        ...
        return {...}
```

`ctx` provides:
- `tool`: a decorator. The name becomes `<id>__<fn>` internally, but the agent sees the short
  name.
- `settings`: a read-only dict, with secrets resolved.
- `records(name)` → `.add(items)`, `.update(id, fields)`, `.query(where=None, limit=50)`,
  `.get(id)`, `.delete(id)`.
- `data_dir`
- `http`: a shared `httpx.Client` with a timeout and a polite User-Agent.
- `log(msg)`

Plugin tools run in-process (same trust model as `tools/`). Code is only loaded after the
user **trusts** the plugin.

Every agent also automatically gets:
- record tools for its tables: `records_add`, `records_update`, `records_query`,
  `records_delete`, each validated against the field schema
- `web_search(query, num=10)`, a Jarvis tool: Serper if `SERPER_API_KEY`, Brave if
  `BRAVE_API_KEY`, otherwise the free `ddgs` package
- `fetch_page(url)`, a Jarvis tool: readable text plus extracted emails, phones and social
  links

### 2.4 Import, trust, export
- Import from:
  - a `.zip` upload
  - a GitHub URL (a repo, or a `tree/<ref>/<subdir>` path, fetched as a zip)
  - a local folder path

  Import validates the manifest (schema, id, required files, unknown keys warned), then shows
  a **review card**: name, version, author, permissions, list of Python tool files, and an
  "Install" button. Prompt-only agents need no trust.
- When the id already exists, choose **Upgrade** (keep data) or **Install as copy**.
- Export: "Export agent" downloads a `.zip` of the plugin folder (no data, no secrets).
  "Export with data" adds `data/records/*` but still no secrets.
- API: `GET /api/agents`, `GET /api/agents/{id}`, `POST /api/agents/import`
  (multipart or `{url}`), `POST /api/agents/{id}/install`, `POST /api/agents/{id}/trust`,
  `DELETE /api/agents/{id}`, `POST /api/agents/{id}/duplicate`,
  `GET /api/agents/{id}/export.zip`, `GET/PUT /api/agents/{id}/settings`,
  `GET /api/agents/{id}/records/{table}`, `PATCH`/`DELETE` a record,
  `POST /api/agents/{id}/actions/{action}` (`{inputs, session_id?}` → `{session_id}`),
  `GET /api/agents/{id}/exports/{export}`, and `POST /api/agents/{id}/chat`
  (`{text, session_id?}`).

### 2.5 The Agents UI
- **Agents** grid: a card per agent (icon, name, description, record counts), plus "Import"
  and "New agent". "New agent" opens a small wizard (name, description, role prompt) that
  writes a prompt-only plugin.
- **Agent workspace** (`#/agents/<id>`), two columns on desktop and tabs on mobile:
  - Left: **Actions** (a button each; clicking opens an inline input form → Run), then
    **Records** (a sortable table, inline status editing, row menu: open profile, copy
    message, delete, plus search and status filter chips), then **Export ▾**.
  - Right: **Chat** with this agent (session picker for its past chats, normal streaming
    chat UI reused from `chat.js`). Running an action posts it into the current agent chat,
    so progress streams live.
  - Header: icon, name, version, Settings (drawer-style form, incl. secrets with
    "Saved ••••"), About, ⋯ (Duplicate, Export, Remove).
  - First run: if required settings are missing, Settings opens with a short explanation.

### 2.6 Interfaces other features use
```python
AgentManager.list() -> [summary]
AgentManager.get(id) -> Plugin            # manifest, paths, trusted
AgentManager.profile(id) -> AgentProfile  # for Runtimes / spawn_agent / discussions
AgentManager.render_action(id, action_id, inputs) -> str   # validated, templated prompt
AgentManager.run_action(id, action_id, inputs, session_id=None, runtimes) -> session_id
```

### 2.7 Proof of concept: **Lead Finder** (built-in)
This encodes the workflow from the meeting brief:
1. **Discover** names via public Instagram/LinkedIn profiles (web search with `site:`
   queries plus the niche/location).
2. **Qualify**: quality over quantity, scored 1–5 against the ideal-lead description, with a
   one-line reason. Below `min_score` is dropped.
3. **Enrich**: Google the person for a public email/phone/website. Never guess or fabricate
   contact details; record the source URL for each.
4. **Outreach drafts**: a personal first message per lead, which the user sends themselves
   by Instagram DM or LinkedIn message. Jarvis never sends DMs or automates those platforms,
   which would break their terms and risk the account.
5. **Follow-up**: when a lead is marked *replied*, draft the follow-up with the calendar
   link. Status pipeline: new → contacted → replied → call_link_sent → booked /
   not_interested.

Settings:

| Setting | Notes |
|---|---|
| offer | |
| ideal_lead | |
| sender_name | |
| calendar_link | |
| tone | |
| min_score | default 3 |
| SERPER_API_KEY | optional |
| BRAVE_API_KEY | optional |

Actions: **Find leads** (niche, platforms, location, count), **Find contact details**
(leads missing contacts), **Draft first messages**, **Draft follow-ups** (replied leads),
**Pipeline review**.

Exports: Leads CSV, Outreach sheet (markdown), JSON.

---

## 3. Routines — recurring and timed tasks (replaces cron jobs)

`jarvis/routines.py` provides an **in-process scheduler**. Cron-file jobs only ran when cron
was installed and gave poor feedback.

Routine (`mind/routines/<id>.json`):

```json
{"id": "...", "name": "Morning notes digest", "enabled": true,
 "schedule": {"kind": "cron", "cron": "0 9 * * 1-5"},
 "target": {"kind": "prompt", "prompt": "Summarize ~/notes ..."},
 "on_missed": "run_once",
 "notify": true,
 "last_run": "...", "next_run": "...",
 "runs": [{"started": "...", "finished": "...", "status": "ok", "session_id": "...", "summary": "first 200 chars"}]}
```

`schedule.kind` is one of:
- `cron`: 5-field
- `interval`: `{"every_minutes": N}`
- `once`: `{"at": ISO}`

`target.kind` is one of:
- `prompt`: ask Jarvis
- `agent_action`: `{agent, action, inputs}`
- `agent_prompt`: `{agent, prompt}`

`on_missed` is `run_once` or `skip`. `runs` keeps the last 50.

- A small built-in cron parser (5 fields; `*`, lists, ranges, steps, names like `mon`) gives
  next-run times. There's no croniter dependency.
- `parse_schedule("every weekday at 9")` understands common English, which the tools and
  the UI hint use:
  - "every N minutes/hours"
  - "every day at HH:MM"
  - "weekdays at …"
  - "every monday at …"
  - "on the 1st at …"
  - "in 20 minutes"
  - "tomorrow at 8"
  - "at 2026-12-01 09:00"
  - raw cron
- **Scheduler thread** starts with `jarvis --web`, and with the new `jarvis --daemon`
  (headless, used by `jarvis.service`):
  - It wakes every 20 s and runs due routines in worker threads, at most 2 concurrently.
  - Each run creates a session `kind="routine"`, `owner=<routine id>`, titled
    "<name> — <date>".
  - Unattended approvals: when the web UI is connected, the approval is requested (toast +
    Routines badge) and waits the normal timeout. Otherwise it is denied, and the run records
    that it needed approval.
- Tools replace `cron_*`: `routine_create(name, schedule, prompt, agent?, action?)`,
  `routine_list`, `routine_update`, `routine_delete`, `routine_run_now`. The old `cron_*`
  names stay as hidden aliases.
- Migration: existing `mind/jobs/*.md` become routines once. The old system-crontab entries
  are reported, and removing them is offered, never done silently.
- **Routines view**:
  - list: name, human schedule ("Weekdays at 09:00"), next run in relative time, last
    status chip, enable toggle, Run now, ⋯ (edit, duplicate, delete)
  - "New routine" form:
    - **What**: ask Jarvis / run an agent action (agent → action → its inputs)
    - **When**: a picker (every N minutes/hours · daily at · weekly on [days] at · monthly
      on day · once at · custom cron), with a live "Next runs: …" preview of 3 times
    - notify toggle
  - run history per routine, each linking to its session
  - unread results badge in the sidebar
- API: `GET/POST /api/routines`, `PATCH/DELETE /api/routines/{id}`,
  `POST /api/routines/{id}/run`, `GET /api/routines/{id}/runs`,
  `POST /api/routines/preview` (`{schedule}` → next 3 runs, or an error).

---

## 4. Discussions — agenda-driven group chats

`jarvis/discussions.py`. Several agents, each with any provider and model, discuss and
research an **agenda** (mandatory), with the user taking part.

Discussion (`mind/discussions/<id>/discussion.json` + `transcript.jsonl`):

```json
{"id": "...", "title": "...", "status": "draft|running|paused|waiting|finished",
 "agenda": {"goal": "required", "items": [{"title": "required", "notes": ""}],
            "outcome": "decision|plan|report|ideas"},
 "mode": "round_robin|parallel|moderated|debate",
 "rounds_per_item": 2,
 "participants": [{"id": "p1", "name": "Researcher", "role": "role prompt", "provider": "gemini",
                   "model": "gemini-2.5-flash", "tools": true, "stance": "", "color": "#…",
                   "agent": ""}],
 "moderator": {"provider": "", "model": ""},
 "current_item": 0, "minutes": "", "created": "..."}
```

- `participants` holds 2–6 entries. `agent` is an optional Agents plugin id, which supplies
  the role and tools.
- `moderator` empty means the user's current model.

### Modes
- **Round robin (sequential)**: speakers take turns in order. Each sees the whole
  transcript.
- **Parallel**: everyone answers the current item at the same time without seeing each
  other's answers for that round, which avoids anchoring. Next round they all see
  everything.
- **Moderated**: a moderator model picks the next speaker each step (JSON:
  `{"next": "p2", "advance": false, "reason": …}`), decides when an item is done, and can
  call on the user.
- **Debate**: participants have stances (for/against/judge). Sides alternate; the judge
  sums up each item.

### Flow
1. The draft is created with a **mandatory agenda**: a goal plus at least one item. Starting
   is blocked until both exist.
2. It starts on the **Start** button or the user's first message (which becomes the
   opening remark).
3. For each agenda item, run `rounds_per_item` rounds by the mode's rules. Then the
   moderator writes an **item summary** (decisions, open questions). Later items see the
   summaries of earlier items plus the full current item, which keeps contexts small.
4. At the end the moderator writes the **minutes**: per-item outcomes, decisions, action
   items (owner, task) and open questions. They're exportable as Markdown.

### Participants
- Each speaking turn is a `Jarvis` run (`kind="discussion"`, persist=False) with
  `llm_overrides(provider, model)`, `max_tool_rounds` 8, and research tools only:
  `web_search`, `fetch_page`, `recall`, `read_file`, `grep`, `glob`. No shell, no writes.
- The system prompt holds: the goal, the agenda with the current item marked, the role, the
  other participants, and the rules (be concise, ≤ ~180 words unless asked, build on or
  challenge others by name, cite sources for facts, no repeating, say "PASS" if there's
  nothing new).

### The user
- They can send a message at any time. It joins the transcript and the next speaker
  responds to it.
- `@Name` makes that participant answer next.
- Controls: Pause / Resume / Next item / Conclude now / Stop.
- In moderated mode the moderator can set status `waiting` to ask the user something.

### Events and streaming
Events: `discussion_status`, `discussion_speaker` (who is speaking), `discussion_delta`,
`discussion_message`, `discussion_item` (advanced plus summary), `discussion_minutes`. They
carry `discussion_id`, and tokens stream live, as in chat.

### Persistence
Transcripts persist, and a finished discussion can be reopened. **Continue** adds a new
agenda item.

### Discussions view
- **List**: title, status, participants' avatars, agenda progress.
- **Create**, as a single form:
  1. title
  2. **goal** (required)
  3. agenda items (add/remove/reorder; at least 1)
  4. expected outcome
  5. mode, with a one-line explanation of each
  6. rounds per item
  7. participants: add from presets (Researcher, Skeptic, Strategist, Domain expert, Devil's
     advocate) or an installed Agent; per participant: name, role, provider·model, tools
     on/off, stance in debate mode
  8. moderator model
- **Room**:
  - left: the agenda checklist (current item highlighted, item summaries collapsible under
    done items)
  - center: the transcript, with speaker name and colour, provider·model badge, live
    streaming, tool activity chips and the stats footer
  - top: controls and status
  - bottom: the composer ("Speak to the group…", `@mention` autocomplete)
  - end: a Minutes card with Export (.md)

API:
- `GET/POST /api/discussions`
- `GET/PATCH/DELETE /api/discussions/{id}`
- `POST /api/discussions/{id}/start|pause|resume|next|conclude|stop`
- `POST /api/discussions/{id}/message` (`{text}`)
- `GET /api/discussions/{id}/export.md`

---

## 5. Follow-ups (not in 1.1.2)

- **Code mode / programmatic tool calling**: a `run_script` tool where the model writes a
  short Python script that calls Jarvis tools (`tools.web_search(...)`) inside a
  subprocess. Only what the script prints returns to context. This gives big savings for
  batch work, such as enriching 50 leads.
- **OS sandbox**: an opt-in `security.sandbox = "bwrap"` wraps `run_command` in bubblewrap
  (read-only system, writable allowed_roots, optional no-network), the way Codex and Claude
  Code sandbox on Linux.
- **Marketplace**: a GitHub-index catalog for Agents, skills and MCP servers, building on the
  Agents importer.
