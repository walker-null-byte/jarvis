// Create-discussion form (#/discussions/new): one scrolling form with inline validation.
import { api } from "./api.js";
import { ComboBox, Select } from "./combo.js";
import { el } from "./dom.js";
import { field, ico, partAvatar } from "./discussion_common.js";
import { icon } from "./icons.js";
import { providerOptions, shortName } from "./switcher.js";
import { pageHeader } from "./view_parts.js";

const SAME = { value: "", label: "Same as my current model", sub: "" };

export function mountForm(container, ctx, templateId) {
  let meta = null;
  let providers = [];
  let destroyed = false;
  container.append(pageHeader("New discussion", "Set the agenda, pick the participants, then press Start in the room."),
    el("p", { class: "note", text: "Loading…" }));

  Promise.all([api("/discussions/meta"), api("/providers")]).then(([m, p]) => {
    if (destroyed) return;
    meta = m;
    providers = p.providers;
    container.replaceChildren();
    build(container, ctx, meta, providers, templateId);
  }).catch((error) => {
    container.replaceChildren(pageHeader("New discussion", ""),
      el("div", { class: "error-banner", role: "alert" }, el("div", { class: "error-head" }, icon("alert", 16), el("strong", { text: "Could not open the form" })),
        el("p", { class: "error-text", text: error.message })));
  });
  return () => { destroyed = true; };
}

