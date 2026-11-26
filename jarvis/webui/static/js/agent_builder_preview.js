// Live preview of a builder draft: a mini version of the agent's workspace.
// new BuilderPreview({ draftId }) -> { node, update(draft, busy) }
import { el } from "./dom.js";
import { icon } from "./icons.js";
import { aicon, agentTile } from "./agent_icons.js";
import { pretty, request } from "./agent_forms.js";
import { permChips } from "./agent_review.js";

const chip = (text, cls = "", ...extra) => el("span", { class: `pchip ${cls}`.trim() }, ...extra, text);
const section = (title, count, ...body) => el("section", { class: "bp-section" },
  el("h3", { class: "aws-h" }, title, count ? el("span", { class: "bp-count", text: String(count) }) : null), ...body);

export class BuilderPreview {
  constructor({ draftId }) {
    this.draftId = draftId;
    this.node = el("div", { class: "bp", "aria-live": "off" });
    this.openCode = new Set();     // file paths whose code is shown
    this.code = new Map();         // path -> {text} | {error}, valid for one draft revision
    this.codeFor = "";
    this.problemsOpen = false;
    this.todoOpen = true;
    this.draft = null;
    this.busy = false;
  }

  update(draft, busy) {
    this.draft = draft;
    this.busy = busy;
    if (draft.updated !== this.codeFor) { this.codeFor = draft.updated; this.code.clear(); }
    this.render();
  }

  render() {
    const d = this.draft;
    const m = d.manifest || {};
    const check = d.check || { ok: false, todo: [], problems: [], notes: [], tools: [] };
    const settings = m.settings || [];
    const records = m.records || [];
    const actions = m.actions || [];
    const exports = m.exports || [];
    const pyTools = check.tools || [];
    const files = d.files || [];
    const empty = !settings.length && !records.length && !actions.length && !exports.length && !pyTools.length;

    const parts = [
      el("div", { class: "bp-head" }, agentTile({ icon: d.icon || m.icon, has_icon_file: false }, 48),
        el("div", { class: "bp-title" },
          el("h3", {}, el("span", { text: d.name || m.name || "Untitled agent" }), d.version ? el("span", { class: "badge", text: `v${d.version}` }) : null),
          el("p", { class: "note", text: d.description || m.description || "No description yet." }))),
      this.status(check),
    ];
    if (empty) {
      parts.push(this.busy ? this.planning() : el("p", { class: "bp-empty note", text: "Nothing built yet. Ask the builder for actions, tables or settings and they appear here." }));
    }
    if (actions.length) parts.push(section("Actions", actions.length, el("div", { class: "bp-list" }, actions.map((a) => this.action(a)))));
    if (records.length) parts.push(section("Records", records.length, el("div", { class: "bp-list" }, records.map((t) => this.table(t)))));
    if (settings.length) parts.push(section("Settings", settings.length, el("div", { class: "bp-list tight" }, settings.map((s) => this.setting(s)))));
    if (exports.length) parts.push(section("Exports", exports.length, el("div", { class: "bp-list tight" }, exports.map((x) => this.exporter(x)))));
    if (pyTools.length) parts.push(section("Python tools", pyTools.length, el("div", { class: "bp-list" }, pyTools.map((t) => this.pyFile(t))),
      check.notes?.length ? el("ul", { class: "bp-notes" }, check.notes.map((n) => el("li", { text: n }))) : null));
    else if (check.notes?.length) parts.push(section("Notes", 0, el("ul", { class: "bp-notes" }, check.notes.map((n) => el("li", { text: n })))));
    const perms = m.permissions || {};
    parts.push(section("Permissions", 0, permChips({ network: !!perms.network, commands: !!perms.commands, files: perms.files || "data" })));
    if (files.length) parts.push(section("Files", files.length, el("ul", { class: "bp-files" },
      files.map((f) => el("li", {}, el("code", { text: f.path }), el("span", { class: "note", text: formatSize(f.size) }))))));
    this.node.replaceChildren(...parts);
  }

