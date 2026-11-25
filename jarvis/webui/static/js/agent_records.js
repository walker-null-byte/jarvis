// Records table for one agent table: search, status filter chips, sortable columns,
// inline status editing, expandable rows with every field, and a row menu.
import { Select } from "./combo.js";
import { el, iconBtn } from "./dom.js";
import { icon } from "./icons.js";
import { aicon } from "./agent_icons.js";
import { ago, pretty, request } from "./agent_forms.js";

// Pipeline colours by stage name; anything else is coloured by its position.
const TONES = { new: "t-new", contacted: "t-blue", replied: "t-amber", call_link_sent: "t-violet", booked: "t-green",
  done: "t-green", won: "t-green", not_interested: "t-red", lost: "t-red", rejected: "t-red" };
const ORDER = ["t-new", "t-blue", "t-amber", "t-violet", "t-green", "t-red"];
export function toneFor(spec, value) {
  if (TONES[value]) return TONES[value];
  const index = Math.max(0, (spec?.options || []).indexOf(value));
  return ORDER[index % ORDER.length];
}

export class RecordsView {
  constructor({ agentId, table, toast, onEmptyAction, emptyActionLabel }) {
    this.agentId = agentId;
    this.spec = table;
    this.toast = toast;
    this.records = [];
    this.query = "";
    this.filter = "";
    this.sort = { key: "", dir: 1 };
    this.open = new Set();
    this.known = null; // ids seen before the last refresh, to highlight new rows
    this.fields = Object.fromEntries(table.fields.map((f) => [f.name, f]));
    this.status = table.status_field ? this.fields[table.status_field] : null;
    this.onEmptyAction = onEmptyAction;
    this.emptyActionLabel = emptyActionLabel;

    this.search = el("input", { type: "search", class: "rec-search", placeholder: `Search ${table.label.toLowerCase()}`, "aria-label": `Search ${table.label}` });
    this.search.addEventListener("input", () => { this.query = this.search.value.trim().toLowerCase(); this.render(); });
    this.chips = el("div", { class: "rec-chips", role: "group", "aria-label": "Filter by status" });
    this.count = el("span", { class: "rec-count" });
    this.body = el("div", { class: "rec-body" });
    this.node = el("section", { class: "rec", "aria-label": table.label },
      el("div", { class: "rec-tools" }, this.search, this.chips), this.body);
  }

  // ---------- columns ----------

  columns() {
    const f = this.spec.fields.filter((x) => !x.hidden);
    const title = this.fields[this.spec.title_field] || f[0];
    const subtitle = f.find((x) => x.type === "text" && x !== title && !x.editable);
    const cols = [{ key: title.name, label: title.label, kind: "title", field: title, subtitle }];
    for (const x of f) {
      if (x === title || x === subtitle || x === this.status) continue;
      if (x.type === "select" || x.type === "number") cols.push({ key: x.name, label: x.label, kind: x.type, field: x });
    }
    const links = f.filter((x) => x.type === "url" || x.type === "email" || (x.type === "text" && /phone/.test(x.name)));
    if (links.length) cols.push({ key: "_links", label: "Links", kind: "links", fields: links });
    if (this.status) cols.push({ key: this.status.name, label: this.status.label, kind: "status", field: this.status });
    return cols;
  }

  // ---------- data ----------

  async refresh(highlight = false) {
    const active = document.activeElement;
    if (active && this.body.contains(active) && active.tagName === "TEXTAREA") {
      // Don't re-render under someone typing a note; try again when they leave the field.
      active.addEventListener("blur", () => setTimeout(() => this.refresh(highlight), 50), { once: true });
      return;
    }
    try {
      const data = await request(`/agents/${encodeURIComponent(this.agentId)}/records/${encodeURIComponent(this.spec.name)}`);
      const before = new Set(this.records.map((r) => r.id));
      this.fresh = highlight ? new Set(data.records.filter((r) => !before.has(r.id)).map((r) => r.id)) : new Set();
      this.records = data.records;
      this.loaded = true;
      this.render();
      this.onCount?.(this.records.length);
    } catch (error) {
      this.body.replaceChildren(el("p", { class: "agent-error", role: "alert", text: error.message }));
    }
  }