function build(container, ctx, meta, providers, templateId) {
  const nextKey = (() => { let n = 0; return () => `k${++n}`; })();
  const state = {
    title: "", goal: "", items: [{ key: nextKey(), text: "" }], outcome: "decision", mode: "round_robin", rounds: 2,
    participants: [], moderator: { provider: "", model: "" },
  };
  const providerById = new Map(providers.map((p) => [p.id, p]));
  const currentProvider = meta.current.provider;
  const modelCache = new Map(); // provider id -> {items, error}

  // ---------- participants ----------
  const usedColors = () => new Set(state.participants.map((p) => p.color));
  function newParticipant(preset = {}, extra = {}) {
    const color = extra.color || meta.colors.find((c) => !usedColors().has(c)) || meta.colors[state.participants.length % meta.colors.length];
    return { key: nextKey(), name: preset.name || "", role: preset.role || "", provider: "", model: "", tools: true, stance: "", color, agent: "", ...extra };
  }
  function addPreset(preset) {
    if (state.participants.length >= meta.limits.participants[1]) return;
    let name = preset.name;
    for (let n = 2; state.participants.some((p) => p.name.toLowerCase() === name.toLowerCase()); n++) name = `${preset.name} ${n}`;
    state.participants.push(newParticipant({ ...preset, name }));
    drawParticipants(true);
    validate();
  }

  // ---------- template ----------
  const tpl = meta.templates.find((t) => t.id === templateId);
  if (tpl) {
    state.title = tpl.title.replace(/^Debate: X vs Y$/, "Debate");
    state.goal = tpl.goal;
    state.items = tpl.items.map((text) => ({ key: nextKey(), text }));
    state.outcome = tpl.outcome;
    state.mode = tpl.mode;
    state.rounds = tpl.rounds;
    tpl.participants.forEach((name, i) => {
      const preset = meta.presets.find((p) => p.name === name) || { name, role: "" };
      state.participants.push(newParticipant(preset, { stance: (tpl.stances || [])[i] || "" }));
    });
  } else {
    ["Researcher", "Skeptic"].forEach((name) => state.participants.push(newParticipant(meta.presets.find((p) => p.name === name))));
  }

  // ---------- topic ----------
  const title = el("input", { type: "text", id: "d-title", value: state.title, placeholder: "Defaults to the goal", maxlength: 120, autocomplete: "off" });
  title.addEventListener("input", () => { state.title = title.value; });
  const goal = el("textarea", { id: "d-goal", rows: 3, maxlength: 2000, placeholder: "e.g. Decide whether we should launch the beta in March, and what has to be true first." });
  goal.value = state.goal;
  const goalField = field("Goal", goal, { required: true, id: "d-goal",
    help: "What should this discussion achieve? Every participant sees it, so make it specific." });
  goal.addEventListener("input", () => { state.goal = goal.value; goalField.setError(""); validate(); });
  goal.addEventListener("blur", () => { if (!goal.value.trim()) goalField.setError("The goal is required."); });
  const outcome = new Select({ label: "Expected outcome", options: meta.outcomes.map((o) => ({ value: o.id, label: o.label })), value: state.outcome,
    onchange: (v) => { state.outcome = v; } });

  // ---------- agenda ----------
  const itemList = el("ol", { class: "d-items", "aria-label": "Agenda items" });
  const itemsField = field("Agenda items", itemList, { required: true,
    help: "The group works through these in order. Each gets rounds of discussion, a summary, then the next." });
  function focusItem(key) { itemList.querySelector(`[data-key="${key}"] input`)?.focus(); }
  function moveItem(index, delta) {
    const to = index + delta;
    if (to < 0 || to >= state.items.length) return;
    const [item] = state.items.splice(index, 1);
    state.items.splice(to, 0, item);
    drawItems();
    focusItem(item.key);
  }
  function drawItems() {
    itemList.replaceChildren(...state.items.map((item, i) => {
      const input = el("input", { type: "text", value: item.text, maxlength: 300, autocomplete: "off", placeholder: i ? "Next topic" : "First topic to discuss", "aria-label": `Agenda item ${i + 1}` });
      input.addEventListener("input", () => { item.text = input.value; itemsField.setError(""); validate(); });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); addItem(i + 1); }
        else if (e.altKey && e.key === "ArrowUp") { e.preventDefault(); moveItem(i, -1); }
        else if (e.altKey && e.key === "ArrowDown") { e.preventDefault(); moveItem(i, 1); }
        else if (e.key === "ArrowUp" && i > 0) { e.preventDefault(); focusItem(state.items[i - 1].key); }
        else if (e.key === "ArrowDown" && i < state.items.length - 1) { e.preventDefault(); focusItem(state.items[i + 1].key); }
        else if (e.key === "Backspace" && !input.value && state.items.length > 1) { e.preventDefault(); removeItem(i, true); }
      });
      return el("li", { class: "d-item", dataset: { key: item.key } },
        el("span", { class: "d-item-n", text: String(i + 1) }), input,
        el("button", { type: "button", class: "icon-btn", "aria-label": `Move item ${i + 1} up`, title: "Move up (Alt+↑)", disabled: i === 0, onclick: () => moveItem(i, -1) }, ico("up", 15)),
        el("button", { type: "button", class: "icon-btn", "aria-label": `Move item ${i + 1} down`, title: "Move down (Alt+↓)", disabled: i === state.items.length - 1, onclick: () => moveItem(i, 1) }, icon("down", 15)),
        el("button", { type: "button", class: "icon-btn", "aria-label": `Remove item ${i + 1}`, title: "Remove", disabled: state.items.length === 1, onclick: () => removeItem(i) }, icon("x", 15)));
    }));
    validate();
  }
  function addItem(at = state.items.length) {
    if (state.items.length >= meta.limits.items) return;
    const item = { key: nextKey(), text: "" };
    state.items.splice(at, 0, item);
    drawItems();
    focusItem(item.key);
  }
  function removeItem(i, focusPrev = false) {
    const [gone] = state.items.splice(i, 1);
    drawItems();
    const target = state.items[Math.max(0, focusPrev ? i - 1 : i)];
    if (target) focusItem(target.key);
    return gone;
  }
  const addItemBtn = el("button", { type: "button", class: "small", onclick: () => addItem() }, icon("plus", 14), "Add item");

  // ---------- mode ----------
  const mode = new Select({ label: "Discussion mode", options: meta.modes.map((m) => ({ value: m.id, label: m.label, sub: m.help })), value: state.mode,
    onchange: (v) => { state.mode = v; syncMode(); drawParticipants(); validate(); } });
  const rounds = el("input", { type: "number", id: "d-rounds", min: 1, max: meta.limits.rounds, value: state.rounds, "aria-label": "Rounds per item" });
  rounds.addEventListener("input", () => { state.rounds = Number(rounds.value); validate(); });
  function syncMode() {}

  // ---------- model pickers ----------
  const providerSelect = (value, onchange, label) => new Select({ label, value, onchange,
    options: [{ ...SAME, sub: `${shortName(providerById.get(currentProvider) || { id: currentProvider })} · ${meta.current.model || "default model"}` },
      ...providerOptions(providers)] });

  async function loadModels(providerId, box, force = false) {
    const id = providerId || currentProvider;
    const provider = providerById.get(id);
    if (provider && !hasKey(provider)) { box.setState({ items: [], error: "Add an API key in Settings to list models. You can still type a model id.", loading: false }); return; }
    if (!force && modelCache.has(id)) { box.setState({ loading: false, ...modelCache.get(id) }); return; }
    box.setState({ loading: true, error: "" });
    try {
      const out = await api("/provider/test", { method: "POST", body: { provider: id } });
      const entry = { items: out.models || [], error: out.ok || (out.models || []).length ? "" : `Could not list models: ${out.message}` };
      modelCache.set(id, entry);
      box.setState({ loading: false, ...entry });
    } catch (error) { box.setState({ loading: false, items: [], error: error.message }); }
  }
  const hasKey = (p) => p.kind !== "api_key" || p.id === "custom"
    || ((p.saved || p.env_set) && (p.fields || []).every((f) => !f.required || f.set));
  const modelPlaceholder = (providerId) => (providerById.get(providerId || currentProvider)?.default_model) || "model id (blank = default)";

  function modelBox(label, target, providerOf) {
    const box = new ComboBox({ label, placeholder: modelPlaceholder(providerOf()), onfocus: () => loadModels(providerOf(), box),
      onchange: (v) => { target.model = v; } });
    box.value = target.model;
    return box;
  }

  // ---------- participant cards ----------
  const peopleList = el("div", { class: "d-people" });
  const peopleField = field("Participants", peopleList, { required: true });
  const presetRow = el("div", { class: "d-presets", role: "group", "aria-label": "Add a participant" });
  function addAgent(agent) {
    if (state.participants.length >= meta.limits.participants[1]) return;
    state.participants.push(newParticipant({ name: agent.name, role: agent.description || "" }, { agent: agent.id }));
    drawParticipants(true); validate();
  }
  function drawPresets() {
    const full = state.participants.length >= meta.limits.participants[1];
    presetRow.replaceChildren(
      ...meta.presets.map((p) => el("button", { type: "button", class: "chip small", disabled: full, title: p.role, onclick: () => addPreset(p) }, icon("plus", 13), p.name)),
      el("button", { type: "button", class: "chip small", disabled: full, onclick: () => addPreset({ name: "Participant", role: "" }) }, icon("plus", 13), "Blank"),
      ...meta.agents.map((a) => el("button", { type: "button", class: "chip small", disabled: full, title: a.description || "Installed agent", onclick: () => addAgent(a) }, icon("agents", 13), a.name)));
  }

  function participantCard(p, index) {
    const name = el("input", { type: "text", value: p.name, maxlength: 40, placeholder: "Name", "aria-label": `Participant ${index + 1} name`, autocomplete: "off" });
    const role = el("textarea", { rows: 3, maxlength: 3000, placeholder: "Role: how should this participant think and what should they contribute?", "aria-label": `${p.name || "Participant"} role` });
    role.value = p.role;
    const avatarHost = el("div", { class: "d-card-avatar" }, partAvatar(p, 34));
    const nameError = el("span", { class: "d-error", role: "alert", hidden: true });
    name.addEventListener("input", () => { p.name = name.value; avatarHost.replaceChildren(partAvatar(p, 34)); validate(); });
    role.addEventListener("input", () => { p.role = role.value; });
    const warn = el("p", { class: "d-warn", role: "status", hidden: true });
    const syncWarn = () => {
      const provider = providerById.get(p.provider);
      const bad = provider && !hasKey(provider);
      warn.hidden = !bad;
      warn.replaceChildren(...(bad ? [icon("alert", 14), `No API key for ${shortName(provider)}. Add one in Settings or this participant will fail.`] : []));
    };
    const model = modelBox("Model", p, () => p.provider);
    const provider = providerSelect(p.provider, (v) => {
      p.provider = v; p.model = ""; model.value = "";
      model.input.placeholder = modelPlaceholder(v);
      syncWarn();
    }, `${p.name || "Participant"} provider`);
    const tools = el("input", { type: "checkbox", id: `d-tools-${p.key}`, checked: p.tools });
    tools.addEventListener("change", () => { p.tools = tools.checked; });
    const stance = new Select({ label: "Stance", value: p.stance, onchange: (v) => { p.stance = v; validate(); },
      options: [{ value: "", label: "Auto", sub: "alternates for / against" }, { value: "for", label: "For" }, { value: "against", label: "Against" },
        { value: "judge", label: "Judge", sub: "sums up each item" }] });
    const colors = el("div", { class: "d-colors", role: "radiogroup", "aria-label": "Color" }, meta.colors.map((c) => {
      const dot = el("button", { type: "button", class: "d-color", role: "radio", "aria-checked": String(c === p.color), "aria-label": c, title: c,
        onclick: () => { p.color = c; colors.querySelectorAll(".d-color").forEach((n) => n.setAttribute("aria-checked", String(n === dot))); avatarHost.replaceChildren(partAvatar(p, 34)); } });
      dot.style.setProperty("--c", c);
      return dot;
    }));
    syncWarn();
    const node = el("section", { class: "d-person", dataset: { key: p.key }, "aria-label": p.name || `Participant ${index + 1}` },
      el("div", { class: "d-person-head" }, avatarHost, el("div", { class: "d-person-name" }, name, nameError), colors,
        el("button", { type: "button", class: "icon-btn", "aria-label": `Remove ${p.name || "participant"}`, title: "Remove",
          onclick: () => { state.participants = state.participants.filter((x) => x !== p); drawParticipants(); validate(); } }, icon("trash", 15))),
      p.agent ? el("p", { class: "note", text: `Uses the installed agent “${p.agent}” for its tools.` }) : null,
      role,
      el("div", { class: "d-person-row" },
        el("div", { class: "field" }, el("span", { text: "Provider" }), provider.node),
        el("div", { class: "field" }, el("span", { text: "Model" }), model.node)),
      el("div", { class: "d-person-row d-person-opts" },
        el("label", { class: "check-row", for: `d-tools-${p.key}` }, tools, el("span", { text: "Can use research tools (web search, read files)" })),
        el("div", { class: "field d-stance" }, el("span", { text: "Stance" }), stance.node)),
      warn);
    node.nameError = nameError;
    node.stanceBox = node.querySelector(".d-stance");
    return node;
  }
  function drawParticipants(focusLast = false) {
    peopleList.replaceChildren(...state.participants.map(participantCard));
    peopleList.querySelectorAll(".d-stance").forEach((n) => { n.hidden = state.mode !== "debate"; });
    drawPresets();
    if (focusLast) peopleList.lastElementChild?.querySelector("input[type=text]")?.focus();
  }

  // ---------- moderator ----------
  const modModel = modelBox("Moderator model", state.moderator, () => state.moderator.provider);
  const modProvider = providerSelect("", (v) => {
    state.moderator.provider = v; state.moderator.model = ""; modModel.value = "";
    modModel.input.placeholder = modelPlaceholder(v);
  }, "Moderator provider");

  // ---------- validation + submit ----------
  const reason = el("p", { class: "d-reason", role: "status", "aria-live": "polite" });
  const create = el("button", { type: "submit", class: "primary big", text: "Create discussion" });
  let attempted = false;

  function problems() {
    const out = [];
    if (!state.goal.trim()) out.push({ text: "Add a goal.", on: goalField });
    if (!state.items.some((i) => i.text.trim())) out.push({ text: "Add at least one agenda item.", on: itemsField });
    const names = state.participants.map((p) => p.name.trim().toLowerCase());
    if (state.participants.length < meta.limits.participants[0]) out.push({ text: `Add at least ${meta.limits.participants[0]} participants.`, on: peopleField });
    if (names.some((n) => !n)) out.push({ text: "Every participant needs a name.", on: peopleField });
    else if (new Set(names).size !== names.length) out.push({ text: "Participant names must be different.", on: peopleField });
    const r = Number(state.rounds);
    if (!(r >= 1 && r <= meta.limits.rounds)) out.push({ text: `Rounds per item: 1 to ${meta.limits.rounds}.`, on: null });
    if (state.mode === "debate") {
      const sides = state.participants.filter((p) => p.stance !== "judge");
      const forCount = sides.filter((p, i) => (p.stance || (i % 2 ? "against" : "for")) === "for").length;
      if (sides.length < 2 || !forCount || forCount === sides.length) out.push({ text: "A debate needs someone for and someone against.", on: peopleField });
    }
    return out;
  }
  function validate() {
    const list = problems();
    create.disabled = list.length > 0;
    reason.textContent = list.length ? list[0].text : `${state.participants.length} participants, ${state.items.filter((i) => i.text.trim()).length} agenda items. Ready.`;
    reason.classList.toggle("ok", !list.length);
    for (const f of [itemsField, peopleField]) f.setError(attempted ? list.find((p) => p.on === f)?.text : "");
    const seen = new Map();
    state.participants.forEach((p) => { const n = p.name.trim().toLowerCase(); seen.set(n, (seen.get(n) || 0) + 1); });
    peopleList.querySelectorAll(".d-person").forEach((card, i) => {
      const p = state.participants[i];
      const bad = p && p.name.trim() && seen.get(p.name.trim().toLowerCase()) > 1;
      card.nameError.hidden = !bad;
      card.nameError.textContent = bad ? "Another participant already has this name." : "";
    });
  }

  const form = el("form", { class: "d-form", novalidate: true, onsubmit: submit },
    section("Topic", "What is this discussion about?", field("Title", title, { id: "d-title", help: "Optional. Shown in the list." }), goalField,
      field("Expected outcome", outcome.node, { help: "Shapes how the minutes are written." })),
    section("Agenda", "", itemsField, el("div", { class: "d-item-actions" }, addItemBtn, el("span", { class: "note", text: "Enter adds an item · Alt+↑/↓ reorders" }))),
    section("How it runs", "", field("Mode", mode.node, {}),
      field("Rounds per item", rounds, { id: "d-rounds", help: "How many times each participant speaks on every agenda item (1 to 6)." })),
    section("Participants", "2 to 6. Each can use a different provider and model.", peopleField,
      el("div", { class: "d-add-row" }, el("span", { class: "note", text: "Add:" }), presetRow)),
    section("Moderator", "Writes the item summaries and the minutes, and picks speakers in moderated mode.",
      el("div", { class: "d-person-row" },
        el("div", { class: "field" }, el("span", { text: "Provider" }), modProvider.node),
        el("div", { class: "field" }, el("span", { text: "Model" }), modModel.node))),
    el("div", { class: "d-footer" }, reason,
      el("button", { type: "button", text: "Cancel", onclick: () => ctx.navigate("discussions") }), create));

  async function submit(event) {
    event.preventDefault();
    attempted = true;
    validate();
    const list = problems();
    if (list.length) {
      list[0].on?.scrollIntoView?.({ block: "center", behavior: "smooth" });
      if (list[0].on === goalField) { goalField.setError("The goal is required."); goal.focus(); }
      return;
    }
    create.disabled = true;
    create.textContent = "Creating…";
    try {
      const out = await api("/discussions", { method: "POST", body: {
        title: state.title, mode: state.mode, rounds_per_item: state.rounds,
        agenda: { goal: state.goal, outcome: state.outcome, items: state.items.filter((i) => i.text.trim()).map((i) => ({ title: i.text })) },
        participants: state.participants.map(({ name, role, provider, model, tools, stance, color, agent }) => ({ name, role, provider, model, tools, stance, color, agent })),
        moderator: state.moderator } });
      for (const w of out.warnings || []) ctx.toast(w, "error");
      ctx.navigate(`discussions/${out.discussion.id}`);
    } catch (error) {
      ctx.toast(error.message, "error");
      create.textContent = "Create discussion";
      validate();
    }
  }

  container.append(el("div", { class: "d-formwrap" }, pageHeader("New discussion", "Set the agenda, pick the participants, then press Start in the room."), form));
  ctx.setTitle("New discussion");
  drawItems();
  drawParticipants();
  validate();
  (tpl ? form.querySelector("#d-goal") : title).focus({ preventScroll: true });
}

function section(heading, sub, ...children) {
  return el("section", { class: "d-section" }, el("h3", { text: heading }), sub ? el("p", { class: "note", text: sub }) : null, ...children);
}
