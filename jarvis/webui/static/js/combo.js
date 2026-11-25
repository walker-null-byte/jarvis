// Themed, accessible form widgets: Select (listbox dropdown) and ComboBox (searchable text + listbox).
// Focus stays on the trigger/input; the highlighted row is announced through aria-activedescendant.
import { el } from "./dom.js";
import { icon } from "./icons.js";

let counter = 0;
const uid = (prefix) => `${prefix}-${++counter}`;

// Places a fixed-position popup under (or above, when short on room) its anchor.
function place(popup, anchor) {
  const rect = anchor.getBoundingClientRect();
  const room = { below: window.innerHeight - rect.bottom - 12, above: rect.top - 12 };
  const up = room.below < 200 && room.above > room.below;
  popup.style.left = `${rect.left}px`;
  popup.style.width = `${rect.width}px`;
  popup.style.maxHeight = `${Math.max(120, Math.min(560, up ? room.above : room.below))}px`;
  if (up) { popup.style.top = "auto"; popup.style.bottom = `${window.innerHeight - rect.top + 4}px`; }
  else { popup.style.bottom = "auto"; popup.style.top = `${rect.bottom + 4}px`; }
}

function closeOnOutside(root, close) {
  const handler = (e) => { if (!root.contains(e.target)) close(); };
  const away = (e) => { if (!root.contains(e.target)) close(); };  // a scrolling ancestor would strand the fixed popup
  document.addEventListener("pointerdown", handler, true);
  document.addEventListener("scroll", away, true);
  window.addEventListener("resize", close);
  return () => {
    document.removeEventListener("pointerdown", handler, true);
    document.removeEventListener("scroll", away, true);
    window.removeEventListener("resize", close);
  };
}

/** Grouped dropdown. options: [{value, label, sub?, group?}] */
export class Select {
  constructor({ label, options = [], value = "", onchange, placeholder = "Choose…" }) {
    this.id = uid("sel");
    this.options = options;
    this.value = value;
    this.onchange = onchange;
    this.placeholder = placeholder;
    this.query = "";
    this.index = -1;
    this.text = el("span", { class: "combo-value" });
    this.button = el("button", {
      type: "button", class: "combo-trigger", role: "combobox", "aria-haspopup": "listbox", "aria-expanded": "false",
      "aria-controls": `${this.id}-list`, "aria-label": label,
    }, this.text, icon("down", 16));
    this.list = el("div", { class: "combo-list", role: "listbox", id: `${this.id}-list`, "aria-label": label, hidden: true });
    this.node = el("div", { class: "combo" }, this.button, this.list);
    this.button.addEventListener("click", () => (this.isOpen ? this.close() : this.open()));
    this.button.addEventListener("keydown", (e) => this.key(e));
    this.button.addEventListener("blur", () => setTimeout(() => { if (this.isOpen && !this.node.contains(document.activeElement)) this.close(); }, 0));
    this.stop = closeOnOutside(this.node, () => this.close());
    this.paint();
  }

  get isOpen() { return !this.list.hidden; }
  get selected() { return this.options.find((o) => o.value === this.value); }

  setOptions(options) { this.options = options; this.paint(); }
  setValue(value) { this.value = value; this.paint(); }

  paint() {
    const chosen = this.selected;
    this.text.replaceChildren(chosen
      ? el("span", { class: "combo-name", text: chosen.label })
      : el("span", { class: "combo-name muted", text: this.placeholder }));
    if (chosen && chosen.sub) this.text.append(el("span", { class: "combo-sub", text: chosen.sub }));
    if (this.isOpen) this.drawList();
  }

