# Jarvis — Product and Implementation Plan

Jarvis is a desktop-first, local-first personal agent inspired by the JARVIS
interface: conversational, voice-capable, persistent, inspectable, and able to
turn reliable procedures into reviewed tools.

The implementation should remain modular and easy to debug. The core agent must
stay small; UI, voice, memory, execution policy, and visual artifacts should be
separate services/modules connected by simple events and data structures.

## Product principles

- **Useful before theatrical.** The assistant should complete work and explain
  what happened. Visual polish supports that goal rather than competing with it.
- **Desktop-first.** The TUI is the first serious interface. A web UI comes
  after the desktop experience and should reuse the same agent/event layer.
- **Inspectable by default.** Conversations, tool calls, errors, generated
  artifacts, memory changes, and self-improvement proposals must be reviewable.
- **Safe action boundaries.** A model may propose actions; the runtime decides
  what is allowed, what needs confirmation, and what is sandboxed.
- **Persistent learning without silent self-modification.** Memory can be
  written during normal use. New tools and core changes go through tests,
  review, versioning, and rollback.
- **Graceful degradation.** Text mode must work without voice, visual artifacts
  must work without a detached-window backend, and a missing optional dependency
  must not prevent the core assistant from starting.

## Current repository status (audited 2026-08-02)

The current repository is a working prototype. `python -m pytest -q` passes all
32 regression tests, and `python -m compileall -q jarvis tests` passes. The
status below is based on the code, not only the README.

### Implemented baseline

- ✅ OpenAI-compatible LLM client with blocking/streaming responses, streamed
  tool-call accumulation, configurable timeouts, bounded exponential retries,
  and normalized request failures (`llm.py`).
- ✅ Agent loop with a configurable tool-round budget, tool-result truncation,
  rolling history, and LLM-generated history summaries (`agent.py`).
- ✅ Text CLI and a basic Textual TUI (`__main__.py`, `ui.py`).
- ✅ Episodic daily Markdown logs (`memory.py`).
- ◐ File-backed semantic facts with remember/recall/forget tools, strength,
  recall reinforcement, an index, decay, archive support, atomic writes, safe
  IDs, source/confidence/category/validity provenance, review decisions,
  expiration, and never-remember rules.
- ✅ LLM-assisted consolidation that extracts facts, detects contradictions,
  drafts skills, decays facts, and attempts git commits (`consolidate.py`).
- ✅ Built-in command/file/directory/cron tools and optional turbomcp default
  tools (`tools_core.py`).
- ✅ Learned-tool directory loading, polling hot reload, OpenAI schemas, and a
  lazy MCP stdio server (`registry.py`, `mcp_server.py`).
- ✅ Skill-to-Python-tool generation scaffold with import/registration smoke
  testing (`codify.py`).
- ✅ Piper/Kokoro TTS and faster-whisper recording scaffolds, plus a circular
  terminal spectrum driven by measured TTS frame amplitude (`voice.py`, `listen.py`,
  `ui.py`).
- ✅ Headless job runner and a supervisor scaffold (`run_job.py`,
  `supervisor.py`).

### Not yet complete or production-ready

- ✅ The TUI now has the planned two-pane assistant workspace: a persistent
  awareness rail with a circular pseudo-3D waveform/orb, status text, sessions,
  collapsible activity/diagnostics views, and artifact previews.
- ✅ TUI streaming now updates one assistant card per turn, with tool activity
  separated into the collapsible Activity panel.
- ✅ Voice disabled mode is a no-op, the agent reuses one voice instance, and the
  waveform callback is attached to the instance that speaks. Model loading and
  playback portability still need more testing across audio devices.
- ✅ STT now defines compute type, uses the actual downloaded Whisper path, and
  loads/transcribes from UI workers. Device-specific testing remains.
- ✅ Supervisor crash logging imports are fixed and interactive TUI supervision
  preserves the terminal. Restart policy and configuration-error handling still
  need hardening.
- ◐ Scheduling now has an explicit private-file fallback and an opt-in system
  crontab backend. Reports distinguish recorded-only, installed, unavailable,
  and not-installed states; job names/schedules are validated, entries are
  deduplicated, and run status is persisted. Timers/reminders and duration/
  next-run metadata remain.
