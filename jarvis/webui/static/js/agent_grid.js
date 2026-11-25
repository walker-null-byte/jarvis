// Agents grid (#/agents): one card per agent, plus the Import and New agent dialogs.
import { el } from "./dom.js";
import { icon } from "./icons.js";
import { agentTile, aicon } from "./agent_icons.js";
import { ago, formField, request, showErrors } from "./agent_forms.js";
import { emptyState, pageHeader } from "./view_parts.js";

export function mountGrid(container, ctx) {
  const { toast, navigate } = ctx;
  const grid = el("div", { class: "agent-grid", "aria-busy": "true" });
  const brokenBox = el("div", { class: "agent-broken-list" });
  const importBtn = el("button", { type: "button", onclick: () => openImport(ctx, () => load()) }, icon("upload", 16), "Import");
  const newBtn = el("button", { type: "button", class: "primary", onclick: () => openWizard(ctx) }, icon("plus", 16), "New agent");
  container.append(
    pageHeader("Agents", "Specialists with their own instructions, settings, records and tools. Import one or make your own.", importBtn, newBtn),
    grid, brokenBox);
  ctx.setTitle?.("Agents");

  async function load() {
    try {
      const { agents, broken } = await request("/agents");
      grid.removeAttribute("aria-busy");
      grid.replaceChildren(...agents.map(card));
      if (!agents.length) grid.replaceChildren(emptyState("agents", "No agents yet", "Import an agent from a .zip or GitHub, or make a new one."));
      brokenBox.replaceChildren(...(broken || []).map(brokenCard));
    } catch (error) {
      grid.replaceChildren(el("p", { class: "agent-error", role: "alert", text: error.message }));
    }
  }

  function card(agent) {
    const badges = [];
    if (agent.builtin) badges.push(el("span", { class: "badge", text: "Built-in" }));
    if (agent.needs_trust) badges.push(el("span", { class: "badge warn", text: "Code not trusted" }));
    else if (agent.missing_settings.length) badges.push(el("span", { class: "badge warn", text: "Needs setup" }));
    const counts = agent.records.map((r) => `${r.count} ${r.label.toLowerCase()}`).join(" · ");
    const last = agent.last_run ? `Last run: ${agent.last_run.label} · ${ago(agent.last_run.time)}` : "";
    return el("a", { class: "agent-card", href: `#/agents/${encodeURIComponent(agent.id)}`, "aria-label": `Open ${agent.name}` },
      el("div", { class: "agent-card-head" }, agentTile(agent, 44),
        el("div", { class: "agent-card-title" },
          el("strong", { text: agent.name }),
          el("span", { class: "note", text: `v${agent.version}${agent.author ? ` · ${agent.author}` : ""}` })),
        el("div", { class: "agent-card-badges" }, badges)),
      el("p", { class: "agent-card-desc", text: agent.description || "No description." }),
      el("div", { class: "agent-card-foot" },
        counts ? el("span", { text: counts }) : el("span", { text: `${agent.actions} action${agent.actions === 1 ? "" : "s"}` }),
        last ? el("span", { class: "note", text: last }) : null));
  }

  function brokenCard(item) {
    return el("div", { class: "agent-broken", role: "alert" },
      el("div", { class: "agent-broken-head" }, icon("alert", 16), el("strong", { text: `${item.folder} can't be loaded` })),
      el("pre", { text: item.message }),
      el("p", { class: "note", text: item.builtin ? "This is a built-in agent folder." : `Fix or delete the folder mind/agents/${item.folder}.` }));
  }

  load();
  return { refresh: load };
}

// ---------- import ----------