  drawList() {
    const parts = [];
    let group = null;
    this.options.forEach((o, i) => {
      if (o.group && o.group !== group) {
        group = o.group;
        parts.push(el("div", { class: "combo-group", role: "presentation", text: group }));
      }
      parts.push(el("div", {
        class: `combo-option${o.value === this.value ? " selected" : ""}${i === this.index ? " hl" : ""}`,
        role: "option", id: `${this.id}-o${i}`, "aria-selected": String(o.value === this.value),
        onpointerdown: (e) => e.preventDefault(),  // keep focus on the trigger
        onclick: () => this.choose(i),
        onmousemove: () => { if (this.index !== i) this.highlight(i, false); },
      }, el("span", { class: "combo-text" },
        el("span", { class: "combo-name", text: o.label }),
        o.sub ? el("span", { class: "combo-sub", text: o.sub }) : null),
      o.value === this.value ? icon("check", 14) : null));
    });
    this.list.replaceChildren(...parts);
  }

  highlight(i, scroll = true) {
    this.index = i;
    this.list.querySelectorAll(".combo-option").forEach((n, k) => n.classList.toggle("hl", k === i));
    const node = this.list.querySelector(`#${this.id}-o${i}`);
    if (node) {
      this.button.setAttribute("aria-activedescendant", node.id);
      if (scroll) node.scrollIntoView({ block: "nearest" });
    }
  }

  open() {
    if (!this.options.length) return;
    this.index = Math.max(0, this.options.findIndex((o) => o.value === this.value));
    this.list.hidden = false;
    this.button.setAttribute("aria-expanded", "true");
    this.drawList();
    place(this.list, this.button);
    this.highlight(this.index);
  }

  close() {
    if (!this.isOpen) return;
    this.list.hidden = true;
    this.button.setAttribute("aria-expanded", "false");
    this.button.removeAttribute("aria-activedescendant");
  }

  choose(i) {
    const option = this.options[i];
    this.close();
    this.button.focus();
    if (!option || option.value === this.value) return;
    this.value = option.value;
    this.paint();
    this.onchange?.(option.value, option);
  }

  key(e) {
    const last = this.options.length - 1;
    if (!this.isOpen) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) { e.preventDefault(); this.open(); }
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) { this.open(); this.typeahead(e.key); }
      return;
    }
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); this.highlight(Math.min(last, this.index + 1)); break;
      case "ArrowUp": e.preventDefault(); this.highlight(Math.max(0, this.index - 1)); break;
      case "Home": e.preventDefault(); this.highlight(0); break;
      case "End": e.preventDefault(); this.highlight(last); break;
      case "Enter": case " ": e.preventDefault(); this.choose(this.index); break;
      case "Escape": e.preventDefault(); e.stopPropagation(); this.close(); break;
      case "Tab": this.close(); break;
      default: if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) this.typeahead(e.key);
    }
  }

  typeahead(char) {
    clearTimeout(this.queryTimer);
    this.query += char.toLowerCase();
    this.queryTimer = setTimeout(() => { this.query = ""; }, 700);
    const from = this.query.length === 1 ? this.index + 1 : this.index;
    const order = [...this.options.keys()];
    const hit = [...order.slice(from), ...order.slice(0, from)]
      .find((i) => this.options[i].label.toLowerCase().startsWith(this.query));
    if (hit !== undefined) this.highlight(hit);
  }
}

/** Free-text input with a filtered suggestion list. */
export class ComboBox {
  constructor({ label, placeholder = "", onchange, onfocus }) {
    this.id = uid("cb");
    this.items = [];
    this.state = { loading: false, error: "" };
    this.index = -1;
    this.onchange = onchange;
    this.touched = false;  // only filter once the user types; focusing shows the whole list
    this.input = el("input", {
      type: "text", class: "combo-input", role: "combobox", "aria-autocomplete": "list", "aria-expanded": "false",
      "aria-controls": `${this.id}-list`, "aria-label": label, placeholder, autocomplete: "off", spellcheck: "false",
    });
    this.toggle = el("button", { type: "button", class: "combo-toggle", tabindex: "-1", "aria-label": "Show models", title: "Show models" }, icon("down", 16));
    this.list = el("div", { class: "combo-list", role: "listbox", id: `${this.id}-list`, "aria-label": label, hidden: true });
    this.node = el("div", { class: "combo has-input" }, this.input, this.toggle, this.list);
    this.input.addEventListener("focus", () => { onfocus?.(); });
    this.input.addEventListener("click", () => { if (!this.isOpen) this.open(); });
    this.input.addEventListener("input", () => { this.touched = true; this.open(); this.onchange?.(this.input.value); });
    this.input.addEventListener("keydown", (e) => this.key(e));
    this.toggle.addEventListener("pointerdown", (e) => e.preventDefault());
    this.toggle.addEventListener("click", () => { if (this.isOpen) this.close(); else { this.input.focus(); this.touched = false; this.open(); } });
    this.stop = closeOnOutside(this.node, () => this.close());
    this.input.addEventListener("blur", () => setTimeout(() => { if (!this.node.contains(document.activeElement)) this.close(); }, 0));
  }

