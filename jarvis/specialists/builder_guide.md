# Agent format guide (for the builder)

Each section below is one `builder_guide(topic)`. The draft tools take JSON; Jarvis writes agent.toml.

## overview

An agent is a folder: `agent.toml` (generated from your JSON), `prompt.md` (role, workflow, rules),
optional `README.md`, `skills/*.md` (playbooks the agent reads with agent_skill), `exports/*.md`
(export templates), `tools/*.py` (Python tools), `icon.svg`.

Pieces and the tool that sets each:
- name, description, icon, version, tags, author, the `agent` table, `permissions`: `draft_set`
- settings (values the user fills in once): `draft_upsert("settings", {...})`
- records (tables the agent fills and the user reviews): `draft_upsert("records", {...})`, `draft_upsert_field`
- actions (buttons with inputs): `draft_upsert("actions", {...})`
- exports (downloads): `draft_upsert("exports", {...})`
- files: `draft_write_file(path, content)`

Top-level keys: `name` (shown everywhere), `description` (one or two sentences), `icon`, `version`
("0.1.0"), `tags` (list), `author`. The id is fixed. The `agent` table: `greeting` (markdown shown in an
empty chat), `suggestions` (list of starter questions), `core_tools` (Jarvis tools the agent sees
directly, e.g. ["web_search", "fetch_page"]), `max_tool_rounds` (1-200), `provider`/`model` (leave out).
Icons: activity agents alert bell book brain briefcase calendar chart check clock cloud code filter globe
info key lock mail pen pencil phone play search send server shield sparkle target terminal user users wave
wrench zap (or write icon.svg).

```json
{"description": "Tracks books I want to read and suggests new ones.", "icon": "book",
 "agent": {"greeting": "What are you reading?", "suggestions": ["What should I read next?"],
           "core_tools": ["web_search", "fetch_page"]}}
```

Every agent automatically gets records_add / records_update / records_query / records_delete for its
tables, plus every Jarvis tool allowed by its permissions. Do not mention those in settings or tools.

## settings

Static values set once in the Settings panel. Keys: `key` (letters, digits, _), `label`, `type`,
`required`, `help`, `placeholder`, `default`, `options` (select / multiselect), `min`, `max` (number).
Types: text, textarea, number, boolean, select, multiselect, url, secret.
API keys, tokens and passwords MUST be `secret` (stored in the keystore, never shown to the model,
never exported, no default). Names like API_KEY, TOKEN, SECRET, PASSWORD must be secrets.
Python tools read them with `ctx.settings.get("KEY")`; in prompts use `{settings.key}` (not secrets).

```json
{"key": "tone", "label": "Tone", "type": "select", "options": ["friendly", "formal"], "default": "friendly"}
{"key": "BOOKS_API_KEY", "label": "Books API key", "type": "secret", "help": "From the provider's dashboard."}
{"key": "genres", "label": "Favourite genres", "type": "multiselect", "options": ["sci-fi", "history", "crime"]}
```

## records

