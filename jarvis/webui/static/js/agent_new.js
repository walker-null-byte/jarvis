// "New agent" dialog: three ways in. Make with AI (default) starts a builder draft,
// Quick is the plain name/description/role form, Copy duplicates an existing agent.
import { Select } from "./combo.js";
import { el } from "./dom.js";
import { icon } from "./icons.js";
import { aicon } from "./agent_icons.js";
import { formField, request, showErrors } from "./agent_forms.js";
import { modelPicker } from "./agent_model_pick.js";

export function openNewAgent(ctx, { tab = "ai" } = {}) {
  const { toast, navigate } = ctx;
  const opener = document.activeElement;
  const dialog = el("dialog", { class: "modal agent-dialog agent-new", "aria-label": "New agent" });
  const close = () => { dialog.close(); dialog.remove(); opener?.focus?.(); };
  const panels = { ai: aiPanel(), quick: quickPanel(), copy: copyPanel() };
  const labels = { ai: ["Make with AI", "sparkle"], quick: ["Quick", "pen"], copy: ["Copy an agent", "copy"] };
  const tabBar = el("div", { class: "seg", role: "tablist", "aria-label": "How to make the agent" });
  const tabs = {};
  const panelHost = el("div", { class: "agent-new-panel" });
  for (const [key, [text, ic]] of Object.entries(labels)) {
    tabs[key] = el("button", { type: "button", role: "tab", id: `anew-tab-${key}`, "aria-controls": "anew-panel", onclick: () => show(key) }, aicon(ic, 14), el("span", { text }));
    tabBar.append(tabs[key]);
  }
  tabBar.addEventListener("keydown", (e) => {
    const keys = Object.keys(tabs);
    const at = keys.findIndex((k) => tabs[k].getAttribute("aria-selected") === "true");
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = keys[(at + step + keys.length) % keys.length];
    show(next);
    tabs[next].focus();
  });
  function show(key) {
    for (const [k, b] of Object.entries(tabs)) { b.setAttribute("aria-selected", String(k === key)); b.tabIndex = k === key ? 0 : -1; }
    panelHost.setAttribute("role", "tabpanel");
    panelHost.id = "anew-panel";
    panelHost.setAttribute("aria-labelledby", `anew-tab-${key}`);
    panelHost.replaceChildren(panels[key].node);
    panels[key].focus?.();
  }
  dialog.append(el("div", { class: "agent-dialog-head" }, el("h3", { text: "New agent" }),
    el("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: close }, icon("x", 18))), tabBar, panelHost);
  dialog.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  document.body.append(dialog);
  dialog.showModal();
  show(tab);

  // ----- Make with AI -----
  function aiPanel() {
    const fields = {
      request: formField({ key: "request", label: "What should this agent do?", type: "textarea", required: true,
        placeholder: "A reading list tracker. I add books with author and status, a button suggests what to read next from what I liked, and I can export the list as CSV.",
        help: "Say what you do repeatedly (buttons it needs), what it should remember (tables), anything it needs to connect to (APIs and keys), and what you want out of it." }, "", { onEnter: () => start() }),
      name: formField({ key: "name", label: "Name (optional)", type: "text", placeholder: "Reading List" }, "", { onEnter: () => start() }),
    };
    fields.request.control.rows = 6;
    const base = new Select({ label: "Start from", options: [{ value: "", label: "Blank" }], value: "" });
    request("/agents").then(({ agents }) => base.setOptions([{ value: "", label: "Blank" }, ...agents.map((a) => ({ value: a.id, label: a.name, sub: a.builtin ? "Built-in" : "" }))])).catch(() => {});
    const picker = modelPicker({ idPrefix: "anew-llm" });
    const go = el("button", { type: "button", class: "primary", onclick: () => start() }, aicon("sparkle", 15), "Start building");
    const advanced = el("details", { class: "anew-more" }, el("summary", { text: "Start from an existing agent or pick the model" }),
      el("div", { class: "agent-form" },
        el("div", { class: "form-field" }, el("label", { text: "Start from" }), base.node,
          el("p", { class: "help", text: "Copies that agent as the starting point; the builder then changes it. The original is left alone." })),
        el("div", { class: "form-field" }, el("span", { class: "af-label", text: "Model for the builder" }), picker.node)));
    const node = el("div", { class: "anew-ai" },
      el("p", { class: "note", text: "Describe it in plain words. Jarvis builds the agent with you in a chat and shows it taking shape; nothing is installed until you review it." }),
      el("div", { class: "agent-form" }, fields.request.node, fields.name.node), advanced,
      el("div", { class: "modal-actions" }, el("button", { type: "button", text: "Cancel", onclick: close }), go));
    async function start() {
      const values = { request: fields.request.get().trim(), name: fields.name.get().trim() };
      if (showErrors(fields, values.request ? {} : { request: "describe what it should do" })) return;
      const body = { request: values.request };
      if (values.name) body.name = values.name;
      if (base.value) body.base = base.value;
      const llm = picker.get();
      if (llm) body.llm = llm;
      go.disabled = true;
      try {
        const draft = await request("/agent-drafts", { method: "POST", body });
        close();
        navigate(`agents/build/${encodeURIComponent(draft.id)}`);
      } catch (error) {
        go.disabled = false;
        if (!showErrors(fields, error.data?.fields)) toast(error.message, "error");
      }
    }
    return { node, focus: () => fields.request.focus() };
  }

  // ----- Quick -----
  function quickPanel() {
    const fields = {
      name: formField({ key: "name", label: "Name", type: "text", required: true, placeholder: "Recipe Planner" }, "", { onEnter: () => create() }),
      description: formField({ key: "description", label: "Short description", type: "text", placeholder: "Plans a week of dinners from what's in the fridge" }, "", { onEnter: () => create() }),
      prompt: formField({ key: "prompt", label: "Role and instructions", type: "textarea", required: true,
        placeholder: "You are a meal planner. Ask what's in the fridge, then plan five dinners…",
        help: "What it is for, how it should work, and any rules. You can refine this later in prompt.md." }, "", { onEnter: () => create() }),
    };
    fields.prompt.control.rows = 7;
    const createBtn = el("button", { type: "button", class: "primary", text: "Create agent", onclick: () => create() });
    const node = el("div", {},
      el("p", { class: "note", text: "A plain role with no buttons or tables. Use Make with AI for settings, records, actions and tools." }),
      el("div", { class: "agent-form" }, Object.values(fields).map((f) => f.node)),
      el("div", { class: "modal-actions" }, el("button", { type: "button", text: "Cancel", onclick: close }), createBtn));
    async function create() {
      const values = Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, f.get()]));
      const errors = {};
      if (!values.name.trim()) errors.name = "give it a name";
      if (!values.prompt.trim()) errors.prompt = "describe what it does";
      if (showErrors(fields, errors)) return;
      createBtn.disabled = true;
      try {
        const detail = await request("/agents", { method: "POST", body: values });
        close();
        toast(`${detail.name} created`);
        navigate(`agents/${encodeURIComponent(detail.id)}`);
      } catch (error) {
        createBtn.disabled = false;
        if (!showErrors(fields, error.data?.fields)) toast(error.message, "error");
      }
    }
    return { node, focus: () => fields.name.focus() };
  }

  // ----- Copy -----
  function copyPanel() {
    const pick = new Select({ label: "Agent to copy", options: [], value: "", placeholder: "Loading agents…" });
    const go = el("button", { type: "button", class: "primary", text: "Copy and open", disabled: true, onclick: () => copy() });
    request("/agents").then(({ agents }) => {
      pick.setOptions(agents.map((a) => ({ value: a.id, label: a.name, sub: a.description || (a.builtin ? "Built-in" : "") })));
      pick.placeholder = "Choose an agent";
      pick.paint?.();
    }).catch((error) => toast(error.message, "error"));
    pick.onchange = (v) => { go.disabled = !v; };
    const node = el("div", {},
      el("p", { class: "note", text: "Makes your own editable copy of an agent. Use Edit with AI on the copy to change it." }),
      el("div", { class: "agent-form" }, el("div", { class: "form-field" }, el("label", { text: "Agent to copy" }), pick.node)),
      el("div", { class: "modal-actions" }, el("button", { type: "button", text: "Cancel", onclick: close }), go));
    async function copy() {
      if (!pick.value) return;
      go.disabled = true;
      try {
        const made = await request(`/agents/${encodeURIComponent(pick.value)}/duplicate`, { method: "POST" });
        close();
        toast(`Copied as ${made.name}`);
        navigate(`agents/${encodeURIComponent(made.id)}`);
      } catch (error) { go.disabled = false; toast(error.message, "error"); }
    }
    return { node, focus: () => pick.button.focus() };
  }
}