  get isOpen() { return !this.list.hidden; }
  get value() { return this.input.value; }
  set value(v) { this.input.value = v; }

  setState(state) {
    this.state = { ...this.state, ...state };
    if (state.items) this.items = state.items;
    this.node.classList.toggle("loading", !!this.state.loading);
    if (this.isOpen) this.drawList();
  }

  shown() {
    const needle = this.touched ? this.input.value.trim().toLowerCase() : "";
    return this.items.filter((m) => !needle || m.toLowerCase().includes(needle));
  }

  drawList() {
    const shown = this.shown();
    const parts = shown.map((m, i) => el("div", {
      class: `combo-option${m === this.input.value ? " selected" : ""}${i === this.index ? " hl" : ""}`,
      role: "option", id: `${this.id}-o${i}`, "aria-selected": String(m === this.input.value),
      onpointerdown: (e) => e.preventDefault(), onclick: () => this.choose(m),
      onmousemove: () => { if (this.index !== i) this.highlight(i, false); },
    }, el("span", { class: "combo-text mono", text: m }), m === this.input.value ? icon("check", 14) : null));
    const { loading, error } = this.state;
    if (loading) parts.push(el("div", { class: "combo-note", role: "presentation" }, el("span", { class: "spin on" }), "Loading models…"));
    else if (error && !this.items.length) parts.push(el("div", { class: "combo-note error", role: "presentation", text: error }));
    else if (!this.items.length) parts.push(el("div", { class: "combo-note", role: "presentation", text: "No models listed. Type a model id." }));
    else if (!shown.length) parts.push(el("div", { class: "combo-note", role: "presentation", text: "No match. Your text will be used as the model id." }));
    this.list.replaceChildren(...parts);
    if (this.index >= shown.length) this.index = shown.length - 1;
  }

  open() {
    this.list.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
    this.index = -1;
    this.drawList();
    place(this.list, this.input);
  }

  close() {
    if (!this.isOpen) return;
    this.list.hidden = true;
    this.input.setAttribute("aria-expanded", "false");
    this.input.removeAttribute("aria-activedescendant");
    this.touched = false;
  }

  highlight(i, scroll = true) {
    this.index = i;
    this.list.querySelectorAll(".combo-option").forEach((n, k) => n.classList.toggle("hl", k === i));
    const node = this.list.querySelector(`#${this.id}-o${i}`);
    if (node) {
      this.input.setAttribute("aria-activedescendant", node.id);
      if (scroll) node.scrollIntoView({ block: "nearest" });
    }
  }

  choose(value) {
    this.input.value = value;
    this.close();
    this.input.focus();
    this.onchange?.(value);
  }

  key(e) {
    const count = this.shown().length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!this.isOpen) { this.open(); if (!count) return; }
      this.highlight(e.key === "ArrowDown" ? Math.min(count - 1, this.index + 1) : Math.max(0, this.index - 1));
    } else if (e.key === "Enter" && this.isOpen && this.index >= 0) {
      e.preventDefault();
      this.choose(this.shown()[this.index]);
    } else if (e.key === "Escape" && this.isOpen) {
      e.preventDefault(); e.stopPropagation(); this.close();
    } else if (e.key === "Tab") this.close();
  }
}
