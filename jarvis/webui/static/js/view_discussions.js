// Discussions view: list (#/discussions), create form (#/discussions/new) and room (#/discussions/<id>).
import { api } from "./api.js";
import { confirmBox, el } from "./dom.js";
import { avatarStack, ico, MODE_LABEL, statusChip, timeAgo } from "./discussion_common.js";
import { mountForm } from "./discussion_form.js";
import { mountRoom } from "./discussion_room.js";
import { icon } from "./icons.js";
import { registerView } from "./views.js";
import { emptyState, pageHeader } from "./view_parts.js";

let stop = null; // cleanup of whatever is mounted

function templateCards(templates, onPick) {
  return el("div", { class: "d-templates", role: "list", "aria-label": "Templates" }, templates.map((t) =>
    el("button", { type: "button", class: "d-template", role: "listitem", onclick: () => onPick(t) },
      el("span", { class: "d-template-icon" }, icon(t.icon || "sparkle", 18)),
      el("span", { class: "d-template-text" }, el("strong", { text: t.title }), el("span", { class: "note", text: t.text })))));
}

function card(d, open, remove) {
  const pct = d.items_total ? Math.round((d.items_done / d.items_total) * 100) : 0;
  const del = el("button", { type: "button", class: "icon-btn d-card-del", "aria-label": `Delete ${d.title}`, title: "Delete",
    onclick: (e) => { e.stopPropagation(); remove(d); } }, icon("trash", 15));
  const node = el("article", { class: "d-card", tabindex: "0", role: "link", "aria-label": `${d.title}, ${d.status}`,
    onclick: () => open(d),
    onkeydown: (e) => { if (e.target === node && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(d); } } },
    el("div", { class: "d-card-top" }, el("h3", { text: d.title }), statusChip(d.status), del),
    el("p", { class: "d-goal", text: d.goal }),
    el("div", { class: "d-card-foot" },
      avatarStack(d.participants),
      el("span", { class: "badge", text: MODE_LABEL[d.mode] || d.mode }),
      el("span", { class: "d-progress-text", text: `${d.items_done}/${d.items_total} items` }),
      el("span", { class: "d-ago note", text: timeAgo(d.updated) })),
    el("div", { class: "d-bar", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Agenda progress" },
      el("span")));
  node.querySelector(".d-bar span").style.width = `${pct}%`;  // CSP forbids inline style attributes
  return node;
}

function mountList(container, ctx) {
  ctx.setTitle("Discussions");
  const body = el("div", { class: "view-body" });
  const create = el("button", { type: "button", class: "primary", onclick: () => ctx.navigate("discussions/new") }, ico("plus", 16), "New discussion");
  container.append(pageHeader("Discussions", "Agenda-driven group chats between your agents.", create), body);
  let meta = null;
  let destroyed = false;

  const useTemplate = (t) => { sessionStorageSet(t); ctx.navigate("discussions/new"); };

  async function load() {
    try {
      const [list, info] = await Promise.all([api("/discussions"), meta ? meta : api("/discussions/meta")]);
      meta = info;
      if (destroyed) return;
      draw(list.discussions);
    } catch (error) {
      if (destroyed) return;
      body.replaceChildren(el("div", { class: "error-banner", role: "alert" }, el("div", { class: "error-head" }, icon("alert", 16), el("strong", { text: "Could not load discussions" })),
        el("p", { class: "error-text", text: error.message }),
        el("div", { class: "error-actions" }, el("button", { type: "button", class: "small", text: "Retry", onclick: load }))));
    }
  }

  function draw(list) {
    if (!list.length) {
      const empty = emptyState("discussions", "No discussions yet",
        "Put several agents in one room with an agenda. They take turns, research, and write the minutes, and you can step in at any time.");
      empty.append(el("h4", { class: "d-subhead", text: "Start from a template" }), templateCards(meta.templates, useTemplate),
        el("button", { type: "button", class: "ghost", onclick: () => ctx.navigate("discussions/new") }, "or start from scratch"));
      body.replaceChildren(empty);
      return;
    }
    body.replaceChildren(
      el("div", { class: "d-grid" }, list.map((d) => card(d, (x) => ctx.navigate(`discussions/${x.id}`), async (x) => {
        if (await confirmBox("Delete discussion", `Delete “${x.title}” and its transcript? This cannot be undone.`)) {
          try { await api(`/discussions/${x.id}`, { method: "DELETE" }); ctx.toast("Discussion deleted"); load(); }
          catch (error) { ctx.toast(error.message, "error"); }
        }
      }))),
      el("h4", { class: "d-subhead", text: "Templates" }), templateCards(meta.templates, useTemplate));
  }

  let timer = 0;
  const off = ctx.bus.on((msg) => {
    if (msg.kind?.startsWith("discussion_") && ["discussion_status", "discussion_item", "discussion_minutes"].includes(msg.kind)) {
      clearTimeout(timer);
      timer = setTimeout(load, 300);
    }
  });
  stop = () => { destroyed = true; off(); clearTimeout(timer); };
  load();
}

// A template chosen on the list is handed to the form through sessionStorage (survives the route change only).
function sessionStorageSet(t) { try { sessionStorage.setItem("discussion-template", t.id); } catch { /* private mode */ } }
function sessionStorageTake() {
  try { const id = sessionStorage.getItem("discussion-template"); sessionStorage.removeItem("discussion-template"); return id; }
  catch { return null; }
}

registerView("discussions", {
  mount(container, params, ctx) {
    stop?.();
    stop = null;
    if (params.id === "new") stop = mountForm(container, ctx, sessionStorageTake()) || null;
    else if (params.id) stop = mountRoom(container, params.id, ctx) || null;
    else mountList(container, ctx);
  },
  unmount() { stop?.(); stop = null; document.getElementById("view")?.classList.remove("d-room-host"); },
});
