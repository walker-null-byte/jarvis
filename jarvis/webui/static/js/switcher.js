// Header pill + popover for switching provider/model between messages.
import { api } from "./api.js";
import { el, toast } from "./dom.js";
import { icon } from "./icons.js";
import { signInFlow, signInLink } from "./auth.js";

const KINDS = [
  ["subscription", "Subscription", "cloud"],
  ["local", "Local", "server"],
  ["api_key", "API key", "key"],
];

export class Switcher {
  constructor(pill, menu, ctx) {
    this.pill = pill;
    this.menu = menu;
    this.ctx = ctx;
    this.models = [];
    this.filter = "";
    pill.addEventListener("click", (e) => { e.stopPropagation(); this.menu.hidden ? this.open() : this.close(); });
    menu.addEventListener("click", (e) => e.stopPropagation());
    menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); this.close(); this.pill.focus(); } });
    document.addEventListener("click", () => this.close());
  }

  get isOpen() { return !this.menu.hidden; }

  close() {
    this.menu.hidden = true;
    this.pill.setAttribute("aria-expanded", "false");
  }

  async open() {
    this.menu.hidden = false;
    this.pill.setAttribute("aria-expanded", "true");
    this.menu.replaceChildren(el("p", { class: "note pad", text: "Loading…" }));
    this.filter = "";
    try {
      const [{ providers, active }, models, chatgpt] = await Promise.all([
        api("/providers"),
        api("/models").catch(() => ({ ok: false, models: [], message: "Could not list models. Is the server running? You can type a model name in Settings." })),
        api("/auth/chatgpt").catch(() => null),
      ]);
      this.providers = providers;
      this.active = active;
      this.models = models.models || [];
      this.modelNote = models.ok === false ? models.message : "";
      this.chatgpt = chatgpt;
      this.draw();
      this.menu.scrollTop = 0;
      this.menu.querySelector(".prov-btn.active")?.scrollIntoView({ block: "nearest" });
      this.menu.querySelector("input[type=search]")?.focus({ preventScroll: true });
    } catch (error) {
      this.menu.replaceChildren(el("p", { class: "note pad", text: error.message }));
    }
  }

  draw() {
    const status = this.ctx.status || {};
    const parts = [];
    for (const [kind, title, iconName] of KINDS) {
      const group = this.providers.filter((p) => p.id !== "custom" && (p.kind || (p.id === "chatgpt" ? "subscription" : "api_key")) === kind);
      if (!group.length) continue;
      parts.push(el("div", { class: "menu-group" },
        el("div", { class: "menu-title" }, icon(iconName, 13), title),
        group.map((p) => this.providerRow(p))));
    }
    const current = this.providers.find((p) => p.id === this.active);
    const filter = el("input", { type: "search", placeholder: "Filter models…", "aria-label": "Filter models", value: this.filter });
    const list = el("div", { class: "model-list", role: "listbox", "aria-label": "Models" });
    const drawModels = () => {
      const needle = filter.value.trim().toLowerCase();
      const shown = this.models.filter((m) => !needle || m.toLowerCase().includes(needle));
      list.replaceChildren(...(shown.length ? shown.map((m) => el("button", {
        type: "button", role: "option", class: `model-row${m === status.model ? " active" : ""}`,
        "aria-selected": String(m === status.model), onclick: () => this.pickModel(m),
      }, el("span", { text: m }), m === status.model ? icon("check", 14) : null))
        : [el("p", { class: "note pad", text: this.modelNote || (this.models.length ? "No match." : "No models listed. Type one in Settings.") })]));
    };
    filter.addEventListener("input", () => { this.filter = filter.value; drawModels(); });
    drawModels();
    parts.push(el("div", { class: "menu-group" },
      el("div", { class: "menu-title" }, `Model · ${current?.label || this.active}`), filter, list));
    this.menu.replaceChildren(...parts);
  }

  providerRow(p) {
    const active = p.id === this.active;
    const row = el("div", { class: "prov" },
      el("button", {
        type: "button", class: `prov-btn${active ? " active" : ""}`, "aria-pressed": String(active),
        onclick: () => this.pickProvider(p.id),
      }, el("span", { class: "prov-label", text: p.label || p.id }), active ? icon("check", 14) : null));
    if (active && p.id === "chatgpt" && !(this.chatgpt && this.chatgpt.signed_in)) {
      row.append(this.signInBox());
    }
    return row;
  }

  signInBox() {
    const note = el("span", { class: "note", role: "status" });
    const button = el("button", { type: "button", class: "primary small", text: "Sign in with ChatGPT" });
    button.addEventListener("click", async () => {
      button.disabled = true;
      note.textContent = "Waiting for you to finish signing in…";
      try {
        await signInFlow({ onLink: (f) => { note.replaceChildren("Waiting… If no tab opened, ", signInLink(f.auth_url), "."); } });
        toast("Signed in with ChatGPT");
        this.chatgpt = { signed_in: true };
        await this.pickProvider("chatgpt");
      } catch (error) { note.textContent = error.message; button.disabled = false; }
    });
    return el("div", { class: "signin-box" }, button, note);
  }

  async apply(body) {
    try {
      const out = await api("/provider", { method: "POST", body });
      this.ctx.setStatus(out.status);
      return out;
    } catch (error) { toast(error.message, "error"); return null; }
  }

  async pickProvider(id) {
    if (id === this.active && this.providers) { return; }
    const out = await this.apply({ provider: id });
    if (!out) return;
    toast(`Switched to ${this.providers.find((p) => p.id === id)?.label || id}`);
    await this.open();
  }

  async pickModel(model) {
    const out = await this.apply({ provider: this.active, model });
    if (!out) return;
    toast(`Model set to ${model}. It applies to your next message.`);
    this.close();
    this.pill.focus();
  }
}
