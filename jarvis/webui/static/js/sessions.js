// Left rail: session search, list and the per-session menu.
import { api } from "./api.js";
import { askText, confirmBox, el, toast } from "./dom.js";

function when(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export class SessionList {
  constructor(listNode, searchInput, ctx) {
    this.list = listNode;
    this.search = searchInput;
    this.ctx = ctx;
    this.timer = null;
    this.search.addEventListener("input", () => {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.refresh(), 250);
    });
    document.addEventListener("click", () => this.closeMenus());
  }

  closeMenus() {
    this.list.querySelectorAll(".menu").forEach((m) => m.remove());
  }

  async refresh() {
    try {
      const { sessions } = await api(`/sessions?q=${encodeURIComponent(this.search.value.trim())}`);
      this.list.replaceChildren(...(sessions.length ? sessions.map((s) => this.row(s))
        : [el("li", { class: "note empty", text: "No conversations" })]));
    } catch (error) { toast(error.message, "error"); }
  }

  row(session) {
    const open = el("button", {
      class: "session-open", "aria-current": session.active ? "true" : null,
      onclick: () => this.resume(session.id),
    }, el("span", { class: "session-title", text: session.title || "Conversation" }),
       el("span", { class: "session-time", text: when(session.updated) }));
    const more = el("button", { class: "icon-btn", "aria-label": `Actions for ${session.title}`, "aria-haspopup": "menu", text: "⋯" });
    more.addEventListener("click", (event) => {
      event.stopPropagation();
      const had = li.querySelector(".menu");
      this.closeMenus();
      if (!had) li.append(this.menu(session));
    });
    const li = el("li", { class: `session${session.active ? " active" : ""}` }, open, more);
    return li;
  }

  menu(session) {
    const item = (label, fn, cls = "") => el("button", { role: "menuitem", class: cls, text: label, onclick: fn });
    return el("div", { class: "menu", role: "menu" },
      item("Rename", () => this.rename(session)),
      item("Export Markdown", () => { window.location.href = `/api/sessions/${session.id}/export?format=markdown`; }),
      item("Export JSON", () => { window.location.href = `/api/sessions/${session.id}/export?format=json`; }),
      item("Delete", () => this.remove(session), "danger"));
  }

  async resume(id) {
    if (id === this.ctx.sessionId) return;
    try { await this.ctx.applySnapshot(await api(`/sessions/${id}/resume`, { method: "POST" })); }
    catch (error) { toast(error.message, "error"); }
    this.ctx.closeRail?.();
  }

  async rename(session) {
    const title = await askText("Rename conversation", session.title);
    if (title === null) return;
    try { await api(`/sessions/${session.id}`, { method: "PATCH", body: { title } }); this.ctx.refreshStatus(); this.refresh(); }
    catch (error) { toast(error.message, "error"); }
  }

  async remove(session) {
    if (!(await confirmBox("Delete conversation", `Delete “${session.title}”? This cannot be undone.`))) return;
    try {
      await api(`/sessions/${session.id}`, { method: "DELETE" });
      if (session.id === this.ctx.sessionId) await this.ctx.reloadSnapshot();
      this.refresh();
    } catch (error) { toast(error.message, "error"); }
  }
}