  visible() {
    let rows = this.records;
    if (this.filter && this.status) rows = rows.filter((r) => (r[this.status.name] || "") === this.filter);
    if (this.query) rows = rows.filter((r) => JSON.stringify(r).toLowerCase().includes(this.query));
    const { key, dir } = this.sort;
    if (key) {
      const spec = this.fields[key];
      const rank = (r) => {
        const v = r[key];
        if (spec?.type === "number") return v ?? -Infinity;
        if (spec?.type === "select") return spec.options.indexOf(v);
        return String(v ?? "").toLowerCase();
      };
      rows = [...rows].sort((a, b) => (rank(a) > rank(b) ? dir : rank(a) < rank(b) ? -dir : 0));
    }
    return rows;
  }

  // ---------- rendering ----------

  render() {
    this.renderChips();
    if (!this.loaded) { this.body.replaceChildren(el("div", { class: "rec-empty note", text: "Loading…" })); return; }
    if (!this.records.length) {
      this.body.replaceChildren(el("div", { class: "rec-empty" },
        aicon("filter", 22),
        el("strong", { text: `No ${this.spec.label.toLowerCase()} yet` }),
        el("span", { class: "note", text: this.emptyActionLabel ? `Run ${this.emptyActionLabel}: new ones appear here live while the agent works.` : "The agent fills this table as it works." }),
        this.emptyActionLabel ? el("button", { type: "button", class: "small", onclick: () => this.onEmptyAction?.() }, aicon("play", 13), this.emptyActionLabel) : null));
      return;
    }
    const rows = this.visible();
    const cols = this.columns();
    const head = el("tr", {}, cols.map((c) => {
      const sortable = c.kind !== "links";
      const active = this.sort.key === c.key;
      const th = el("th", { scope: "col", class: `c-${c.kind}`, "aria-sort": active ? (this.sort.dir > 0 ? "ascending" : "descending") : null });
      th.append(sortable ? el("button", { type: "button", class: "th-sort", onclick: () => this.toggleSort(c.key) },
        c.label, active ? icon(this.sort.dir > 0 ? "down" : "send", 12) : null) : el("span", { text: c.label }));
      return th;
    }), el("th", { scope: "col", class: "c-menu" }, el("span", { class: "sr-only", text: "Actions" })));
    const body = el("tbody");
    for (const record of rows) {
      body.append(this.row(record, cols));
      if (this.open.has(record.id)) body.append(this.detail(record, cols.length + 1));
    }
    const table = el("table", { class: "rec-table" }, el("thead", {}, head), body);
    this.body.replaceChildren(rows.length ? el("div", { class: "rec-scroll" }, table)
      : el("div", { class: "rec-empty note", text: "Nothing matches the search or filter." }));
  }

  renderChips() {
    if (!this.status) { this.chips.hidden = true; return; }
    const counts = {};
    for (const r of this.records) counts[r[this.status.name] || ""] = (counts[r[this.status.name] || ""] || 0) + 1;
    const chip = (value, label, n) => el("button", {
      type: "button", class: `rec-chip ${value ? toneFor(this.status, value) : ""}${this.filter === value ? " on" : ""}`,
      "aria-pressed": String(this.filter === value), onclick: () => { this.filter = this.filter === value && value ? "" : value; this.render(); },
    }, label, el("span", { class: "rec-chip-n", text: String(n) }));
    this.chips.replaceChildren(chip("", "All", this.records.length),
      ...this.status.options.filter((o) => counts[o]).map((o) => chip(o, pretty(o), counts[o])));
  }

  toggleSort(key) {
    this.sort = this.sort.key === key ? (this.sort.dir > 0 ? { key, dir: -1 } : { key: "", dir: 1 }) : { key, dir: key === this.spec.title_field ? 1 : -1 };
    this.render();
  }

