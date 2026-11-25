// New / edit routine dialog: what to do, when to do it (picker, free text or cron) and notification options.
import { el } from "./dom.js";
import { Select } from "./combo.js";
import { icon } from "./icons.js";
import { localInput, when } from "./routine_time.js";

const DAY_CHIPS = [["Mon", 1], ["Tue", 2], ["Wed", 3], ["Thu", 4], ["Fri", 5], ["Sat", 6], ["Sun", 0]];
const PRESETS = [
  { value: "minutes", label: "Every N minutes" },
  { value: "hours", label: "Every N hours" },
  { value: "daily", label: "Every day at" },
  { value: "weekdays", label: "Weekdays at" },
  { value: "weekly", label: "Weekly on chosen days" },
  { value: "monthly", label: "Monthly on a day" },
  { value: "once", label: "Once at" },
  { value: "cron", label: "Custom cron" },
];
const CUSTOM_PROMPT = "";  // the "no action" choice of the agent action select

const pad = (n) => String(n).padStart(2, "0");

// ---- schedule <-> picker ----

export function pickerToSchedule(p) {
  const [hour, minute] = (p.time || "09:00").split(":").map(Number);
  const at = `${minute} ${hour}`;
  switch (p.preset) {
    case "minutes": return { kind: "interval", every_minutes: Number(p.n) || 0 };
    case "hours": return { kind: "interval", every_minutes: (Number(p.n) || 0) * 60 };
    case "daily": return { kind: "cron", cron: `${at} * * *` };
    case "weekdays": return { kind: "cron", cron: `${at} * * 1-5` };
    case "weekly": return { kind: "cron", cron: `${at} * * ${[...p.days].sort((a, b) => ((a + 6) % 7) - ((b + 6) % 7)).join(",")}` };
    case "monthly": return { kind: "cron", cron: `${at} ${Number(p.dom) || 1} * *` };
    case "once": return { kind: "once", at: p.at.length === 16 ? `${p.at}:00` : p.at };
    default: return { kind: "cron", cron: p.cron.trim() };
  }
}

export function scheduleToPicker(schedule, base) {
  const p = { ...base };
  if (!schedule) return p;
  if (schedule.kind === "interval") {
    const m = schedule.every_minutes;
    Object.assign(p, m % 60 === 0 ? { preset: "hours", n: m / 60 } : { preset: "minutes", n: m });
  } else if (schedule.kind === "once") {
    Object.assign(p, { preset: "once", at: schedule.at.slice(0, 16) });
  } else {
    const f = schedule.cron.split(/\s+/);
    const single = /^\d+$/;
    p.cron = schedule.cron;
    p.preset = "cron";
    if (f.length === 5 && single.test(f[0]) && single.test(f[1]) && f[3] === "*") {
      const time = `${pad(f[1])}:${pad(f[0])}`;
      if (f[2] === "*" && f[4] === "*") Object.assign(p, { preset: "daily", time });
      else if (f[2] === "*" && f[4] === "1-5") Object.assign(p, { preset: "weekdays", time });
      else if (f[2] === "*" && /^[0-7](,[0-7])*$/.test(f[4])) Object.assign(p, { preset: "weekly", time, days: new Set(f[4].split(",").map((d) => Number(d) % 7)) });
      else if (single.test(f[2]) && f[4] === "*") Object.assign(p, { preset: "monthly", time, dom: Number(f[2]) });
    }
  }
  return p;
}

function defaultPicker() {
  const soon = new Date(Date.now() + 3600e3);
  soon.setMinutes(0, 0, 0);
  return { preset: "daily", n: 30, time: "09:00", days: new Set([1]), dom: 1, at: localInput(soon), cron: "0 9 * * 1-5" };
}

// ---- small widgets ----

export function switchControl(checked, label, onchange) {
  const input = el("input", { type: "checkbox", role: "switch", "aria-label": label });
  input.checked = !!checked;
  input.addEventListener("change", () => onchange?.(input.checked));
  return { input, node: el("label", { class: "switch" }, input, el("span", { class: "switch-track" })) };
}

