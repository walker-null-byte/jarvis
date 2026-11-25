// Right drawer: tab strip plus the Activity panel. Other panels live in panel_*.js
// and are registered by app.js; each exports render(container, ctx) -> optional {refresh}.
import { el, iconBtn } from "./dom.js";
import { icon } from "./icons.js";

const QUIET_KINDS = new Set([
  "voice_amplitude", "assistant_delta", "assistant_reasoning_delta", "snapshot", "busy_changed",
]);
const MAX_LOG = 400;

export class Drawer {
  constructor(root, tabs, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.tabs = tabs;
    this.views = new Map();
    this.buttons = new Map();
    const strip = el("div", { class: "tabs", role: "tablist", "aria-label": "Panels" });
    this.body = el("div", { class: "tab-body" });
    for (const tab of tabs) {
      const button = el("button", {
        class: "tab", role: "tab", id: `tab-${tab.id}`, "aria-controls": "tab-body",
        onclick: () => this.show(tab.id), title: tab.label, "aria-label": tab.label,
      }, tab.icon ? icon(tab.icon, 15) : null, el("span", { class: "tab-label", text: tab.label }));
      button.addEventListener("keydown", (e) => this.arrowKeys(e, tab.id));
      this.buttons.set(tab.id, button);
      strip.append(button);
    }
    const head = el("div", { class: "drawer-head" }, strip,
      iconBtn("x", "Close side panel", () => ctx.closeDrawer?.(), "narrow-only"));
    root.append(head, this.body);
    this.body.id = "tab-body";
    this.body.setAttribute("role", "tabpanel");
  }

  arrowKeys(event, id) {
    const index = this.tabs.findIndex((t) => t.id === id);
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    const next = this.tabs[(index + step + this.tabs.length) % this.tabs.length];
    this.show(next.id);
    this.buttons.get(next.id).focus();
  }

  show(id) {
    this.active = id;
    try { localStorage.setItem("jarvis.tab", id); } catch { /* storage blocked */ }
    for (const [key, button] of this.buttons) {
      button.classList.toggle("active", key === id);
      button.setAttribute("aria-selected", String(key === id));
      button.tabIndex = key === id ? 0 : -1;
    }
    for (const [key, view] of this.views) view.node.hidden = key !== id;
    if (!this.views.has(id)) {
      const tab = this.tabs.find((t) => t.id === id);
      const node = el("section", { class: "panel", "aria-labelledby": `tab-${id}` });
      this.body.append(node);
      this.views.set(id, { node, api: tab.render(node, this.ctx) || {} });
    } else {
      this.views.get(id).api.refresh?.();
    }
  }

  // Panels may react to live events (e.g. memory changes) even when hidden.
  notify(msg) {
    for (const view of this.views.values()) view.api.onEvent?.(msg);
  }
}

// ---------- Activity ----------

const FILTERS = {
  all: () => true,
  tools: (m) => m.kind.startsWith("tool_") || m.kind === "artifact_created",
  turns: (m) => ["turn_started", "turn_finished", "user_message", "assistant_message", "cancel_requested"].includes(m.kind),
  approvals: (m) => m.kind.startsWith("approval_"),
  errors: (m) => m.kind === "error",
};

function summarize(msg) {
  const d = msg.data || {};
  switch (msg.kind) {
    case "tool_started": return `${d.name} ${JSON.stringify(d.args || {}).slice(0, 120)}`;
    case "tool_finished": return `${d.name} → ${String(d.result ?? "").slice(0, 120)}`;
    case "turn_finished": return d.status;
    case "error": return d.message;
    case "approval_requested": return `${d.action}`;
    case "approval_finished": return d.approved ? "approved" : "denied";
    case "user_message": case "assistant_message": return String(d.content || "").slice(0, 120);
    case "config_changed": return `${d.section || ""} ${d.key || ""}`;
    case "character_changed": return d.character?.name || "";
    case "session_started": return msg.session_id;
    default: return "";
  }
}

export function renderActivity(container) {
  const entries = [];
  const list = el("ol", { class: "activity-list", "aria-live": "off" });
  const filter = el("select", { "aria-label": "Filter events" },
    Object.keys(FILTERS).map((name) => el("option", { value: name, text: name })));
  const search = el("input", { type: "search", placeholder: "Filter text…", "aria-label": "Filter text" });
  const clearButton = el("button", { text: "Clear", onclick: () => { entries.length = 0; redraw(); } });
  container.append(el("div", { class: "panel-tools" }, filter, search, clearButton), list);

  function visible(msg) {
    const needle = search.value.trim().toLowerCase();
    return FILTERS[filter.value](msg) && (!needle || `${msg.kind} ${summarize(msg)}`.toLowerCase().includes(needle));
  }

  function row(msg) {
    const time = (msg.timestamp || "").slice(11, 19);
    return el("li", { class: `activity-row kind-${msg.kind}` },
      el("span", { class: "act-time", text: time }),
      el("span", { class: "act-kind", text: msg.kind }),
      el("span", { class: "act-text", text: summarize(msg) }));
  }

  function redraw() {
    list.replaceChildren(...entries.filter(visible).slice(-MAX_LOG).map(row));
    list.scrollTop = list.scrollHeight;
  }
  filter.addEventListener("change", redraw);
  search.addEventListener("input", redraw);

  return {
    onEvent(msg) {
      if (QUIET_KINDS.has(msg.kind)) return;
      entries.push(msg);
      if (entries.length > MAX_LOG * 2) entries.splice(0, MAX_LOG);
      if (visible(msg)) {
        list.append(row(msg));
        while (list.children.length > MAX_LOG) list.firstChild.remove();
        list.scrollTop = list.scrollHeight;
      }
    },
  };
}