  // Two separate blocks: "Agent todo" (not written yet: neutral) and "problems" (wrong: warning).
  status(check) {
    const todo = check.todo || [];
    const problems = check.problems || [];
    if (check.ok && !todo.length && !problems.length) {
      return el("div", { class: "bp-status ok", role: "status" }, icon("check", 16), el("strong", { text: "Ready to install" }),
        this.busy ? el("span", { class: "note", text: "The builder is still working" }) : null);
    }
    const block = (kind, iconName, title, hint, items, open, remember) => {
      const details = el("details", { class: `bp-status ${kind}`, open });
      details.addEventListener("toggle", () => remember(details.open));
      details.append(el("summary", {}, icon(iconName, 16), el("strong", { text: title }), el("span", { class: "note", text: hint })),
        el("ul", { class: kind === "todo" ? "bp-tasks" : "" }, items.map((t) => el("li", { text: t }))));
      return details;
    };
    return el("div", { class: "bp-statuses" },
      todo.length ? block("todo", "info", `Agent todo: ${todo.length}`, "The builder has not done these yet", todo,
        this.todoOpen, (v) => { this.todoOpen = v; }) : null,
      problems.length ? block("bad", "alert", `${problems.length} problem${problems.length === 1 ? "" : "s"}`,
        "Must be fixed before install", problems, this.problemsOpen, (v) => { this.problemsOpen = v; }) : null);
  }

  planning() {
    return el("div", { class: "bp-planning", role: "status" },
      el("div", { class: "bp-skel", "aria-hidden": "true" }, el("i"), el("i"), el("i")),
      el("p", { class: "note" }, el("span", { class: "spin on" }), " The builder is planning…"));
  }

  action(a) {
    const inputs = (a.inputs || []).map((i) => chip(i.label || pretty(i.key), i.required ? "req" : "", aicon("pen", 11)));
    return el("div", { class: "bp-card" },
      el("span", { class: "aws-action-icon" }, aicon(a.icon || "play", 16)),
      el("div", { class: "bp-card-text" },
        el("strong", { text: a.label || a.id }),
        a.description ? el("span", { class: "note", text: a.description }) : null,
        inputs.length ? el("div", { class: "bp-chips", "aria-label": "Inputs" }, inputs) : null));
  }

  table(t) {
    const fields = (t.fields || []).map((f) => el("span", { class: "pchip" }, f.label || f.name, el("span", { class: "ptype", text: f.type || "text" })));
    return el("div", { class: "bp-card block" },
      el("strong", { text: t.label || pretty(t.name) }),
      el("div", { class: "bp-chips", "aria-label": `${t.label || t.name} fields` }, fields));
  }

  setting(s) {
    const secret = s.type === "secret";
    return el("div", { class: "bp-row" },
      secret ? el("span", { class: "bp-lock", title: "Secret: stored in the keystore" }, aicon("lock", 14), el("span", { class: "sr-only", text: "Secret" })) : aicon("sliders", 14),
      el("span", { class: "bp-row-main", text: s.label || pretty(s.key) }),
      s.required ? el("span", { class: "badge warn", text: "required" }) : null,
      el("span", { class: "ptype", text: s.type || "text" }));
  }

  exporter(x) {
    return el("div", { class: "bp-row" }, aicon("download", 14), el("span", { class: "bp-row-main", text: x.label || x.id }),
      el("span", { class: "ptype", text: (x.format || "csv").toUpperCase() }));
  }

  pyFile(t) {
    const path = t.file.includes("/") ? t.file : `tools/${t.file}`;
    const box = el("div", { class: "bp-card block" });
    const pre = el("pre", { class: "bp-code", tabindex: "0", "aria-label": `Code of ${t.file}` });
    const toggle = el("button", { type: "button", class: "small", "aria-expanded": String(this.openCode.has(path)) }, aicon("code", 13), this.openCode.has(path) ? "Hide code" : "View code");
    const paint = async () => {
      const open = this.openCode.has(path);
      toggle.setAttribute("aria-expanded", String(open));
      toggle.lastChild.textContent = open ? "Hide code" : "View code";
      pre.hidden = !open;
      if (!open) return;
      if (!this.code.has(path)) {
        pre.textContent = "Loading…";
        try {
          const out = await request(`/agent-drafts/${encodeURIComponent(this.draftId)}/file?path=${encodeURIComponent(path)}`);
          this.code.set(path, { text: out.content });
        } catch (error) { this.code.set(path, { text: `Could not load the file: ${error.message}` }); }
      }
      pre.textContent = this.code.get(path).text;
    };
    toggle.addEventListener("click", () => { if (this.openCode.has(path)) this.openCode.delete(path); else this.openCode.add(path); paint(); });
    const uses = (t.uses || []).map((u) => chip(u, "use"));
    box.append(el("div", { class: "bp-file-head" }, el("code", { text: t.file }), toggle),
      el("div", { class: "bp-chips", "aria-label": "Tools" }, (t.tools || []).map((name) => chip(name, "tool", aicon("zap", 11)))),
      uses.length ? el("div", { class: "bp-uses" }, el("span", { class: "note", text: "Uses" }), uses) : null,
      pre);
    paint();
    return box;
  }
}

function formatSize(n) {
  if (!Number.isFinite(n)) return "";
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}