function field(label, control, help) {
  return el("div", { class: "form-field" }, el("label", { text: label }), control, help ? el("p", { class: "help", text: help }) : null);
}

function inputFor(spec, values) {
  const key = spec.key;
  const common = { "aria-label": spec.label || key };
  if (spec.type === "boolean") {
    const box = el("input", { type: "checkbox", ...common });
    box.checked = values[key] ?? !!spec.default;
    values[key] = box.checked;
    box.addEventListener("change", () => { values[key] = box.checked; });
    return el("label", { class: "check" }, box, el("span", { text: spec.label || key }));
  }
  let control;
  if (spec.type === "select") {
    const options = (spec.options || []).map((o) => ({ value: String(o.value ?? o), label: String(o.label ?? o.value ?? o) }));
    values[key] = values[key] ?? spec.default ?? options[0]?.value ?? "";
    control = new Select({ label: spec.label || key, options, value: values[key], onchange: (v) => { values[key] = v; } }).node;
  } else if (spec.type === "multiselect") {
    const chosen = new Set(values[key] || spec.default || []);
    values[key] = [...chosen];
    control = el("div", { class: "day-chips" }, ...(spec.options || []).map((o) => {
      const value = String(o.value ?? o);
      const chip = el("button", { type: "button", class: "chip-toggle", "aria-pressed": String(chosen.has(value)), text: String(o.label ?? value),
        onclick: () => {
          chosen.has(value) ? chosen.delete(value) : chosen.add(value);
          chip.setAttribute("aria-pressed", String(chosen.has(value)));
          values[key] = [...chosen];
        } });
      return chip;
    }));
  } else {
    const multi = spec.type === "textarea";
    control = el(multi ? "textarea" : "input", {
      ...(multi ? { rows: 3 } : { type: spec.type === "number" ? "number" : spec.type === "url" ? "url" : "text" }), ...common,
      value: values[key] ?? spec.default ?? "",
    });
    if (multi) control.value = values[key] ?? spec.default ?? "";
    const store = () => { values[key] = spec.type === "number" && control.value !== "" ? Number(control.value) : control.value; };
    control.addEventListener("input", store);
    store();
  }
  return field(`${spec.label || key}${spec.required ? " *" : ""}`, control, spec.help);
}

// ---- the dialog ----

/**
 * Opens the form. Resolves with the saved routine, or null when cancelled.
 * options: { api, routine?, agents: [], example? }  (example: a routine-shaped object used as a starting point)
 */