- ☐ Consolidation is not automatically run on application exit, personality/user
  files are not updated, and it does not automatically promote a drafted skill.
  Contradiction resolution is now idempotent, archives superseded facts, and
  reports only facts changed in the current cycle.
- ☐ Skill parsing now accepts both bullet and numbered dream-cycle formats and
  comma-separated tools. Generated tools are still only import/registration-
  tested, can call a broad tool set, and need promotion approval/sandboxing.
- ◐ Registry and direct built-in shell/filesystem tools now use a guarded
  capability policy with blocked destructive patterns, allowed roots, approval
  prompts, cancellation, timeout limits, redaction, and a JSONL audit trail.
  Network controls, richer dry-run behavior, and learned-tool capability
  declarations remain.
- ✅ Artifact storage/tools and inline UI cards now exist for images, charts,
  tables, JSON, Markdown, and text. A controlled optional Tk detached surface
  can open persisted artifacts; richer renderers and lifecycle updates remain
  planned.
- ◐ Sessions now have durable JSON metadata/message streams, UI resume/search,
  Markdown/JSON export, new-session, and retry-last-turn actions. Import,
  richer session filtering, and structured episode migration are still planned.
- ◐ Configuration, dependency lists, README, and runtime defaults are largely
  aligned. Environment overrides, complete voice defaults, the Python 3.10
  TOML fallback, provider presets, and shared runtime slash commands are now
  implemented; packaging and optional clipboard support still need a final
  audit.
- ☐ Test coverage still needs contract tests for the registry reload, voice/STT,
  UI streaming, packaging, and deeper safety policy paths. Runtime, LLM,
  session, artifact, config, jobs, supervisor, policy, cancellation, redaction,
  schema-validation, audit, codification, scheduler, memory-control, slash
  commands, slash-command discovery, and UI regressions now have focused tests
  (34 tests total).

### Implementation progress in this pass

- ✅ Added `events.py`, `sessions.py`, and `artifacts.py` as small runtime
  services. The agent emits turn/tool/artifact/state events without coupling to
  Textual.
- ✅ Replaced the flat TUI with a left awareness rail, animated circular
  pseudo-3D orb, status text, collapsible Conversations/Activity/Diagnostics,
  proper mutable streaming messages, inline artifact cards, new-session, stop,
  and push-to-talk worker paths.
- ✅ Added environment-variable configuration overrides and complete voice/STT
  defaults, plus dependency metadata for Python 3.10 and audio/STT packages.
- ✅ Added a narrow `SurfaceManager`/`surface_window.py` path so the model can
  request a generated artifact in a separate Python window without receiving a
  general-purpose window or code-execution tool.
- ✅ Added `policy.py` and wired guarded command, file, and directory tools into
  the registry. Added an explicit scheduler report so private-file jobs are
  shown as recorded-only rather than falsely reported as installed.
- ✅ Added an explicit scheduler backend with opt-in system crontab support,
  unavailable-state reporting, per-job scheduler checks, a `cron_status` tool,
  and atomic JSON run-state persistence for running/succeeded/failed jobs.
- ✅ Repaired contradiction resolution and dream-skill parsing, added recoverable
  fact archiving, idempotence, safe skill-name validation, and scoped codify git
  staging.
- ✅ Added approval requests with TUI Allow/Deny controls and CLI prompts,
  cancellable foreground shell processes, turn cancellation, and headless-safe
  denial of risky actions.
- ✅ Added best-effort secret redaction for tool results, arguments, sessions,
  episodes, approval previews, and generated-tool static validation against
  common import/call escape hatches.
- ✅ Added configurable LLM timeouts/retries with backoff, no-retry handling for
  partially streamed responses, lightweight tool argument schema validation,
  and redacted structured JSONL audit records for sessions, turns, and tools.
- ✅ Added session search, Markdown/JSON export, durable session refresh in the
  left rail, and retry-last-turn as a new auditable turn.
- ✅ Added atomic, path-safe fact persistence with source episode/event,
  confidence, category, observed/validity dates, correction links, provenance,
  and recall responses that expose those fields.
