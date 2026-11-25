// Agent workspace (#/agents/<id>): header, actions with inline input forms, records,
// exports, and the agent's chat. Two columns on desktop, tabs on phones.
import { api } from "./api.js";
import { Select } from "./combo.js";
import { confirmBox, el, iconBtn } from "./dom.js";
import { icon } from "./icons.js";
import { renderMarkdown } from "./markdown.js";
import { AgentChat } from "./agent_chat.js";
import { agentTile, aicon } from "./agent_icons.js";
import { download, formField, request, showErrors } from "./agent_forms.js";
import { RecordsView } from "./agent_records.js";

export function mountWorkspace(container, agentId, ctx) {
  const { bus, toast, navigate } = ctx;
  const root = el("div", { class: "aws", "aria-busy": "true" });
  container.append(root);
  container.classList.add("agents-wide");
  let alive = true;
  let detail = null;
  let chat = null;
  const tables = new Map(); // name -> RecordsView
  let refreshTimer = 0;
  let drawer = null;
  let runActionUI = () => {};
  const offBus = bus.on((msg) => {
    if (msg.kind === "agent_records_changed" && msg.data?.agent_id === agentId) {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => tables.get(msg.data.table)?.refresh(true), 250);
    }
  });

  async function load(preferredSession) {
    try {
      detail = await request(`/agents/${encodeURIComponent(agentId)}`);
    } catch (error) {
      root.removeAttribute("aria-busy");
      root.replaceChildren(el("div", { class: "aws-missing" },
        el("h2", { text: error.status === 404 ? "Agent not found" : "Could not open this agent" }),
        el("p", { class: "note", text: error.message }),
        el("a", { class: "button", href: "#/agents" }, icon("left", 15), "All agents")));
      return;
    }
    if (!alive) return;
    ctx.setTitle?.(detail.name);
    render(preferredSession);
  }

  function render(preferredSession) {
    chat?.destroy();
    tables.clear();
    root.removeAttribute("aria-busy");
    const m = detail.manifest;

    // ----- header -----
    const moreMenu = el("div", { class: "menu aws-menu", role: "menu", hidden: true });
    const moreBtn = iconBtn("more", "More", (e) => { e.stopPropagation(); toggleMenu(); }, "", 18);
    moreBtn.setAttribute("aria-haspopup", "menu");
    function toggleMenu(force) {
      const open = force ?? moreMenu.hidden;
      moreMenu.hidden = !open;
      moreBtn.setAttribute("aria-expanded", String(open));
      if (open) moreMenu.querySelector("button")?.focus();
    }
    const item = (name, label, fn, cls = "") => el("button", { type: "button", role: "menuitem", class: cls, onclick: () => { toggleMenu(false); fn(); } }, aicon(name, 15), label);
    moreMenu.append(
      item("copy", "Duplicate to customise", duplicate),
      item("download", "Export agent (.zip)", () => download(`/agents/${encodeURIComponent(agentId)}/export.zip`)),
      item("download", "Export with records", () => download(`/agents/${encodeURIComponent(agentId)}/export.zip?data=1`)),
      detail.py_files.length ? item("shield", detail.trusted ? "Stop trusting its code" : "Review code & trust", () => openTrust()) : null,
      detail.builtin ? null : item("trash", "Remove agent", remove, "danger"));
    moreMenu.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); toggleMenu(false); moreBtn.focus(); } });
    const header = el("header", { class: "aws-head" },
      el("a", { class: "icon-btn aws-back", href: "#/agents", "aria-label": "All agents", title: "All agents" }, icon("left", 18)),
      agentTile(detail, 40),
      el("div", { class: "aws-title" },
        el("h2", {}, el("span", { text: detail.name }), el("span", { class: "badge", text: `v${detail.version}` }),
          detail.builtin ? el("span", { class: "badge", text: "Built-in" }) : null),
        el("p", { class: "note", text: detail.description })),
      el("div", { class: "aws-head-actions" },
        el("button", { type: "button", onclick: () => openSettings() }, icon("sliders", 15), el("span", { class: "label", text: "Settings" }),
          detail.missing_settings.length ? el("span", { class: "dot-warn", "aria-label": "needs setup" }) : null),
        el("button", { type: "button", onclick: () => openAbout() }, icon("info", 15), el("span", { class: "label", text: "About" })),
        el("div", { class: "popover-wrap" }, moreBtn, moreMenu)));

    // ----- banners -----
    const banners = el("div", { class: "aws-banners" });
    if (detail.missing_settings.length) {
      const labels = detail.missing_settings.map((k) => m.settings.find((s) => s.key === k)?.label || k);
      banners.append(el("div", { class: "aws-banner info" }, aicon("sliders", 16),
        el("span", { text: `Before it can start, ${detail.name} needs: ${labels.join(" · ")}` }),
        el("button", { type: "button", class: "small primary", text: "Open settings", onclick: () => openSettings() })));
    }
    if (detail.needs_trust) {
      banners.append(el("div", { class: "aws-banner warn" }, aicon("shield", 16),
        el("span", { text: `Its Python tools (${detail.py_files.join(", ")}) are not loaded until you trust them.` }),
        el("button", { type: "button", class: "small", text: "Review & trust", onclick: () => openTrust() })));
    }
    for (const problem of detail.tool_errors) {
      banners.append(el("div", { class: "aws-banner danger", role: "alert" }, icon("alert", 16),
        el("span", {}, el("code", { text: problem.file }), ` failed to load: ${problem.error}`)));
    }

    // ----- actions -----
    const actionList = el("div", { class: "aws-actions" });
    const formHost = el("div", { class: "aws-form-host" });
    let openAction = null;
    for (const action of m.actions) {
      const button = el("button", { type: "button", class: "aws-action", "aria-expanded": "false", dataset: { action: action.id },
        onclick: () => toggleAction(action, button) },
        el("span", { class: "aws-action-icon" }, aicon(action.icon || "play", 17)),
        el("span", { class: "aws-action-text" }, el("strong", { text: action.label }),
          action.description ? el("span", { class: "note", text: action.description }) : null));
      actionList.append(button);
    }
    function toggleAction(action, button) {
      if (openAction === action.id) { closeForm(); return; }
      actionList.querySelectorAll(".aws-action").forEach((b) => { b.classList.remove("on"); b.setAttribute("aria-expanded", "false"); });
      button.classList.add("on");
      button.setAttribute("aria-expanded", "true");
      openAction = action.id;
      formHost.replaceChildren(actionForm(action, () => closeForm(button)));
      formHost.querySelector("input, textarea, .combo-trigger, button.primary")?.focus();
    }
    function closeForm(button) {
      openAction = null;
      formHost.replaceChildren();
      actionList.querySelectorAll(".aws-action").forEach((b) => { b.classList.remove("on"); b.setAttribute("aria-expanded", "false"); });
      button?.focus();
    }
    runActionUI = (id) => {
      const action = m.actions.find((a) => a.id === id);
      const button = actionList.querySelector(`[data-action="${CSS.escape(id)}"]`);
      if (action && button) { if (openAction !== id) toggleAction(action, button); button.scrollIntoView({ block: "nearest" }); }
    };

    function actionForm(action, close) {
      const fields = Object.fromEntries(action.inputs.map((spec) => [spec.key, formField(spec, undefined, { onEnter: () => run() })]));
      const runBtn = el("button", { type: "button", class: "primary" }, aicon("play", 14), `Run ${action.label.toLowerCase()}`);
      const where = el("p", { class: "note aws-where" });
      const updateWhere = () => { where.textContent = chat?.sid ? "Runs in the open chat, so you can watch it work." : "Starts a new chat, so you can watch it work."; };
      updateWhere();
      async function run() {
        showErrors(fields, {});
        const inputs = Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, f.get()]));
        runBtn.disabled = true;
        try {
          await chat.runAction(action.id, inputs);
          close();
          showTab("chat");
        } catch (error) {
          if (error.data?.missing_settings) { toast(error.message, "error"); openSettings(); }
          else if (!showErrors(fields, error.data?.fields)) toast(error.message, "error");
        } finally { runBtn.disabled = false; }
      }
      runBtn.addEventListener("click", run);
      const box = el("div", { class: "aws-form", role: "group", "aria-label": action.label },
        action.inputs.length ? el("div", { class: "agent-form" }, Object.values(fields).map((f) => f.node)) : el("p", { class: "note", text: action.description || "No inputs needed." }),
        where,
        el("div", { class: "aws-form-actions" }, el("button", { type: "button", class: "ghost", text: "Cancel", onclick: () => close() }), runBtn));
      box.addEventListener("keydown", (e) => { if (e.key === "Escape" && !e.target.closest(".combo")) { e.stopPropagation(); close(); } });
      return box;
    }

    // ----- records & exports -----
    const exportsBtn = m.exports.length ? exportMenu(m.exports) : null;
    const recordTabs = el("div", { class: "aws-rec-tabs", role: "tablist", "aria-label": "Tables" });
    const recordHost = el("div", { class: "aws-rec-host" });
    const firstAction = m.actions[0];
    m.records.forEach((t, i) => {
      const view = new RecordsView({ agentId, table: t, toast, emptyActionLabel: firstAction?.label,
        onEmptyAction: () => { showTab("work"); runActionUI(firstAction.id); } });
      tables.set(t.name, view);
      const tab = el("button", { type: "button", role: "tab", class: "aws-rec-tab", "aria-selected": String(i === 0) },
        el("span", { text: t.label }), el("span", { class: "rec-chip-n", text: String(detail.records[i]?.count ?? 0) }));
      view.onCount = (n) => { tab.lastChild.textContent = String(n); };
      tab.addEventListener("click", () => {
        recordTabs.querySelectorAll(".aws-rec-tab").forEach((b) => b.setAttribute("aria-selected", "false"));
        tab.setAttribute("aria-selected", "true");
        recordHost.replaceChildren(view.node);
      });
      recordTabs.append(tab);
      if (i === 0) recordHost.append(view.node);
      view.render();
      view.refresh();
    });

    const left = el("div", { class: "aws-left", role: "tabpanel", id: "aws-panel-work" },
      banners,
      m.actions.length ? el("section", { class: "aws-section" },
        el("h3", { class: "aws-h", text: "Actions" }), actionList, formHost) : null,
      m.records.length ? el("section", { class: "aws-section" },
        el("div", { class: "aws-rec-head" }, el("h3", { class: "aws-h", text: "Records" }), recordTabs, el("div", { class: "spacer" }), exportsBtn),
        recordHost) : null,
      !m.actions.length && !m.records.length ? el("div", { class: "aws-plain" }, aicon("pen", 22),
        el("p", { text: `${detail.name} is a chat agent: talk to it on the right.` }), exportsBtn) : null);

    chat = new AgentChat({ agent: detail, bus, toast, onSwitchModel: () => openSettings("model") });
    chat.onBusy = (busy) => { chatTab.classList.toggle("working", busy); };
    const right = el("div", { class: "aws-right", role: "tabpanel", id: "aws-panel-chat" }, chat.node);

    // ----- phone tabs -----
    const workTab = el("button", { type: "button", role: "tab", "aria-controls": "aws-panel-work", onclick: () => showTab("work") }, aicon("zap", 15), "Workspace");
    const chatTab = el("button", { type: "button", role: "tab", "aria-controls": "aws-panel-chat", onclick: () => showTab("chat") }, icon("discussions", 15), "Chat", el("span", { class: "tab-dot", "aria-hidden": "true" }));
    const tabs = el("div", { class: "aws-tabs", role: "tablist", "aria-label": "Workspace or chat" }, workTab, chatTab);
    function showTab(name) {
      root.dataset.tab = name;
      workTab.setAttribute("aria-selected", String(name === "work"));
      chatTab.setAttribute("aria-selected", String(name === "chat"));
    }
    showTab(root.dataset.tab || "work");

    root.replaceChildren(header, tabs, el("div", { class: "aws-cols" }, left, right));
    chat.start(preferredSession);
    if (detail.missing_settings.length && !sessionStorage.getItem(`agent-setup-${agentId}`)) {
      try { sessionStorage.setItem(`agent-setup-${agentId}`, "1"); } catch { /* storage blocked */ }
      openSettings();
    }
  }

  document.addEventListener("click", closeMenus);
  function closeMenus(e) {
    if (!e.target.closest(".popover-wrap")) root.querySelectorAll(".aws-menu, .aws-export-menu").forEach((n) => { n.hidden = true; });
  }

  function exportMenu(exports) {
    const menu = el("div", { class: "menu aws-export-menu", role: "menu", hidden: true });
    const button = el("button", { type: "button", class: "small", "aria-haspopup": "menu", "aria-expanded": "false" }, icon("download", 14), "Export", icon("down", 13));
    button.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      button.setAttribute("aria-expanded", String(!menu.hidden));
      if (!menu.hidden) menu.querySelector("button")?.focus();
    });
    menu.append(...exports.map((x) => el("button", { type: "button", role: "menuitem", onclick: () => {
      menu.hidden = true;
      download(`/agents/${encodeURIComponent(agentId)}/exports/${encodeURIComponent(x.id)}`);
    } }, el("span", { class: "menu-text" }, el("span", { text: x.label }),
      el("span", { class: "note", text: x.description || x.format.toUpperCase() })))));
    menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); menu.hidden = true; button.focus(); } });
    return el("div", { class: "popover-wrap" }, button, menu);
  }

  // ---------- side panel (settings / about / trust) ----------

  function openPanel(title, body, footer) {
    closePanel();
    const panel = el("aside", { class: "aws-drawer", role: "dialog", "aria-modal": "true", "aria-label": title },
      el("div", { class: "aws-drawer-head" }, el("h3", { text: title }), iconBtn("x", "Close", () => closePanel(), "", 18)),
      el("div", { class: "aws-drawer-body" }, body),
      footer ? el("div", { class: "aws-drawer-foot" }, footer) : null);
    const scrim = el("div", { class: "aws-scrim", onclick: () => closePanel() });
    panel.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !e.target.closest(".combo")) { e.stopPropagation(); closePanel(); }
    });
    root.append(scrim, panel);
    drawer = { panel, scrim, opener: document.activeElement };
    requestAnimationFrame(() => panel.classList.add("open"));
    (panel.querySelector(".aws-drawer-body input, .aws-drawer-body textarea, .aws-drawer-body button") || panel.querySelector("button"))?.focus();
    return panel;
  }

  function closePanel() {
    if (!drawer) return;
    drawer.panel.remove();
    drawer.scrim.remove();
    drawer.opener?.focus?.();
    drawer = null;
  }

  async function openSettings(focus) {
    let info;
    try { info = await request(`/agents/${encodeURIComponent(agentId)}/settings`); }
    catch (error) { toast(error.message, "error"); return; }
    const fields = {};
    const secretState = {};
    const plain = info.fields.filter((f) => f.type !== "secret");
    const secrets = info.fields.filter((f) => f.type === "secret");
    for (const spec of plain) fields[spec.key] = formField(spec, info.values[spec.key], { onEnter: () => save() });
    const secretNodes = secrets.map((spec) => secretField(spec, info.secrets[spec.key], secretState));
    // model override
    const providerSelect = new Select({ label: "Provider for this agent", options: [{ value: "", label: "Same as the main chat" }], value: info.llm.provider || "" });
    api("/providers").then(({ providers }) => {
      providerSelect.setOptions([{ value: "", label: "Same as the main chat" }, ...providers.map((p) => ({ value: p.id, label: p.label || p.id }))]);
      providerSelect.setValue(info.llm.provider || "");
    }).catch(() => {});
    const model = el("input", { type: "text", placeholder: info.manifest_llm.model || "Provider's default model", "aria-label": "Model for this agent" });
    model.value = info.llm.model || "";
    const intro = info.missing.length ? el("p", { class: "hint-box", text: `${detail.name} needs a few details before it can start. Fields marked * are required; the rest make its work better.` }) : null;
    const saveBtn = el("button", { type: "button", class: "primary", text: "Save" });
    const body = el("div", { class: "agent-form" }, intro,
      plain.map((spec) => fields[spec.key].node),
      secrets.length ? el("h4", { class: "aws-sub", text: "API keys" }) : null,
      secrets.length ? el("p", { class: "help", text: "Saved on this computer in your keystore, never shown again and never exported." }) : null,
      secretNodes,
      el("h4", { class: "aws-sub", id: "aws-model-head", text: "Model" }),
      el("div", { class: "form-field" }, el("label", { text: "Provider" }), providerSelect.node),
      el("div", { class: "form-field" }, el("label", { text: "Model (optional)" }), model,
        el("p", { class: "help", text: "Give this agent its own model, e.g. a stronger one for research. Empty follows the main chat." })));
    const panel = openPanel(`${detail.name} settings`, body,
      [el("button", { type: "button", class: "ghost", text: "Cancel", onclick: () => closePanel() }), saveBtn]);
    if (focus === "model") panel.querySelector("#aws-model-head")?.scrollIntoView();
    saveBtn.addEventListener("click", save);
    async function save() {
      const values = Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, f.get()]));
      const errors = {};
      for (const spec of plain) {
        const v = values[spec.key];
        if (spec.required && (v === "" || v === null || (Array.isArray(v) && !v.length))) errors[spec.key] = "is required";
      }
      if (showErrors(fields, errors)) return;
      saveBtn.disabled = true;
      try {
        await request(`/agents/${encodeURIComponent(agentId)}/settings`, { method: "PUT",
          body: { values, secrets: secretState, llm: { provider: providerSelect.value, model: model.value.trim() } } });
        toast("Settings saved");
        closePanel();
        load(chat?.sid);
      } catch (error) {
        saveBtn.disabled = false;
        if (!showErrors(fields, error.data?.fields)) toast(error.message, "error");
      }
    }
  }

  function secretField(spec, status, state) {
    const box = el("div", { class: "form-field secret-field" });
    const label = el("label", { text: spec.label || spec.key });
    const help = spec.help ? el("p", { class: "help", text: spec.help }) : null;
    function saved() {
      box.replaceChildren(label, el("div", { class: "secret-row" },
        el("span", { class: "secret-saved" }, icon("key", 14), `Saved ${status.hint}`),
        el("button", { type: "button", class: "small", text: "Replace", onclick: () => editing(true) }),
        el("button", { type: "button", class: "small danger", text: "Forget", onclick: () => { state[spec.key] = null; status = { saved: false }; editing(false); } })), help);
    }
    function editing(replacing) {
      const input = el("input", { type: "password", autocomplete: "off", placeholder: replacing ? "Paste the new key" : "Paste the key", "aria-label": spec.label || spec.key });
      input.addEventListener("input", () => { if (input.value.trim()) state[spec.key] = input.value.trim(); else delete state[spec.key]; });
      box.replaceChildren(label, input, state[spec.key] === null ? el("p", { class: "help", text: "Will be forgotten when you save." }) : null, help);
      if (replacing) input.focus();
    }
    if (status?.saved) saved(); else editing(false);
    return box;
  }

  function openAbout() {
    const m = detail.manifest;
    const perms = m.permissions;
    const row = (k, v) => v ? el("div", { class: "review-row" }, el("span", { class: "review-key", text: k }), el("span", { text: v })) : null;
    openPanel(`About ${detail.name}`, el("div", { class: "aws-about" },
      el("div", { class: "review-list" },
        row("Version", `${detail.version}${detail.author ? ` by ${detail.author}` : ""}`),
        row("Permissions", [perms.network ? "internet" : "no internet", perms.commands ? "runs commands" : "no commands",
          { data: "reads files, writes only its data", workspace: "edits files", none: "no files" }[perms.files]].join(" · ")),
        row("Its tools", [...detail.tools, ...(m.records.length ? ["records_add", "records_update", "records_query", "records_delete"] : [])].join(", ")),
        row("Jarvis tools", m.agent.core_tools.join(", ")),
        row("Skills", detail.skills.join(", ")),
        row("Folder", detail.location)),
      detail.warnings.length ? el("details", { class: "review-warn" }, el("summary", { text: "Manifest warnings" }), el("ul", {}, detail.warnings.map((w) => el("li", { text: w })))) : null,
      el("div", { class: "md aws-readme" }, renderMarkdown(detail.readme || `# ${detail.name}\n\n${detail.description || ""}`))));
  }

  function openTrust() {
    const trusting = !detail.trusted;
    const go = el("button", { type: "button", class: trusting ? "primary" : "danger", text: trusting ? "Trust and load tools" : "Stop trusting" });
    go.addEventListener("click", async () => {
      go.disabled = true;
      try {
        detail = await request(`/agents/${encodeURIComponent(agentId)}/trust`, { method: "POST", body: { trusted: trusting } });
        closePanel();
        toast(trusting ? "Tools loaded" : "Tools unloaded");
        render(chat?.sid);
      } catch (error) { go.disabled = false; toast(error.message, "error"); }
    });
    openPanel(trusting ? "Trust this agent's code?" : "Stop trusting this code?", el("div", { class: "agent-form" },
      el("p", { text: trusting
        ? `${detail.name} comes with Python tools. Once trusted they run inside Jarvis with your permissions: they can read and change files and use the internet like any program you run.`
        : "Its Python tools will no longer load. Prompt, settings, records and actions keep working." }),
      el("ul", { class: "aws-files" }, detail.py_files.map((f) => el("li", {}, el("code", { text: f })))),
      el("p", { class: "note", text: `Read them first in ${detail.location}. Trust covers exactly this code: if a file changes, you're asked again.` })),
      [el("button", { type: "button", class: "ghost", text: "Cancel", onclick: () => closePanel() }), go]);
  }

  async function duplicate() {
    try {
      const copy = await request(`/agents/${encodeURIComponent(agentId)}/duplicate`, { method: "POST" });
      toast(`Copied as ${copy.name}. Edit its files in ${copy.location}.`);
      navigate(`agents/${encodeURIComponent(copy.id)}`);
    } catch (error) { toast(error.message, "error"); }
  }

  async function remove() {
    const ok = await confirmBox(`Remove ${detail.name}?`, "Its files, settings, saved keys and records are deleted. Its chats stay in your history.");
    if (!ok) return;
    try {
      await request(`/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" });
      toast(`${detail.name} removed`);
      navigate("agents");
    } catch (error) { toast(error.message, "error"); }
  }

  load();
  return {
    unmount() {
      alive = false;
      offBus();
      chat?.destroy();
      clearTimeout(refreshTimer);
      document.removeEventListener("click", closeMenus);
      container.classList.remove("agents-wide");
    },
  };
}

