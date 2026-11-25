// Form fields rendered from a manifest's input/setting specs:
// {key, label, type, required, help, placeholder, options, default, min, max}.
// Each returns { node, get(), set(value), error(text), focus() }.
import { Select } from "./combo.js";
import { el } from "./dom.js";

let uid = 0;

export function formField(spec, value, { onEnter } = {}) {
  const id = `af-${++uid}`;
  const errorNode = el("p", { class: "af-error", role: "alert", hidden: true });
  const help = spec.help ? el("p", { class: "help", id: `${id}-help`, text: spec.help }) : null;
  const label = el("label", { for: id }, el("span", { text: spec.label || spec.key }),
    spec.required ? el("span", { class: "af-req", "aria-hidden": "true", text: " *" }) : null);
  const describedBy = help ? `${id}-help` : null;
  let control;
  let get;
  let set;
  const current = value ?? spec.default ?? (spec.type === "multiselect" ? [] : "");

  if (spec.type === "textarea") {
    control = el("textarea", { id, rows: 3, placeholder: spec.placeholder || null, "aria-describedby": describedBy });
    control.value = current ?? "";
    get = () => control.value;
    set = (v) => { control.value = v ?? ""; };
    control.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); onEnter?.(); } });
  } else if (spec.type === "boolean") {
    control = el("input", { id, type: "checkbox", "aria-describedby": describedBy });
    control.checked = !!current;
    get = () => control.checked;
    set = (v) => { control.checked = !!v; };
    const row = el("label", { class: "check af-check", for: id }, control, el("span", { text: spec.label || spec.key }));
    const node = el("div", { class: "form-field" }, row, help, errorNode);
    return wrap(node, control, get, set, errorNode);
  } else if (spec.type === "select") {
    const select = new Select({
      label: spec.label || spec.key,
      options: [...(spec.required ? [] : [{ value: "", label: "—" }]), ...spec.options.map((o) => ({ value: o, label: pretty(o) }))],
      value: current || "",
    });
    control = select.button;
    control.id = id;
    get = () => select.value;
    set = (v) => select.setValue(v ?? "");
    const node = el("div", { class: "form-field" }, label, select.node, help, errorNode);
    return wrap(node, control, get, set, errorNode);
  } else if (spec.type === "multiselect") {
    const chosen = new Set(Array.isArray(current) ? current : []);
    const boxes = spec.options.map((o) => {
      const box = el("input", { type: "checkbox", value: o });
      box.checked = chosen.has(o);
      return { o, box, node: el("label", { class: "af-choice" }, box, el("span", { text: pretty(o) })) };
    });
    control = boxes[0]?.box || el("span");
    get = () => boxes.filter((b) => b.box.checked).map((b) => b.o);
    set = (v) => { const s = new Set(v || []); boxes.forEach((b) => { b.box.checked = s.has(b.o); }); };
    const node = el("fieldset", { class: "form-field af-multi" }, el("legend", { text: spec.label || spec.key }),
      el("div", { class: "af-choices" }, boxes.map((b) => b.node)), help, errorNode);
    return wrap(node, control, get, set, errorNode);
  } else {
    const type = spec.type === "number" ? "number" : spec.type === "url" ? "url" : "text";
    control = el("input", { id, type, placeholder: spec.placeholder || null, "aria-describedby": describedBy,
      min: spec.min ?? null, max: spec.max ?? null, step: spec.type === "number" ? "any" : null, inputmode: type === "number" ? "decimal" : null });
    control.value = current ?? "";
    get = () => (spec.type === "number" ? (control.value === "" ? "" : Number(control.value)) : control.value);
    set = (v) => { control.value = v ?? ""; };
    control.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); onEnter?.(); } });
  }
  const node = el("div", { class: "form-field" }, label, control, help, errorNode);
  return wrap(node, control, get, set, errorNode);
}

function wrap(node, control, get, set, errorNode) {
  return {
    node, get, set, control,
    error(text) {
      errorNode.hidden = !text;
      errorNode.textContent = text ? `${text.charAt(0).toUpperCase()}${text.slice(1)}` : "";
      node.classList.toggle("invalid", !!text);
      if (text) control.setAttribute("aria-invalid", "true"); else control.removeAttribute("aria-invalid");
    },
    focus() { control.focus?.(); },
  };
}

// "call_link_sent" -> "Call link sent"
export function pretty(text) {
  const s = String(text ?? "").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Show {key: message} errors on fields; returns true when any were shown.
export function showErrors(fields, errors) {
  let first = null;
  for (const [key, field] of Object.entries(fields)) {
    const message = errors?.[key] || "";
    field.error(message);
    if (message && !first) first = field;
  }
  first?.focus();
  return !!first;
}

// fetch helper that keeps the server's extra error data ({error, fields, issues}).
export async function request(path, { method = "GET", body, form } = {}) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (form) options.body = form;
  else if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  let response;
  try { response = await fetch(`/api${path}`, options); }
  catch { throw Object.assign(new Error("Cannot reach the Jarvis server"), { status: 0, data: {} }); }
  let data = {};
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) throw Object.assign(new Error(data.error || `Request failed (${response.status})`), { status: response.status, data });
  return data;
}

// Start a file download from an API URL (the browser keeps the page).
export function download(path) {
  const link = el("a", { href: `/api${path}`, download: "" });
  document.body.append(link);
  link.click();
  link.remove();
}

// "3 min ago" style time.
export function ago(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(seconds)) return "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  if (seconds < 86400 * 30) return `${Math.round(seconds / 86400)} d ago`;
  return new Date(iso).toLocaleDateString();
}
