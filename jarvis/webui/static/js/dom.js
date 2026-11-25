// Tiny DOM helpers. Nothing here ever parses HTML: text always goes through textContent.

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

import { icon } from "./icons.js";

// Icon-only button: aria-label + native tooltip.
export function iconBtn(name, label, onclick, cls = "", size = 16) {
  return el("button", { type: "button", class: `icon-btn ${cls}`.trim(), "aria-label": label, title: label, onclick }, icon(name, size));
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function formatJson(value, limit = 4000) {
  let text;
  try { text = typeof value === "string" ? value : JSON.stringify(value, null, 2); }
  catch { text = String(value); }
  return text.length > limit ? text.slice(0, limit) + "\n… (truncated)" : text;
}

// ---- dialogs (built with <dialog>; styled in app.css) ----

function modal(title, body, buttons) {
  return new Promise((resolve) => {
    const dialog = el("dialog", { class: "modal", "aria-label": title });
    const finish = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.append(el("h3", { text: title }), body,
      el("div", { class: "modal-actions" }, buttons(finish)));
    dialog.addEventListener("cancel", () => finish(null));
    document.body.append(dialog);
    dialog.showModal();
    (dialog.querySelector("input,textarea") || dialog.querySelector("button.primary"))?.focus();
  });
}

export function askText(title, initial = "") {
  const input = el("input", { type: "text", value: initial, class: "modal-input" });
  return modal(title, input, (finish) => {
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(input.value); });
    return [el("button", { text: "Cancel", onclick: () => finish(null) }),
            el("button", { class: "primary", text: "OK", onclick: () => finish(input.value) })];
  });
}

export function confirmBox(title, message) {
  return modal(title, el("p", { text: message }), (finish) => [
    el("button", { text: "Cancel", onclick: () => finish(false) }),
    el("button", { class: "primary danger", text: "Confirm", onclick: () => finish(true) }),
  ]);
}

export function toast(message, kind = "info") {
  const area = document.getElementById("toasts");
  const node = el("div", { class: `toast ${kind}`, role: "status", text: message });
  area.append(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
}