- ✅ Added pending-memory review decisions, expiry-aware recall, and persistent
  never-remember/allow-remember privacy rules enforced by both direct memory
  tools and consolidation.
- ✅ Added a compact collapsible Textual Memory review panel with provenance
  previews and Approve/Reject/Expire actions that refresh after memory changes.
- ✅ Added independent left-rail scrolling, layered panel/message styling,
  stronger borders and spacing, and a larger circular point-cloud waveform
  with ambient particles, a deforming outer shell, bright inner core, rotation,
  and smoothed RMS/peak response from actual TTS audio frames.
- ✅ Added shared `/model`, `/provider`, `/settings`, `/status`, and `/help`
  commands for both the Textual composer and plain CLI. Model/provider/voice
  changes apply immediately, remain session-local by default, and can be
  explicitly persisted as non-secret runtime overrides.
- ✅ Added an inline slash-command palette to the Textual composer. Typing `/`
  shows filtered commands, `/provider` and `/settings` show subcommands, and
  arrows, Tab, Enter, mouse selection, and Escape provide keyboard-friendly
  discovery without affecting ordinary chat input.
- ✅ Updated README discovery copy with accurate Hermes-alternative positioning
  and documented integrations for OpenAI-compatible LM Studio, Ollama,
  llama.cpp, OpenAI, and Google Gemini endpoints.

## Immediate correctness and quality backlog

These items come before the new visual work because they affect trust in every
later feature.

### P0 — fix before feature implementation

- ✅ Make voice genuinely optional: enforce `voice.enabled`, provide complete
  defaults, cache one voice instance, and prevent model download/initialization
  during text-only startup.
- ✅ Fix STT model resolution and `compute_type`; move model loading,
  transcription, and audio-device failures to background workers with visible
  status and cancellation.
- ✅ Fix the TUI streaming model so one assistant message is updated in place,
  tool-call planning is separated from user-visible reply text, and the final
  reply is not duplicated.
- ✅ Fix supervisor imports and separate interactive console execution from
  captured crash diagnostics. Preserve the terminal when supervising a TUI.
- ✅ Replace private-file-only cron behavior with an explicit scheduler adapter
  that reports whether a job is installed, enabled, running, succeeded, or
  failed. Validate job names and schedules; the private backend remains an
  honest recorded-only fallback.
- ◐ Add structured error handling around LLM connection failures, malformed tool
  calls, missing optional dependencies, audio failures, and invalid generated
  tool output. LLM failures and tool-argument validation are now normalized and
  visible; optional-dependency and UI-worker boundaries still need coverage.

### P1 — trust, safety, and operational quality

- ◐ Extend the capability policy to shell, files, network, cron, and
  window/artifact actions. Shell/filesystem approvals, timeouts, cancellation,
  redaction, and an audit record now exist; network/cron/window coverage and
  richer dry runs remain.
- ◐ Add cancellation and progress to LLM turns and long-running tools. Foreground
  shell tools are now cancellable and bounded; background process IDs and rich
  progress events remain.
- ◐ Validate all tool arguments and generated JSON against schemas. Built-in
  object schemas now enforce required fields, types, enums, and unknown-field
  rejection; nested JSON-schema coverage and comprehensive secret detection
  remain.
- ◐ Make memory writes atomic and path-safe; add source episode, confidence,
  category, temporal validity, and correction/provenance metadata. Core fact
  writes, recall metadata, review decisions, expiration, and never-remember
  controls now exist; richer UI review and change-set previews remain.
- ✅ Make contradiction handling deterministic: preserve both sources, select or
  merge explicitly, retire superseded facts, and make the operation idempotent.
- Make consolidation lockable, resumable, and honest about what changed. Run it
  on graceful shutdown only when configured, and via a real scheduled job when
  configured.
- Make codification parse the actual skill format, validate names/code, test in
  an isolated temporary environment, and commit only the intended files. A
  generated tool must not be promoted just because it imports.

### P2 — quality of life

- ◐ Add configurable request timeouts, retries with backoff, model capability
  detection, token/latency accounting, and a clear offline/unavailable state.
  Timeouts/retries are implemented; capability discovery and accounting remain.
- ◐ Add structured JSON event logs alongside human-readable episode views. The
  append-only redacted audit JSONL trail is implemented; richer event replay
  fields and export remain.