  row(record, cols) {
    const expanded = this.open.has(record.id);
    const tr = el("tr", { class: `rec-row${expanded ? " open" : ""}${this.fresh?.has(record.id) ? " fresh" : ""}`, dataset: { id: record.id } });
    for (const c of cols) tr.append(this.cell(record, c));
    tr.append(el("td", { class: "c-menu" }, this.menuButton(record)));
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button, a, .combo, input, select, textarea")) return;
      this.toggle(record.id);
    });
    return tr;
  }

  toggle(id) {
    if (this.open.has(id)) this.open.delete(id); else this.open.add(id);
    this.render();
  }

  cell(record, c) {
    const value = record[c.key];
    if (c.kind === "title") {
      const expanded = this.open.has(record.id);
      return el("td", { class: "c-title" },
        el("button", { type: "button", class: "rec-title", "aria-expanded": String(expanded), onclick: () => this.toggle(record.id) },
          icon(expanded ? "down" : "right", 13), el("span", { text: value || "(untitled)" })),
        c.subtitle && record[c.subtitle.name] ? el("div", { class: "rec-sub", text: record[c.subtitle.name] }) : null);
    }
    if (c.kind === "number") {
      const max = c.field.max;
      return el("td", { class: "c-number" }, value === undefined ? el("span", { class: "muted", text: "–" })
        : el("span", { class: `score${max && value >= max * 0.8 ? " hi" : max && value <= max * 0.4 ? " lo" : ""}`, text: max ? `${value}/${max}` : String(value) }));
    }
    if (c.kind === "select") return el("td", { class: "c-select" }, value ? el("span", { class: "badge", text: value }) : null);
    if (c.kind === "links") {
      const links = c.fields.filter((f) => record[f.name]).map((f) => {
        const v = String(record[f.name]);
        if (f.type === "email") return el("a", { class: "rec-link", href: `mailto:${v}`, title: `${f.label}: ${v}`, "aria-label": `${f.label}: ${v}` }, aicon("mail", 15));
        if (f.type !== "url") return el("a", { class: "rec-link", href: `tel:${v.replace(/[^\d+]/g, "")}`, title: `${f.label}: ${v}`, "aria-label": `${f.label}: ${v}` }, aicon("phone", 15));
        return el("a", { class: "rec-link", href: v, target: "_blank", rel: "noopener noreferrer", title: `${f.label}: ${v}`, "aria-label": `${f.label}: ${v}` }, aicon(/profile/.test(f.name) ? "external" : "globe", 15));
      });
      return el("td", { class: "c-links" }, links.length ? links : el("span", { class: "muted", text: "–" }));
    }
    if (c.kind === "status") {
      const select = new Select({
        label: `${c.field.label} of ${record[this.spec.title_field] || "record"}`,
        options: c.field.options.map((o) => ({ value: o, label: pretty(o) })),
        value: value || "",
        onchange: (v) => this.patch(record, { [c.key]: v }),
      });
      select.node.classList.add("status-select", toneFor(c.field, value));
      return el("td", { class: "c-status" }, select.node);
    }
    return el("td", { text: value ?? "" });
  }

  detail(record, span) {
    const items = [];
    const shown = new Set(this.columns().filter((c) => c.kind === "select" || c.kind === "number").map((c) => c.key));
    for (const f of this.spec.fields) {
      const v = record[f.name];
      if (f.name === this.spec.title_field || f === this.status || shown.has(f.name)) continue;
      if (f.editable && (f.type === "textarea" || f.type === "text")) { items.push(this.editableNote(record, f)); continue; }
      if (v === undefined || v === null || v === "") continue;
      let value;
      if (f.type === "url") value = el("a", { href: String(v), target: "_blank", rel: "noopener noreferrer", text: String(v) });
      else if (f.type === "email") value = el("a", { href: `mailto:${v}`, text: String(v) });
      else if (f.type === "boolean") value = el("span", { text: v ? "Yes" : "No" });
      else value = el("span", { class: f.type === "textarea" ? "rec-long" : "", text: String(v) });
      const copy = f.type === "textarea" ? iconBtn("copy", `Copy ${f.label}`, () => this.copy(String(v), f.label), "rec-copy", 14) : null;
      items.push(el("div", { class: `rec-field${f.type === "textarea" ? " wide" : ""}` },
        el("span", { class: "rec-key" }, f.label, copy), value));
    }
    items.push(el("div", { class: "rec-field meta" }, el("span", { class: "rec-key", text: "Added" }),
      el("span", { class: "note", text: `${ago(record.created)} · updated ${ago(record.updated)} · #${record.id}` })));
    return el("tr", { class: "rec-detail" }, el("td", { colspan: span }, el("div", { class: "rec-fields" }, items)));
  }

  editableNote(record, f) {
    const area = el("textarea", { rows: 2, "aria-label": f.label, placeholder: `Add ${f.label.toLowerCase()}…` });
    area.value = record[f.name] || "";
    area.addEventListener("change", () => this.patch(record, { [f.name]: area.value.trim() || null }));
    return el("div", { class: "rec-field wide" }, el("span", { class: "rec-key", text: f.label }), area);
  }

  menuButton(record) {
    const wrap = el("div", { class: "popover-wrap rec-menu-wrap" });
    const menu = el("div", { class: "menu rec-menu", role: "menu", hidden: true });
    const button = iconBtn("more", "Row actions", (e) => {
      e.stopPropagation();
      const open = menu.hidden;
      document.querySelectorAll(".rec-menu").forEach((m) => { m.hidden = true; });
      menu.hidden = !open;
      if (open) {
        // Fixed position so the table's scroll box never clips the menu (opens upward near the bottom).
        const r = button.getBoundingClientRect();
        const up = window.innerHeight - r.bottom < menu.offsetHeight + 16;
        menu.style.top = up ? `${Math.max(8, r.top - menu.offsetHeight - 4)}px` : `${r.bottom + 4}px`;
        menu.style.left = `${Math.max(8, r.right - menu.offsetWidth)}px`;
        menu.querySelector("button, a")?.focus();
      }
    }, "rec-more", 16);
    const item = (iconName, label, fn, cls = "") => el("button", { type: "button", role: "menuitem", class: cls, onclick: () => { menu.hidden = true; fn(); } }, aicon(iconName, 14), label);
    const urls = this.spec.fields.filter((f) => f.type === "url" && record[f.name]);
    const texts = this.spec.fields.filter((f) => f.type === "textarea" && !f.editable && record[f.name]);
    menu.append(
      ...urls.map((f) => el("a", { role: "menuitem", class: "menu-link", href: record[f.name], target: "_blank", rel: "noopener noreferrer", onclick: () => { menu.hidden = true; } },
        aicon("external", 14), `Open ${f.label.toLowerCase()}`)),
      ...texts.map((f) => item("copy", `Copy ${f.label.toLowerCase()}`, () => this.copy(record[f.name], f.label))),
      item("trash", "Delete", () => this.remove(record), "danger"));
    menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); menu.hidden = true; button.focus(); } });
    wrap.append(button, menu);
    return wrap;
  }

  async copy(text, label) {
    try { await navigator.clipboard.writeText(text); this.toast(`${label} copied`); }
    catch { this.toast("Could not copy to the clipboard", "error"); }
  }

  async patch(record, fields) {
    try {
      const updated = await request(`/agents/${encodeURIComponent(this.agentId)}/records/${encodeURIComponent(this.spec.name)}/${encodeURIComponent(record.id)}`,
        { method: "PATCH", body: { fields } });
      Object.assign(record, updated);
      for (const [k, v] of Object.entries(fields)) if (v === null) delete record[k];
      this.render();
    } catch (error) { this.toast(error.message, "error"); this.refresh(); }
  }

  async remove(record) {
    const name = record[this.spec.title_field] || `#${record.id}`;
    try {
      await request(`/agents/${encodeURIComponent(this.agentId)}/records/${encodeURIComponent(this.spec.name)}/${encodeURIComponent(record.id)}`, { method: "DELETE" });
      this.records = this.records.filter((r) => r.id !== record.id);
      this.render();
      this.onCount?.(this.records.length);
      this.toast(`${name} deleted`);
    } catch (error) { this.toast(error.message, "error"); }
  }
}

// Close row menus when clicking elsewhere or scrolling (they are fixed-position).
const closeRowMenus = () => document.querySelectorAll(".rec-menu").forEach((m) => { m.hidden = true; });
document.addEventListener("click", (e) => { if (!e.target.closest(".rec-menu-wrap")) closeRowMenus(); });
document.addEventListener("scroll", (e) => { if (!e.target.closest?.(".rec-menu")) closeRowMenus(); }, true);
