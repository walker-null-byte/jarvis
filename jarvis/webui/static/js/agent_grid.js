// Agents grid (#/agents): one card per agent, plus the Import and New agent dialogs.
import { confirmBox, el } from "./dom.js";
import { icon } from "./icons.js";
import { agentTile } from "./agent_icons.js";
import { ago, request } from "./agent_forms.js";
import { renderReview } from "./agent_review.js";
import { openNewAgent } from "./agent_new.js";
import { emptyState, pageHeader } from "./view_parts.js";

export function mountGrid(container, ctx) {
  const { toast, navigate } = ctx;
  const grid = el("div", { class: "agent-grid", "aria-busy": "true" });
  const brokenBox = el("div", { class: "agent-broken-list" });
  const importBtn = el("button", { type: "button", onclick: () => openImport(ctx, () => load()) }, icon("upload", 16), "Import");
  const newBtn = el("button", { type: "button", class: "primary", onclick: () => openNewAgent(ctx) }, icon("plus", 16), "New agent");
  const draftsBox = el("section", { class: "agent-drafts", "aria-label": "Drafts", hidden: true });
  container.append(
    pageHeader("Agents", "Specialists with their own instructions, settings, records and tools. Import one or make your own.", importBtn, newBtn),
    draftsBox, grid, brokenBox);
  ctx.setTitle?.("Agents");

  async function loadDrafts() {
    try {
      const { drafts } = await request("/agent-drafts");
      draftsBox.hidden = !drafts.length;
      draftsBox.replaceChildren(...(drafts.length ? [el("h3", { class: "aws-h", text: "Drafts" }), el("div", { class: "agent-drafts-list" }, drafts.map(draftRow))] : []));
    } catch { draftsBox.hidden = true; }
  }

  function draftBadge(d) {
    const parts = [];
    if (d.todo_count) parts.push(`${d.todo_count} to do`);
    if (d.problem_count) parts.push(`${d.problem_count} problem${d.problem_count === 1 ? "" : "s"}`);
    return parts.join(" · ") || "Not ready";
  }

  function draftRow(d) {
    const edit = d.mode === "edit";
    const badge = d.busy ? el("span", { class: "badge", text: "Building…" })
      : d.ok ? el("span", { class: "badge ok", text: "Ready" })
      : el("span", { class: d.problem_count ? "badge warn" : "badge", text: draftBadge(d) });
    const discard = async () => {
      if (!await confirmBox(`Discard "${d.name || "this draft"}"?`, edit ? "Your changes are thrown away. The installed agent stays as it is." : "The agent being built is thrown away.")) return;
      try { await request(`/agent-drafts/${encodeURIComponent(d.id)}`, { method: "DELETE" }); toast("Draft discarded"); loadDrafts(); }
      catch (error) { toast(error.message, "error"); }
    };
    return el("div", { class: "agent-draft" }, agentTile({ icon: d.icon }, 36),
      el("div", { class: "agent-draft-title" }, el("strong", { text: d.name || "Untitled agent" }),
        el("span", { class: "note", text: `${edit ? `editing ${d.target_name || d.target}` : "new"} · ${ago(d.updated)}` })),
      badge,
      el("div", { class: "agent-draft-actions" },
        el("a", { class: "button primary small", href: `#/agents/build/${encodeURIComponent(d.id)}`, "aria-label": `Resume ${d.name || "draft"}`, text: "Resume" }),
        el("button", { type: "button", class: "small ghost", "aria-label": `Discard ${d.name || "draft"}`, text: "Discard", onclick: discard })));
  }

  async function load() {
    loadDrafts();
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
    renderReview(body, card, {
      toast,
      setStaging: (token) => { state.staging = token; },
      close: (token) => close(token),
      installed: (detail) => {
        close();
        toast(`${detail.name} installed`);
        done?.();
        navigate(`agents/${encodeURIComponent(detail.id)}`);
      },
    });
  }

  chooser();
}