A table the agent fills and the user can review and edit live. Keys: `name` (lowercase letters, digits,
_), `label`, `fields` (list), `unique` (field names that identify a record; duplicates merge),
`title_field` (first column), `status_field` (a select field shown as the pipeline status).
Field keys: `name` (lowercase, not id/created/updated), `label`, `type` (text, textarea, number, boolean,
select, url, email, date), `required`, `options` (select only, required there), `default`, `min`, `max`,
`help`, `editable` (the user owns it: shown editable, never overwritten by the agent's merges), `hidden`.
Use a `status` select with `editable: true` for pipelines. Numbers accept ranges via min/max. Do not add
`options` to non-select fields.

```json
{"name": "books", "label": "Books", "unique": ["title", "author"], "title_field": "title",
 "fields": [
   {"name": "title", "type": "text", "required": true},
   {"name": "author", "type": "text"},
   {"name": "status", "type": "select", "options": ["want", "reading", "done"], "default": "want", "editable": true},
   {"name": "rating", "type": "number", "min": 1, "max": 5, "editable": true, "help": "1-5, set after reading"},
   {"name": "notes", "type": "textarea", "editable": true}]}
```

Add one field later: `draft_upsert_field("books", {"name": "genre", "type": "text"})`.

## actions

A button with inputs asked when it runs. Keys: `id` (lowercase letters, digits, -), `label`,
`description` (shown under the button), `icon`, `inputs` (list, same shape as settings but no `secret`),
`prompt` (required, the instructions sent to the agent), `records` (the table it works on).
Placeholders in the prompt: `{input_key}`, `{settings.key}`, `{records.<table>.count}`. Other braces are
left as written (JSON examples are fine). An unknown placeholder is a problem: it must be an input key,
`input`, `settings.<existing key>` or `records.<existing table>...`.
Write the prompt as numbered steps: what to read first (records_query), what to do, which records to
save with records_add / records_update, and what to report at the end. At least 20 characters.

```json
{"id": "suggest", "label": "Suggest books", "icon": "sparkle", "records": "books",
 "description": "Suggests new books based on the ones I rated highly.",
 "inputs": [{"key": "count", "label": "How many", "type": "number", "default": 3, "min": 1, "max": 10},
            {"key": "mood", "label": "Mood (optional)", "type": "text", "placeholder": "something light"}],
 "prompt": "Suggest {count} new books I have not read. Mood: {mood}.\n1. records_query(where={\"status\": \"done\"}) and look at the highest ratings.\n2. Pick {count} books that match, not already in the table ({records.books.count} saved).\n3. Save each with records_add(status=\"want\").\n4. Reply with one line per book saying why."}
```

## exports

Downloads from the workspace. Keys: `id`, `label`, `description`, `format` (csv, json, markdown, template),
`records` (the table; needed for csv and markdown when there are several tables), `template` (path like
`exports/report.md` for format template), `filename` (`{date}` allowed).

```json
{"id": "csv", "label": "Books (CSV)", "format": "csv", "records": "books"}
{"id": "report", "label": "Reading report", "format": "template", "template": "exports/report.md"}
```

## templates

Template exports (`exports/<name>.md`, written with draft_write_file) only substitute values:
`{{field}}` `{{settings.key}}` `{{agent.name}}` `{{date}}` `{{@number}}` `{{@index}}` `{{count books}}`,
`{{#each books}} ... {{/each}}` (once per record, fields available inside),
`{{#each books status=want,reading}} ... {{/each}}` (only matching), `{{#if rating}} ... {{else}} ... {{/if}}`.
Every block must be closed. Secrets are not available.

```
# Reading report ({{date}})
{{count books}} books saved.
{{#each books status=done}}- {{@number}}. {{title}} by {{author}}{{#if rating}} ({{rating}}/5){{/if}}
{{/each}}
```

## tools

Only when Jarvis's own tools cannot do it. `tools/<name>.py` defines `register(ctx)` and decorates each
tool with `@ctx.tool`. The function signature (type hints) and docstring become the tool schema, so
write a clear docstring saying when to use it; `Args:` lines describe parameters. Rules:
- top-level `def register(ctx):` is required in every file not starting with `_`; `_helpers.py` files are
  importable with `from . import _helpers`.
- tool names: letters, digits, _; not `records_*` or `agent_skill`; not a Jarvis tool name; unique.
- imports: standard library or installed packages only (httpx is available; prefer `ctx.http`).
- network code (ctx.http, httpx, requests, urllib.request, socket) needs permissions.network = true;
  subprocess / os.system needs permissions.commands = true.
- never hard-code keys: add a `secret` setting and use `ctx.settings.get("NAME")`.
`ctx` has: `settings` (read-only, `.get(key, default)`), `http` (httpx.Client with timeouts),
`records(name)` (`.add(items)`, `.update(id, fields)`, `.query(where, search, limit)`, `.get(id)`,
`.delete(id)`, `.all()`, `.count()`), `call(tool, **args)` (e.g. web_search, fetch_page), `data_dir`, `log(msg)`.
Return small JSON-able dicts; on failure return `{"error": "..."}` instead of raising.

```python
def register(ctx):
    @ctx.tool
    def book_lookup(title: str, limit: int = 5) -> dict:
        """Look up books by title in the Books API. Use it to get the author and year of a book.

        Args:
            title: the book title or part of it
            limit: how many results to return (1-10)
        """
        key = ctx.settings.get("BOOKS_API_KEY")
        if not key:
            return {"error": "Set the Books API key in this agent's Settings first."}
        try:
            response = ctx.http.get("https://api.example.com/v1/books",
                                    params={"q": title, "limit": max(1, min(int(limit), 10))},
                                    headers={"Authorization": f"Bearer {key}"})
            response.raise_for_status()
        except Exception as error:  # network problem, bad key, rate limit
            ctx.log(f"book_lookup failed: {error}")
            return {"error": f"The Books API request failed: {error}"}
        return {"books": [{"title": b.get("title"), "author": b.get("author"), "year": b.get("year")}
                          for b in response.json().get("results", [])]}
```

Test it with `draft_test_tool("book_lookup", {"title": "Dune"})` (the user approves running the code;
pass `settings` for keys the user gave you).

## permissions

`draft_set({"permissions": {...}})`. Drafts start with everything off. Keys:
- `network` (default off here): turns on web_search / fetch_page and ctx.http. Needed for research or APIs.
- `commands` (off): run_command and friends, and subprocess in tools. Almost never needed.
- `files`: "data" (default; read-only file tools, tools write into ctx.data_dir), "workspace" (the agent may
  write files), "none".
The user sees these before installing. Turn on only what is needed and say why in the summary.

```json
{"permissions": {"network": true}}
```

## examples

Call `agent_example("lead-finder")` for a complete agent (settings incl. secrets, a records table with a
status pipeline, five actions, three exports, a skill, a Python tools file). `agent_example("lead-finder",
"agent.toml")` reads one file. A tiny finished agent as draft calls:

1. `draft_set({"description": "Plans dinners from what is in the fridge.", "icon": "book", "permissions": {"network": false}})`
2. `draft_upsert("records", {"name": "meals", "label": "Meals", "fields": [{"name": "day", "type": "text", "required": true}, {"name": "dish", "type": "text"}, {"name": "status", "type": "select", "options": ["planned", "cooked"], "default": "planned", "editable": true}]})`
3. `draft_upsert("actions", {"id": "plan", "label": "Plan the week", "records": "meals", "inputs": [{"key": "ingredients", "label": "What do you have?", "type": "textarea", "required": true}], "prompt": "Plan 5 dinners using: {ingredients}. Save each with records_add."})`
4. `draft_upsert("exports", {"id": "csv", "label": "Meal plan (CSV)", "format": "csv", "records": "meals"})`
5. `draft_write_file("prompt.md", "You are Recipe Planner. ...")` (role, steps, rules; at least 80 characters)
6. `draft_check()`

## checklist

Before saying the agent is ready:
- `draft_check` shows no problems.
- name and description are set; the icon is a known one.
- prompt.md says who the agent is, how it works step by step, and what it must never do.
- every action prompt is concrete (steps, which records to save, what to report) and uses only real placeholders.
- records tables have a `unique` key when duplicates would be a problem, and an editable `status` for pipelines.
- API keys are `secret` settings, required ones marked `required`; nothing is hard-coded in Python.
- permissions are the minimum; network is on only if the agent searches the web or calls an API.
- Python tools have docstrings, handle errors with `{"error": ...}`, and were tested if the user agreed.
- the final message lists the buttons, the settings to fill in, the permissions and why, and says to press
  **Review & install** (**Review & apply** when editing).
