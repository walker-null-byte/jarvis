// Small shared pieces for full-page views: page header and empty state.
import { el } from "./dom.js";
import { icon } from "./icons.js";

export function pageHeader(title, subtitle, ...actions) {
  return el("header", { class: "view-head" },
    el("div", { class: "view-head-text" }, el("h2", { text: title }), subtitle ? el("p", { class: "note", text: subtitle }) : null),
    actions.length ? el("div", { class: "view-actions" }, ...actions) : null);
}

export function emptyState(iconName, title, text) {
  return el("div", { class: "view-empty" },
    el("div", { class: "view-empty-icon" }, icon(iconName, 28)),
    el("h3", { text: title }),
    el("p", { class: "note", text }));
}
