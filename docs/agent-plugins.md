# Agent plugins

An **agent** is a folder that turns Jarvis into a specialist: its own role prompt, settings,
action buttons, tables it fills (records), export formats, and optionally its own Python
tools and playbooks. It has its own chat and can still use every Jarvis tool.

Open **Agents** in the sidebar to use, import or create one. The built-in **Lead Finder**
(`jarvis/agents_builtin/lead-finder/`) is a complete, real example worth reading.

## The folder

```
my-agent/
  agent.toml        manifest (required)
  prompt.md         role, workflow and rules (required)
  README.md         shown under About
  icon.svg          optional; otherwise `icon` names a built-in icon
  tools/*.py        optional Python tools (need the user's trust)
  skills/*.md       optional playbooks, read with agent_skill("<name>")
  exports/*.md      optional export templates
```

Where things live:

| What | Where |
|---|---|
| Built-in agents (read-only) | `jarvis/agents_builtin/<id>/` |
| Installed agents | `<mind>/agents/<id>/` (the folder name must equal `id`) |
| Your data for an agent | `<mind>/agent_data/<id>/`: `settings.json`, `records/<table>.json`, `runs.jsonl`, `trust.json` |
| Secrets | the Jarvis keystore, as `agent:<id>:<KEY>` (never exported) |

Data is kept apart from the plugin, so upgrading or re-importing an agent never loses it.
**Duplicate** (⋯ menu) copies any agent, built-in ones included, into `<mind>/agents/` with a
new id so you can edit its files.

## A minimal agent

`agent.toml`:

```toml
schema = 1
id = "recipe-planner"
name = "Recipe Planner"
version = "0.1.0"
description = "Plans a week of dinners from what's in the fridge."

[agent]
prompt = "prompt.md"
greeting = "What's in your fridge?"

[[settings]]
key = "diet"
label = "Diet"
type = "select"
options = ["anything", "vegetarian", "vegan"]
default = "anything"

[[records]]
name = "meals"
fields = [
  { name = "day", type = "text", required = true },
  { name = "dish", type = "text" },
  { name = "status", type = "select", options = ["planned", "cooked"], default = "planned", editable = true },
]

[[actions]]
id = "plan"
label = "Plan the week"
inputs = [{ key = "ingredients", label = "What do you have?", type = "textarea", required = true }]
prompt = "Plan 5 dinners ({settings.diet}) using: {ingredients}. Save each with records_add."

[[exports]]
id = "csv"
label = "Meal plan (CSV)"
format = "csv"
records = "meals"
```

`prompt.md`: plain markdown describing the role, the workflow and the rules.

Zip the folder (or push it to GitHub) and import it from the Agents page.

## `agent.toml` reference

Unknown keys produce warnings, not errors, so newer plugins still load on older Jarvis.
Errors name the file, the key and the problem, e.g.
`agent.toml → settings[2].type: 'colour' is not a valid type (use one of: …)`.

### Top level

| Key | | |
|---|---|---|
| `schema` | required | always `1` |
| `id` | required | `a-z`, `0-9`, `-`; at most 48 characters; unique |
| `name` | required | shown everywhere |
| `version` | | like `1.2.0` (default `0.1.0`) |
| `description`, `author`, `homepage`, `license`, `tags` | | shown on the card and in About |
| `icon` | | a built-in icon name (`agents`, `target`, `mail`, `search`, `chart`, `globe`, `calendar`, `pen`, `briefcase`, `zap`, `book`, `users`, `wrench`, …) or ship `icon.svg` (plain SVG; scripts and external links are refused) |
| `min_jarvis` | | the lowest Jarvis version this agent works with; older Jarvis refuses to import it |

### `[agent]`

| Key | Default | |
|---|---|---|
| `prompt` | `prompt.md` | the role prompt file |
| `greeting` | | markdown shown in an empty chat |
| `suggestions` | `[]` | starter questions shown as chips in an empty chat |
| `provider`, `model` | follow the main chat | a provider preset id (`lmstudio`, `gemini`, `anthropic`, …) and model; the user can override both in Settings |
| `core_tools` | `[]` | Jarvis tools the agent sees directly (e.g. `["web_search", "fetch_page"]`); all other tools stay reachable via `find_tools` |
| `max_tool_rounds` | Jarvis setting | 1–200 |
| `replace_persona` | `true` | the prompt replaces the Jarvis character persona; `false` adds it after the persona |

### `[permissions]`

Shown to the user before installing.

| Key | Default | Effect |
|---|---|---|
| `network` | `true` | `false` hides `web_search`/`fetch_page` and blocks `ctx.http` |
| `commands` | `false` | `false` hides `run_command`, `command_output`, `command_kill` |
| `files` | `"data"` | `"data"`: the write tools (`write_file`, `edit_file`) are hidden, reading still follows Jarvis's normal rules, and plugin tools write into `ctx.data_dir`; `"workspace"`: normal file tools; `"none"`: all file tools hidden |