- ◐ Add session export/import, search, copy, keyboard navigation, interrupt,
  retry-last-turn, new-session, and compact/accessible display options. Search,
  export, retry, interrupt, and new-session are now available; import, copy,
  and accessibility/compact-mode polish remain.
- Align `PLAN.md`, README, configuration defaults, examples, requirements, and
  package metadata after each milestone.

## Target desktop UI

The next UI should be a calm, two-pane workspace. The left partition is a
control/awareness rail; the right side is the primary conversation. The default
view should remain quiet and uncluttered.

```text
┌──────────────────────── Jarvis ────────────────────────────────────────────┐
│ ┌────────────── Awareness rail ──────────────┐ ┌──────── Chat workspace ───┐ │
│ │              animated circular orb         │ │ conversation messages     │ │
│ │          3D/radial audio waveform          │ │ markdown + code           │ │
│ │                                             │ │ inline artifact cards     │ │
│ │  Ready · Listening · Thinking · Speaking   │ │ collapsed tool activity   │ │
│ │  model · voice · connection                 │ │                           │ │
│ │                                             │ │                           │ │
│ │  ▸ Conversations                           │ │                           │ │
│ │  ▸ Activity / tool calls                   │ │                           │ │
│ │  ▸ Diagnostics                              │ │                           │ │
│ │                                             │ │                           │ │
│ │                                             │ │ composer + mic/stop/send  │ │
│ └─────────────────────────────────────────────┘ └───────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Awareness rail

- Fixed but responsive width, roughly 280–320 terminal pixels where possible.
- Top: a circular pseudo-3D orb rendered from Unicode/rich primitives first.
  Use concentric rings, radial amplitude bands, depth shading, and a subtle
  idle “breathing” animation. Listening, thinking, speaking, tool execution,
  and error states each get a distinct motion/color treatment.
- Below the orb: only high-value status—current state, model/connection,
  voice on/off, and a one-line current action. Do not put raw logs here.
- Collapsible sections below status:
  - **Conversations:** recent sessions, new session, resume, rename, search.
  - **Activity:** current turn, tool calls, progress, generated artifacts;
    tool arguments/results collapsed by default.
  - **Diagnostics:** errors, timings, model endpoint, memory/consolidation
    notices. Hidden unless opened.
- Memory should be inspectable from Activity/Diagnostics but not occupy a
  permanent panel in the default view.

### Main chat workspace

- Render user and Jarvis messages as distinct blocks with Markdown/code support,
  copy actions, timestamps, and a clear in-progress state.
- Show tool calls as compact expandable rows; never flood the conversation with
  repeated streaming fragments.
- Show images/charts/tables as inline cards with “expand/open in window,” save,
  and copy-path actions.
- Keep the composer at the bottom with send, stop/cancel, microphone, and
  attachment/artifact affordances. `Ctrl+Space` remains push-to-talk; Escape
  cancels the active turn or speech.
- Provide a compact mode for small terminals and ensure the assistant remains
  usable without color, audio, clipboard, or Unicode support.

### UI event contract

The UI should consume events rather than inspect agent internals. Define a small
event stream such as:

- `session_started`, `user_message`, `assistant_delta`, `assistant_message`
- `tool_started`, `tool_progress`, `tool_finished`
- `state_changed`, `audio_amplitude`, `artifact_created`, `artifact_updated`
- `memory_changed`, `error`, `session_finished`

This event stream will later power the web UI without duplicating the agent loop.

## Artifact and window system

Jarvis needs a controlled way to show work it produces. The model should not
open arbitrary OS windows through shell commands. It should request a validated
artifact through a small runtime-owned surface manager.

### First implementation

- Add an `artifacts/` store under the configured mind/workspace directory with
  content plus metadata: ID, session, title, type, created/updated time, source
  tool, and lifecycle state.
- Add runtime tools with narrow schemas:
  - `show_artifact(type, title, content/path/data, display_mode)`
  - `update_artifact(id, patch)`
  - `list_artifacts(session_id)`
  - `close_artifact(id)`
- Support these types first: Markdown/text, image path, table, JSON data, and
  chart specification. Prefer deterministic rendering from data over model-
  authored HTML.
- Display artifacts inline in the chat and in an expandable right-side/detail
  panel. This is the reliable baseline even when no detached-window backend is
  installed.

### Detached windows

- Add an optional native Python backend behind the same surface manager. It may
  use a lightweight desktop toolkit for images, charts, and documents, while the
  TUI remains the primary controller.
- A detached window must have a stable artifact ID, close/update events, a
  maximum-window policy, and a fallback to the in-app artifact panel.
- For HTML-rich artifacts, serve only generated local content through a
  restricted local viewer. Sanitize HTML, block arbitrary navigation by
  default, and never execute model-provided Python/JavaScript as a side effect
  without explicit user approval.
- Add a first-class graph path: analysis tool → structured data → chart spec →
  rendered image/SVG → inline card or detached chart window.

## Memory and learning architecture

Keep the current file-backed design, but make the records more trustworthy and
useful:

- Episodic records become structured session/turn/tool events while retaining a
  readable Markdown export.
- Facts gain category, confidence, source event IDs, created/observed/valid
  dates, sensitivity, and superseded-by links.
- Retrieval remains keyword-based initially; add the optional semantic index
  only after measuring whether it improves recall. Recalling a fact should
  update its index and provenance atomically.
- ◐ Add explicit memory review: show what will be remembered, allow correction,
  deletion, expiration, and “never remember this” rules. Tool-level review,
  expiry, privacy controls, and the compact UI surface now exist; change-set
  previews and richer correction workflows remain.
- Consolidation should produce a reviewable change set. Automatic changes can be
  enabled for low-risk preferences; sensitive or contradictory facts should
  request approval.
- Personality and user profile updates must be small, diffable, and backed by
  evidence rather than silently rewritten by an unconstrained prompt.

## Self-improvement and tool growth

The intended pipeline is:

```text
episodes → candidate lesson/skill → reviewable proposal → isolated test
         → approved tool version → registry reload → observed outcomes
         → promotion, revision, or rollback