export function openRoutineForm({ api, routine = null, agents = [], example = null }) {
  return new Promise((resolve) => {
    const start = routine || example || {};
    const target = start.target || { kind: "prompt", prompt: "" };
    const state = {
      name: start.name || "",
      mode: target.kind === "prompt" ? "prompt" : "agent",
      prompt: target.prompt || "",
      agent: target.agent || "",
      action: target.kind === "agent_action" ? target.action : CUSTOM_PROMPT,
      inputs: { ...(target.inputs || {}) },
      picker: scheduleToPicker(start.schedule, defaultPicker()),
      text: "",
      notify: start.notify ?? true,
      onMissed: start.on_missed || "run_once",
      preview: null,   // {ok, schedule, description, runs, error}
      saving: false,
    };
    const agentDetail = {};

    const dialog = el("dialog", { class: "modal routine-modal", "aria-label": routine ? "Edit routine" : "New routine" });
    const finish = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.addEventListener("cancel", () => finish(null));

    // -- name
    const nameInput = el("input", { type: "text", "aria-label": "Name", placeholder: "Morning briefing", maxlength: "80", value: state.name });
    nameInput.addEventListener("input", () => { state.name = nameInput.value; validate(); });

    // -- what
    const modeBar = el("div", { class: "segmented", role: "group", "aria-label": "What to do" });
    const modeButtons = {};
    for (const [value, label] of [["prompt", "Ask Jarvis"], ["agent", "Run an agent"]]) {
      modeButtons[value] = el("button", { type: "button", text: label, onclick: () => setMode(value) });
      modeBar.append(modeButtons[value]);
    }
    const promptBox = el("textarea", { rows: 4, "aria-label": "What Jarvis should do", placeholder: "Check my notes for anything due today and give me a short briefing." });
    promptBox.value = state.prompt;
    promptBox.addEventListener("input", () => { state.prompt = promptBox.value; validate(); });
    const promptField = field("What should Jarvis do?", promptBox, "Write it like a message to Jarvis. It runs on its own, so be specific.");

    const agentSelect = new Select({ label: "Agent", placeholder: agents.length ? "Choose an agent" : "No agents installed",
      options: agents.map((a) => ({ value: a.id, label: a.name || a.id })), value: state.agent, onchange: (value) => chooseAgent(value, true) });
    const actionSelect = new Select({ label: "Action", placeholder: "Choose an action", options: [], value: state.action, onchange: (value) => { state.action = value; drawAction(); validate(); } });
    const actionInputs = el("div", { class: "action-inputs" });
    const agentFreePrompt = el("textarea", { rows: 3, "aria-label": "What the agent should do", placeholder: "Find 10 new leads that match my ideal customer." });
    agentFreePrompt.value = state.prompt;
    agentFreePrompt.addEventListener("input", () => { state.prompt = agentFreePrompt.value; validate(); });
    const agentBox = el("div", { class: "agent-box" },
      field("Agent", agentSelect.node), field("Action", actionSelect.node), actionInputs);

    function setMode(mode) {
      state.mode = mode;
      for (const [value, button] of Object.entries(modeButtons)) button.setAttribute("aria-pressed", String(value === mode));
      promptField.hidden = mode !== "prompt";
      agentBox.hidden = mode !== "agent";
      validate();
    }

    async function chooseAgent(id, reset) {
      state.agent = id;
      if (reset) { state.action = CUSTOM_PROMPT; state.inputs = {}; }
      if (id && !agentDetail[id]) {
        try { agentDetail[id] = await api(`/agents/${encodeURIComponent(id)}`); } catch { agentDetail[id] = {}; }
      }
      const actions = actionsOf(agentDetail[id]);
      actionSelect.setOptions([{ value: CUSTOM_PROMPT, label: "Ask the agent something (free text)" },
        ...actions.map((a) => ({ value: a.id, label: a.label || a.id, sub: a.description }))]);
      actionSelect.setValue(state.action);
      drawAction();
      validate();
    }

    const actionsOf = (detail) => [detail?.manifest?.actions, detail?.actions].find(Array.isArray) || [];

    function drawAction() {
      actionInputs.replaceChildren();
      if (!state.agent) return;
      if (state.action === CUSTOM_PROMPT) {
        actionInputs.append(field("What should the agent do?", agentFreePrompt));
        return;
      }
      const action = actionsOf(agentDetail[state.agent]).find((a) => a.id === state.action);
      if (action?.description) actionInputs.append(el("p", { class: "help", text: action.description }));
      for (const spec of action?.inputs || []) actionInputs.append(inputFor(spec, state.inputs));
      if (!(action?.inputs || []).length) actionInputs.append(el("p", { class: "help", text: "This action needs no inputs." }));
    }

    // -- when
    const presetSelect = new Select({ label: "Repeat", options: PRESETS, value: state.picker.preset, onchange: (value) => { state.picker.preset = value; pickerChanged(); } });
    const controls = el("div", { class: "when-controls" });
    const textInput = el("input", { type: "text", "aria-label": "Or type the schedule", placeholder: "every weekday at 9", autocomplete: "off", spellcheck: "false" });
    const preview = el("div", { class: "preview", "aria-live": "polite" });
    const whenBox = el("div", { class: "when-box" });
    let previewTimer = 0;
    let previewSeq = 0;

    function drawControls() {
      const p = state.picker;
      const time = el("input", { type: "time", "aria-label": "Time", value: p.time });
      time.addEventListener("input", () => { p.time = time.value || "09:00"; pickerChanged(); });
      const number = (label, unit, min, max, key) => {
        const input = el("input", { type: "number", min, max, "aria-label": label, value: String(p[key]) });
        input.addEventListener("input", () => { p[key] = input.value; pickerChanged(); });
        return el("div", { class: "inline-controls" }, el("span", { text: "every" }), input, el("span", { text: unit }));
      };
      const parts = {
        minutes: () => number("Minutes", "minutes", 1, 1440, "n"),
        hours: () => number("Hours", "hours", 1, 168, "n"),
        daily: () => el("div", { class: "inline-controls" }, el("span", { text: "at" }), time),
        weekdays: () => el("div", { class: "inline-controls" }, el("span", { text: "at" }), time),
        weekly: () => el("div", { class: "weekly-controls" },
          el("div", { class: "day-chips", role: "group", "aria-label": "Days" }, ...DAY_CHIPS.map(([label, value]) => {
            const chip = el("button", { type: "button", class: "chip-toggle", text: label, "aria-pressed": String(p.days.has(value)),
              onclick: () => { p.days.has(value) ? p.days.delete(value) : p.days.add(value); chip.setAttribute("aria-pressed", String(p.days.has(value))); pickerChanged(); } });
            return chip;
          })),
          el("div", { class: "inline-controls" }, el("span", { text: "at" }), time)),
        monthly: () => {
          const dom = el("input", { type: "number", min: "1", max: "31", "aria-label": "Day of the month", value: String(p.dom) });
          dom.addEventListener("input", () => { p.dom = dom.value; pickerChanged(); });
          return el("div", { class: "inline-controls" }, el("span", { text: "on day" }), dom, el("span", { text: "at" }), time);
        },
        once: () => {
          const at = el("input", { type: "datetime-local", "aria-label": "Date and time", value: p.at });
          at.addEventListener("input", () => { p.at = at.value; pickerChanged(); });
          return at;
        },
        cron: () => {
          const cron = el("input", { type: "text", "aria-label": "Cron expression", value: p.cron, placeholder: "0 9 * * 1-5", spellcheck: "false", class: "mono" });
          cron.addEventListener("input", () => { p.cron = cron.value; pickerChanged(); });
          return el("div", { class: "cron-controls" }, cron,
            el("p", { class: "help", text: "minute hour day-of-month month day-of-week, e.g. 0 9 * * 1-5 is weekdays at 09:00. Names like mon-fri work." }));
        },
      };
      controls.replaceChildren(parts[p.preset]());
    }

    function pickerChanged() {
      state.text = "";
      textInput.value = "";
      whenBox.classList.remove("typed");
      drawControls();
      queuePreview();
    }

    textInput.addEventListener("input", () => {
      state.text = textInput.value.trim();
      whenBox.classList.toggle("typed", !!state.text);
      queuePreview();
    });

    function queuePreview() {
      state.preview = null;
      validate();
      clearTimeout(previewTimer);
      previewTimer = setTimeout(runPreview, 220);
    }

    async function runPreview() {
      const seq = ++previewSeq;
      const body = state.text ? state.text : pickerToSchedule(state.picker);
      if (state.picker.preset === "weekly" && !state.text && !state.picker.days.size) {
        state.preview = { ok: false, error: "Pick at least one day." };
        paintPreview(); validate(); return;
      }
      let result;
      try { result = await api("/routines/preview", { method: "POST", body: { schedule: body } }); }
      catch (error) { result = { error: error.message }; }
      if (seq !== previewSeq) return;
      if (result.error) state.preview = { ok: false, error: result.error };
      else {
        state.preview = { ok: true, schedule: result.schedule, description: result.description, runs: result.next_runs };
        if (state.text) { state.picker = scheduleToPicker(result.schedule, state.picker); presetSelect.setValue(state.picker.preset); drawControls(); }
      }
      paintPreview();
      validate();
    }

    function paintPreview() {
      const p = state.preview;
      preview.className = `preview${p && !p.ok ? " bad" : ""}`;
      if (!p) { preview.replaceChildren(el("span", { class: "note", text: "Checking…" })); return; }
      if (!p.ok) { preview.replaceChildren(icon("alert", 15), el("span", { text: p.error })); return; }
      const runs = p.runs.length ? p.runs.map((r) => when(r)).join(" · ") : "never (that time has already passed)";
      preview.replaceChildren(
        el("div", { class: "preview-title" }, icon("clock", 15), el("strong", { text: p.description })),
        el("div", { class: "note", text: `Next runs: ${runs}` }));
    }

    whenBox.append(
      el("div", { class: "when-row" }, el("div", { class: "when-preset" }, presetSelect.node), controls),
      el("div", { class: "form-field typed-field" }, el("label", { text: "Or type it" }), textInput),
      preview);

    // -- options
    const notifySwitch = switchControl(state.notify, "Notify when finished", (v) => { state.notify = v; });
    const missedSelect = new Select({ label: "If Jarvis was off", value: state.onMissed, options: [
      { value: "run_once", label: "Run once when Jarvis is back" }, { value: "skip", label: "Skip missed runs" }],
    onchange: (v) => { state.onMissed = v; } });

    // -- footer
    const errorLine = el("p", { class: "form-error", role: "alert", hidden: true });
    const save = el("button", { type: "button", class: "primary", text: routine ? "Save changes" : "Create routine", onclick: submit });
    const cancel = el("button", { type: "button", text: "Cancel", onclick: () => finish(null) });

    function targetFromForm() {
      if (state.mode === "prompt") return { kind: "prompt", prompt: state.prompt.trim() };
      if (state.action === CUSTOM_PROMPT) return { kind: "agent_prompt", agent: state.agent, prompt: state.prompt.trim() };
      return { kind: "agent_action", agent: state.agent, action: state.action, inputs: state.inputs };
    }

    function missing() {
      if (!state.name.trim()) return "Give it a name.";
      if (state.mode === "prompt" && !state.prompt.trim()) return "Say what Jarvis should do.";
      if (state.mode === "agent") {
        if (!state.agent) return "Choose an agent.";
        if (state.action === CUSTOM_PROMPT && !state.prompt.trim()) return "Say what the agent should do.";
        const action = actionsOf(agentDetail[state.agent]).find((a) => a.id === state.action);
        const empty = (action?.inputs || []).find((s) => s.required && (state.inputs[s.key] === undefined || state.inputs[s.key] === "" || state.inputs[s.key] === null));
        if (empty) return `Fill in “${empty.label || empty.key}”.`;
      }
      if (!state.preview) return "Checking the schedule…";
      if (!state.preview.ok) return state.preview.error;
      return "";
    }

    function validate() {
      const problem = missing();
      save.disabled = state.saving || !!problem;
      save.title = problem;
    }

    async function submit() {
      if (save.disabled) return;
      state.saving = true;
      validate();
      errorLine.hidden = true;
      const body = { name: state.name.trim(), schedule: state.preview.schedule, target: targetFromForm(), notify: state.notify, on_missed: state.onMissed };
      try {
        const saved = routine
          ? await api(`/routines/${encodeURIComponent(routine.id)}`, { method: "PATCH", body })
          : await api("/routines", { method: "POST", body });
        finish(saved);
      } catch (error) {
        state.saving = false;
        errorLine.textContent = error.message;
        errorLine.hidden = false;
        validate();
      }
    }

    dialog.append(
      el("h3", { text: routine ? "Edit routine" : "New routine" }),
      el("div", { class: "routine-form" },
        field("Name", nameInput),
        el("section", { class: "form-section" }, el("h4", { text: "What" }), modeBar, promptField, agentBox),
        el("section", { class: "form-section" }, el("h4", { text: "When" }), whenBox),
        el("section", { class: "form-section" }, el("h4", { text: "Options" }),
          el("div", { class: "option-row" }, notifySwitch.node, el("div", {}, el("strong", { text: "Notify me when it finishes" }), el("p", { class: "help", text: "A toast and a badge in the sidebar, plus a desktop notification if you enable them." }))),
          field("If Jarvis was off at the scheduled time", missedSelect.node))),
      errorLine,
      el("div", { class: "modal-actions" }, cancel, save));
    document.body.append(dialog);
    dialog.showModal();

    setMode(state.mode);
    drawControls();
    if (state.agent) chooseAgent(state.agent, false);
    // editing an existing schedule: show its preview right away
    if (start.schedule) {
      state.preview = null;
      runPreview();
    } else queuePreview();
    nameInput.focus();
    validate();
  });
}