### `[[settings]]` (static inputs, set once)

`key` (letters, digits, `_`), `label`, `type`, `required`, `help`, `placeholder`,
`default`, `options` (select / multiselect), `min`, `max` (number).

Types: `text`, `textarea`, `number`, `boolean`, `select`, `multiselect`, `url`, `secret`.

A `secret` is stored in the keystore, shown only as "Saved ••••abcd", never returned by
the API, never put in the prompt and never exported. The agent is told only whether it is
set; plugin tools read it with `ctx.settings.get("KEY")`. Secrets cannot have defaults.

The agent's system prompt lists every non-secret setting and its value, and which
required ones are missing. Actions refuse to run until required settings are filled in.

### `[[records]]` (tables the agent fills)

| Key | |
|---|---|
| `name` | lowercase table name |
| `label` | display name |
| `fields` | list of fields, see below |
| `unique` | field names that identify a record; adding a record with the same values merges into the existing one |
| `title_field` | the column shown first (default: the first field) |
| `status_field` | a select field shown as the pipeline status (default: a select field named `status`) |

Field keys: `name`, `label`, `type` (`text`, `textarea`, `number`, `boolean`, `select`,
`url`, `email`, `date`), `required`, `options`, `default`, `min`, `max`, `help`,
`editable` (the user owns it: shown editable in the table and never overwritten by a
merge), `hidden` (not shown as a column). Every record also has `id`, `created`, `updated`.

Values are validated and coerced: numbers from text (`"4/5"` → 4), select values ignore
case/spaces (`"Call link sent"` → `call_link_sent`), `instagram.com/x` becomes
`https://instagram.com/x`, emails are checked. URL de-duplication ignores `www.`, the
scheme, a trailing slash and the query.

### `[[actions]]` (buttons with run-time inputs)

`id`, `label`, `description`, `icon`, `inputs` (same shape as settings, without `secret`),
`prompt` (required), `records` (the table it works on).

The prompt is filled in before it is sent:

| Placeholder | Becomes |
|---|---|
| `{key}` | the input's value (lists joined with `, `; empty optional inputs become `(not given)`) |
| `{settings.key}` | a non-secret setting |
| `{records.<table>.count}` | how many records the table has |

Anything else in braces is left exactly as written, so prompts may contain JSON examples.
Inputs are validated by type; bad or missing ones are reported per field.

Running an action posts it into the open agent chat (or a new one), where it streams like
any message. The chat shows a short card with the inputs; the full instructions are folded
under "Instructions sent".

### `[[exports]]`

| `format` | Output |
|---|---|
| `csv` | UTF-8 with a BOM (opens correctly in Excel), header = field names + `created`, `updated`; cells that look like formulas are defused |
| `json` | the table's records, or all tables when `records` is omitted |
| `markdown` | a markdown table of the non-hidden fields |
| `template` | your `template` file rendered with the template language below |

Keys: `id`, `label`, `description`, `format`, `records`, `template`, `filename`
(`{date}` allowed; default `<agent>-<export>-<date>.<ext>`).

#### Template language

It only substitutes values and never runs code. Nothing is escaped (output is text/markdown).

```
{{field}}  {{settings.key}}  {{agent.name}}  {{date}}  {{@number}}  {{@index}}
{{count leads}}                               number of records in a table
{{#each leads}} … {{/each}}                   once per record (fields available inside)
{{#each leads status=new,contacted}} … {{/each}}   only matching records
{{#if email}} … {{else}} … {{/if}}            non-empty test
```

Templates are checked when the agent is imported; an unclosed block is reported with its line.

## Tools in Python (`tools/*.py`)

```python
def register(ctx):
    @ctx.tool
    def find_contact(name: str, hint: str = "") -> dict:
        """Search the web for a person's public email/phone/website.

        Args:
            name: the person's full name
            hint: their company or city
        """
        key = ctx.settings.get("SERPER_API_KEY")        # secrets resolved
        results = ctx.call("web_search", query=f'"{name}" {hint}')
        ctx.log(f"looked up {name}")
        return {"results": results}
```

The function's signature and docstring become the tool schema the model sees (type hints
→ JSON types, `Args:` lines → parameter descriptions). The agent sees the short name.
A plugin tool with the same name as a Jarvis tool replaces it for this agent only.

`ctx` provides:

| | |
|---|---|
| `ctx.tool` | decorator; also `@ctx.tool(name="x", description="…")` |
| `ctx.settings` | read-only, always current settings; secrets resolved; `.get(key, default)` |
| `ctx.records(name)` | the table: `.add(items)`, `.update(id, fields)`, `.query(where=None, search="", limit=50)`, `.get(id)`, `.delete(id)`, `.all()`, `.count()` |
| `ctx.call(tool, **args)` | run a Jarvis tool (`web_search`, `fetch_page`, …) under the same approvals |
| `ctx.http` | a shared `httpx.Client` with timeouts and a polite User-Agent (blocked when `network = false`) |
| `ctx.data_dir` | `<mind>/agent_data/<id>/`, the agent's own folder |
| `ctx.log(msg)` | append to `<data_dir>/plugin.log` |
| `ctx.id`, `ctx.name` | the agent's id and name |

Rules:
- Each plugin gets its own module namespace, so two agents can both have `tools/search.py`.
  Files starting with `_` are helpers: not registered, but importable with
  `from . import _helpers`.
- **Code is only loaded after the user trusts it.** Trust covers the exact code: if any
  `.py` file changes (an upgrade, an edit), the agent asks again. Built-in agents are trusted.
- A file that fails to import, has no `register(ctx)`, or registers nothing is listed in
  the workspace with its error; the rest of the agent keeps working.
- Plugin tools run in-process with your permissions, like `tools/` in Jarvis itself.

### Tools every agent gets

- `records_add(items, table?)`, `records_update(id, fields, table?)`,
  `records_query(where?, search?, limit?, fields?, table?)`, `records_delete(id, table?)`.
  Their schemas are generated from your fields (names, types, select options), so the
  model sees the real columns. `records_add` reports `added / updated / skipped` and why.
  `where={"email": ""}` finds records without an email; a list value means "any of".
  The workspace table updates live while the agent works.
- `agent_skill(name)` when the plugin has `skills/*.md`.
- Every Jarvis tool, minus those hidden by `[permissions]`.

## Import, trust and export

Import from the Agents page:
- a `.zip` (drag it onto the dialog),
- a GitHub link: `https://github.com/owner/repo` (its default branch, `main` or `master`)
  or `https://github.com/owner/repo/tree/<branch>/<folder>` for an agent inside a repo,
- an https link to a `.zip`,
- a folder path on this computer.

Before anything is installed you see a review card: name, version, author, permissions,
what it contains, and **every Python file**. Choose **Install** (prompt-only agents),
**Trust & install**, or **Install without code** (trust later from the workspace).
If you already have that agent: **Upgrade** (keeps its data) or **Install as a copy**.

Safety checks on every import: at most 20 MB and 500 files; no absolute paths, no `..`,
no symbolic links; only the file types agents use are kept (others are skipped and listed);
`icon.svg` must be a plain SVG.

Export (⋯ menu in the workspace):
- **Export agent (.zip)**: the plugin folder only.
- **Export with records**: adds `data/records/*.json`. Importing such a zip restores the
  records into an agent that has none yet.
Neither ever contains secrets, settings or trust.

## HTTP API

```
GET    /api/agents                               list (+ broken folders with their errors)
POST   /api/agents                               new prompt-only agent {name, description, prompt}
GET    /api/agents/{id}                          details (manifest, README, tools, tool errors)
POST   /api/agents/import                        multipart "file", or {url} / {path} → review card
POST   /api/agents/{id}/install                  {staging, mode: new|upgrade|copy, trust}
DELETE /api/agents/import/{staging}              discard a review
POST   /api/agents/{id}/trust                    {trusted}
POST   /api/agents/{id}/duplicate
DELETE /api/agents/{id}?data=true
GET    /api/agents/{id}/export.zip?data=1
GET    /api/agents/{id}/settings                 values (no secrets), secret status, missing
PUT    /api/agents/{id}/settings                 {values, secrets: {KEY: value|null}, llm: {provider, model}}
GET    /api/agents/{id}/records/{table}?q=
PATCH  /api/agents/{id}/records/{table}/{rid}    {fields}
DELETE /api/agents/{id}/records/{table}/{rid}
POST   /api/agents/{id}/actions/{action}         {inputs, session_id?} → {session_id}
POST   /api/agents/{id}/actions/{action}/preview {inputs} → {prompt}
POST   /api/agents/{id}/chat                     {text, session_id?} → {session_id}
GET    /api/agents/{id}/exports/{export}         file download
```

Agent chats are normal sessions (`kind = "agent"`, `owner = <id>`):
`GET /api/sessions?kind=agent&owner=<id>`, and `/api/runtime/{session}/history|chat|cancel|retry`.
Errors are `{"error": "…"}`, plus `fields` (per-field problems) or `issues` (manifest problems).

From Python (other features use these):

```python
state.agents.list()
state.agents.get(id)                      # Plugin: manifest, path, trusted
state.agents.profile(id)                  # AgentProfile for Runtimes / spawn_agent / discussions
state.agents.render_action(id, action_id, inputs)
state.agents.run_action(id, action_id, inputs, session_id=None, runtimes=None)
```