```

- Keep core code read-only to the agent. Mind files, artifact files, and learned
  tools are separate writable domains.
- Store proposal metadata: source episodes, generated code, required
  capabilities, tests, reviewer decision, version, and observed success rate.
- Smoke tests must include schema/registration, safe representative execution,
  timeout behavior, and failure handling. They must not contact the user's real
  filesystem or execute arbitrary commands during validation.
- Learned tools should declare capabilities and use the same policy/approval
  layer as built-in tools.
- Git commits must be scoped to the intended files; show a diff and provide
  rollback before promotion.

## Voice and interaction

- Keep text as the reliable baseline. TTS/STT are optional capabilities reported
  in the UI state.
- Use a structured response envelope when supported: visible text plus an
  optional short `speech` summary. Retain a small `<say>...</say>` compatibility
  parser for models that can only emit text, and never speak code or raw tool
  output.
- Cache TTS/STT engines, load models lazily with progress, expose device and
  language configuration, and provide stop/cancel controls.
- ◐ Feed both input and output amplitude into the orb. TTS output amplitude now
  drives the radial bars and speaking/listening states are visually distinct;
  microphone/input amplitude remains to be connected.
- Add an audio queue so a new turn can interrupt or replace speech cleanly.

## Automation and resilience

- ✅ Use a real scheduler adapter with a portable fallback (system cron plus the
  private-file recorded-only backend) and a status store. Do not silently claim
  a job is installed when it only exists in a private text file.
- Add reminders/timers as first-class short-lived jobs instead of forcing every
  timing request into cron.
- ◐ Persist job runs, output, and status. Duration and next-run computation remain.
- Add graceful shutdown: finish or cancel active work, flush events, optionally
  consolidate, stop watchers, and close audio/windows.
- Supervisor restarts should use bounded backoff, preserve interactive terminals,
  log the real crash, and avoid restart loops for configuration or dependency
  errors.

## Web UI (last)

Do not begin the web UI until the desktop UI, session store, event contract,
artifact system, and permission policy are stable.

The web phase will add a local service layer with:

- HTTP endpoints for sessions, messages, artifacts, jobs, memory review, and
  health/configuration status.
- WebSocket/SSE streaming over the same event contract used by Textual.
- Local-only binding by default, explicit authentication if exposed beyond the
  machine, and no direct browser access to arbitrary filesystem paths.
- A frontend that mirrors the desktop information architecture without creating
  a second agent implementation.

## Phased execution order

### Phase 0 — Stabilize the working prototype

- Complete the P0/P1 correctness, safety, configuration, dependency, and
  documentation backlog above.
- Add contract tests for LLM responses, agent turns, malformed tool calls,
  registry reload, jobs, supervisor logging, voice-disabled startup, and STT
  initialization.
- Acceptance: text CLI starts with voice disabled, a full fake-LLM turn is
  deterministic, no UI streaming duplication occurs, and all side-effecting
  tools pass the policy layer.

### Phase 1 — Desktop UI shell

- Introduce the event contract and session-aware UI state.
- Replace the flat layout with the awareness rail + main chat workspace.
- Implement the circular pseudo-3D orb, status text, collapsible Conversations,
  Activity, and Diagnostics sections.
- Add proper streaming message updates, Markdown/code rendering, stop/cancel,
  copy/search/new-session actions, and compact mode.
- Acceptance: a user can run a text-only session comfortably, inspect a tool
  call without clutter, resume a session, and understand whether Jarvis is
  idle/listening/thinking/speaking/working/error.

### Phase 2 — Sessions, events, and observability

- Add a durable session store and structured event log with Markdown export.
- Add turn IDs, tool-run IDs, latency/token/status fields, error boundaries,
  retry-last-turn, and replay-friendly history.
- Acceptance: the UI can list/resume/search sessions and Activity can reconstruct
  one turn without reading ad-hoc text output.

### Phase 3 — Artifacts and controlled windows

- Implement artifact storage, inline cards, chart/table/image renderers, and
  `show/update/list/close_artifact` tools.
- Add the optional detached native window backend with in-app fallback and
  lifecycle events.
- Acceptance: Jarvis can analyze data, produce a chart, show it inline, open it
  in a separate window, update it, and close it without arbitrary shell access.

### Phase 4 — Safe execution and automation

- Add capability policy, confirmations, scoped filesystem access, cancellable
  processes, background jobs, reminders/timers, and reliable scheduler status.
- Acceptance: destructive actions are previewable/confirmable, long work is
  cancellable, and a scheduled run is observable from creation through result.

### Phase 5 — Memory and reviewed self-improvement

- ◐ Upgrade provenance/review/contradiction handling, then repair the skill
  proposal → test → promotion pipeline and hot-reload lifecycle.
- Acceptance: a repeated successful procedure becomes a reviewable, tested,
  versioned tool; a failed or superseded tool can be disabled and rolled back.

### Phase 6 — Voice polish

- Finish cached/lazy TTS and STT, structured speech summaries, input/output
  waveform data, device handling, queueing, interruption, and model management.
- Acceptance: voice can be turned off without side effects; missing audio
  dependencies leave text usable; speech and listening state are reflected in
  the orb.

### Phase 7 — Local web UI

- Expose the stabilized event/session/artifact services over a secured local
  API and build the browser client.
- Acceptance: desktop and web clients can observe the same session and artifact
  state without duplicating agent behavior or weakening local safety policy.

## Definition of done for each milestone

- Unit tests cover new pure logic and failure paths.
- Integration tests use a fake LLM and fake side-effect adapters; no test
  depends on a live model, microphone, OS scheduler, or network.
- Side effects have explicit policy tests, logs, and rollback behavior.
- The README, configuration example, plan, and CLI help describe the actual
  behavior.
- The feature remains usable in text-only, offline, and dependency-light modes.

## Decisions for the next implementation turn

- Retain Textual as the first desktop shell and build the orb with a portable
  Unicode/Rich renderer before considering a GPU/native visualizer.
- Keep the main chat on the right of a persistent but collapsible left rail.
- Treat artifacts as first-class runtime objects; in-app display is mandatory,
  detached windows are optional but supported through the same API.
- Keep the web UI explicitly last.
- Resolve the remaining toolkit choice for detached windows during Phase 3 by
  selecting the smallest backend that works on the target Linux desktop and has
  a reliable in-app fallback.