export function openImport(ctx, done) {
  const { toast, navigate } = ctx;
  const dialog = el("dialog", { class: "modal agent-dialog", "aria-label": "Import an agent" });
  const close = (staging) => {
    if (staging) request(`/agents/import/${staging}`, { method: "DELETE" }).catch(() => {});
    dialog.close();
    dialog.remove();
  };
  const body = el("div", { class: "agent-dialog-body" });
  dialog.append(el("div", { class: "agent-dialog-head" }, el("h3", { text: "Import an agent" }),
    el("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: () => close(state.staging) }, icon("x", 18))), body);
  dialog.addEventListener("cancel", (e) => { e.preventDefault(); close(state.staging); });
  document.body.append(dialog);
  dialog.showModal();
  const state = { staging: null };

  function chooser(errorText = "", issues = []) {
    state.staging = null;
    const file = el("input", { type: "file", accept: ".zip,application/zip", hidden: true });
    const drop = el("button", { type: "button", class: "agent-drop", onclick: () => file.click() },
      icon("upload", 26), el("strong", { text: "Drop a .zip here" }), el("span", { class: "note", text: "or click to choose a file" }));
    const url = el("input", { type: "text", placeholder: "https://github.com/owner/repo/tree/main/my-agent", "aria-label": "GitHub link, .zip link or folder path", spellcheck: "false" });
    const go = el("button", { type: "button", class: "primary", text: "Import" });
    const submit = () => {
      const value = url.value.trim();
      if (!value) { url.focus(); return; }
      send(/^(\/|~)/.test(value) ? { path: value } : { url: value });
    };
    go.addEventListener("click", submit);
    url.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    file.addEventListener("change", () => { if (file.files[0]) upload(file.files[0]); });
    for (const kind of ["dragenter", "dragover"]) drop.addEventListener(kind, (e) => { e.preventDefault(); drop.classList.add("over"); });
    for (const kind of ["dragleave", "drop"]) drop.addEventListener(kind, () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) upload(f); });
    body.replaceChildren(
      errorText ? errorBox(errorText, issues) : null,
      drop, file,
      el("div", { class: "agent-or" }, el("span", { text: "or" })),
      el("label", { class: "form-field" }, el("span", { class: "af-label", text: "GitHub link, .zip link or a folder on this computer" }),
        el("div", { class: "agent-url-row" }, url, go)),
      el("p", { class: "help", text: "GitHub: a repository, or a folder in one (…/tree/branch/folder). Nothing runs until you review it." }));
    (errorText ? url : drop).focus();
  }

  function errorBox(text, issues) {
    const lines = issues.length ? issues : [text];
    return el("div", { class: "agent-import-error", role: "alert" },
      el("div", { class: "agent-broken-head" }, icon("alert", 16), el("strong", { text: issues.length ? "This agent has problems" : "Import failed" })),
      issues.length ? el("ul", {}, lines.map((l) => el("li", { text: l }))) : el("p", { text }));
  }

  function busy(text) {
    body.replaceChildren(el("div", { class: "agent-loading", role: "status" }, el("span", { class: "spin on" }), el("span", { text })));
  }

  async function upload(f) {
    if (!/\.zip$/i.test(f.name)) { chooser(`${f.name} is not a .zip file`); return; }
    busy(`Checking ${f.name}…`);
    const form = new FormData();
    form.append("file", f);
    try { review(await request("/agents/import", { method: "POST", form })); }
    catch (error) { chooser(error.message, error.data?.issues || []); }
  }

  async function send(payload) {
    busy(payload.url ? "Downloading and checking…" : "Checking the folder…");
    try { review(await request("/agents/import", { method: "POST", body: payload })); }
    catch (error) { chooser(error.message, error.data?.issues || []); }
  }

  function review(card) {
    state.staging = card.staging;
    const perms = card.permissions;
    const chip = (ok, label) => el("span", { class: `perm ${ok ? "" : "off"}` }, aicon(ok ? "check" : "x", 12), label);
    const list = (title, items) => items.length ? el("div", { class: "review-row" }, el("span", { class: "review-key", text: title }), el("span", { text: items.join(", ") })) : null;
    let mode = card.existing ? (card.existing.builtin ? "copy" : "upgrade") : "new";
    const modes = card.existing ? el("fieldset", { class: "review-modes" },
      el("legend", { text: `You already have ${card.existing.name} v${card.existing.version}` }),
      ...(card.existing.builtin ? [] : [radio("upgrade", `Upgrade to v${card.version} (keeps its records and settings)`)]),
      radio("copy", "Install as a separate copy")) : null;
    function radio(value, label) {
      const input = el("input", { type: "radio", name: "import-mode", value });
      input.checked = mode === value;
      input.addEventListener("change", () => { mode = value; });
      return el("label", { class: "check" }, input, el("span", { text: label }));
    }
    const code = card.py_files.length ? el("div", { class: "review-code", role: "note" },
      el("div", { class: "agent-broken-head" }, aicon("code", 16), el("strong", { text: `Includes Python code (${card.py_files.length} file${card.py_files.length > 1 ? "s" : ""})` })),
      el("ul", {}, card.py_files.map((f) => el("li", {}, el("code", { text: f })))),
      el("p", { class: "note", text: "Trusted code runs on your computer with your permissions. Only trust agents from people you trust. You can install without the code and trust it later." })) : null;
    const install = async (trust) => {
      buttons.forEach((b) => { b.disabled = true; });
      try {
        const detail = await request(`/agents/${encodeURIComponent(card.id)}/install`, { method: "POST", body: { staging: card.staging, mode, trust } });
        state.staging = null;
        close();
        toast(`${detail.name} installed`);
        done?.();
        navigate(`agents/${encodeURIComponent(detail.id)}`);
      } catch (error) {
        buttons.forEach((b) => { b.disabled = false; });
        toast(error.message, "error");
      }
    };
    const buttons = [el("button", { type: "button", text: "Cancel", onclick: () => close(card.staging) })];
    if (card.py_files.length) {
      buttons.push(el("button", { type: "button", text: "Install without code", onclick: () => install(false) }));
      buttons.push(el("button", { type: "button", class: "primary", onclick: () => install(true) }, aicon("shield", 15), "Trust & install"));
    } else {
      buttons.push(el("button", { type: "button", class: "primary", text: "Install", onclick: () => install(false) }));
    }
    const dataCounts = Object.entries(card.data || {}).map(([t, n]) => `${n} ${t}`);
    body.replaceChildren(el("div", { class: "review" },
      el("div", { class: "review-head" }, agentTile({ icon: card.icon }, 48),
        el("div", {}, el("strong", { class: "review-name", text: card.name }),
          el("div", { class: "note", text: `v${card.version}${card.author ? ` · by ${card.author}` : ""} · ${card.id}` }))),
      card.description ? el("p", { class: "review-desc", text: card.description }) : null,
      el("div", { class: "review-perms", "aria-label": "Permissions" },
        chip(perms.network, perms.network ? "Uses the internet" : "No internet"),
        chip(perms.commands, perms.commands ? "Can run commands" : "No commands"),
        chip(perms.files !== "none", { data: "Reads files; writes only its data", workspace: "Can edit your files", none: "No file access" }[perms.files])),
      el("div", { class: "review-list" },
        list("Actions", card.actions), list("Records", card.records), list("Exports", card.exports),
        list("Settings", card.settings), list("Skills", card.skills), list("Data included", dataCounts)),
      code, modes,
      card.warnings.length ? el("details", { class: "review-warn" }, el("summary", { text: `${card.warnings.length} warning${card.warnings.length > 1 ? "s" : ""}` }),
        el("ul", {}, card.warnings.map((w) => el("li", { text: w })))) : null,
      card.skipped.length ? el("details", { class: "review-warn" }, el("summary", { text: `${card.skipped.length} file${card.skipped.length > 1 ? "s" : ""} skipped` }),
        el("ul", {}, card.skipped.map((w) => el("li", { text: w })))) : null,
      el("div", { class: "modal-actions" }, buttons)));
    buttons[buttons.length - 1].focus();
  }

  chooser();
}

// ---------- new agent wizard ----------

export function openWizard(ctx) {
  const { toast, navigate } = ctx;
  const dialog = el("dialog", { class: "modal agent-dialog", "aria-label": "New agent" });
  const close = () => { dialog.close(); dialog.remove(); };
  const fields = {
    name: formField({ key: "name", label: "Name", type: "text", required: true, placeholder: "Recipe Planner" }, "", { onEnter: () => create() }),
    description: formField({ key: "description", label: "Short description", type: "text", placeholder: "Plans a week of dinners from what's in the fridge" }, "", { onEnter: () => create() }),
    prompt: formField({ key: "prompt", label: "Role and instructions", type: "textarea", required: true,
      placeholder: "You are a meal planner. Ask what's in the fridge, then plan five dinners…",
      help: "What it is for, how it should work, and any rules. You can refine this later in prompt.md." }, "", { onEnter: () => create() }),
  };
  fields.prompt.control.rows = 7;
  const createBtn = el("button", { type: "button", class: "primary", text: "Create agent", onclick: () => create() });
  dialog.append(el("div", { class: "agent-dialog-head" }, el("h3", { text: "New agent" }),
      el("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: close }, icon("x", 18))),
    el("p", { class: "note", text: "Start with a role. Settings, records, actions and tools can be added to its agent.toml later." }),
    el("div", { class: "agent-form" }, Object.values(fields).map((f) => f.node)),
    el("div", { class: "modal-actions" }, el("button", { type: "button", text: "Cancel", onclick: close }), createBtn));
  dialog.addEventListener("cancel", close);
  document.body.append(dialog);
  dialog.showModal();
  fields.name.focus();

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
}
