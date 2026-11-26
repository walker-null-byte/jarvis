// The pill under the message box: Thinking (reasoning effort) and Max output (token budget).
// Both are global model settings (llm.effort, llm.max_tokens): they apply to the next message
// and are remembered across restarts, the same as picking a model in the header.
import { api } from "./api.js";
import { el, toast } from "./dom.js";
import { icon } from "./icons.js";

export const EFFORTS = [
  ["", "Auto", "The model's own default"],
  ["off", "Off", "No extended thinking: fastest, cheapest"],
  ["low", "Low", "A little reasoning, quick answers"],
  ["medium", "Medium", "Balanced"],
  ["high", "High", "Deepest reasoning, slowest"],
];
export const BUDGETS = [4096, 8192, 16384, 32768, 65536];

export function tokensLabel(n) {
  return n >= 1024 && n % 1024 === 0 ? `${n / 1024}k` : n.toLocaleString();
}

function effortLabel(value) {
  const known = EFFORTS.find(([v]) => v === value);
  return known ? known[1] : value.charAt(0).toUpperCase() + value.slice(1);
}

export class ComposerOptions {
  constructor(pill, menu, ctx) {
    this.pill = pill;
    this.menu = menu;
    this.ctx = ctx;
    pill.addEventListener("click", (e) => { e.stopPropagation(); this.menu.hidden ? this.open() : this.close(); });
    menu.addEventListener("click", (e) => e.stopPropagation());
    menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); this.close(); this.pill.focus(); } });
    document.addEventListener("click", () => this.close());
  }

  get isOpen() { return !this.menu.hidden; }
  get effort() { return this.ctx.status?.effort || ""; }
  get budget() { return Number(this.ctx.status?.max_tokens) || 0; }
  // The ChatGPT plan API takes no output limit.
  get hasBudget() { return this.ctx.status?.backend !== "chatgpt"; }

  close() {
    this.menu.hidden = true;
    this.pill.setAttribute("aria-expanded", "false");
  }

  open() {
    this.menu.hidden = false;
    this.pill.setAttribute("aria-expanded", "true");
    this.render();
  }

  // Keep the pill label in step with the status (called whenever it refreshes).
  sync() {
    const label = `Thinking: ${effortLabel(this.effort)}`;
    this.pill.replaceChildren(icon("sparkle", 14), el("span", { class: "think-label", text: label }),
      this.hasBudget && this.budget ? el("span", { class: "think-budget", text: tokensLabel(this.budget) }) : null);
    this.pill.title = `${label}${this.hasBudget && this.budget ? `, max output ${this.budget.toLocaleString()} tokens` : ""}. Click to change.`;
    if (this.isOpen) this.render();
  }

  async save(body) {
    try {
      const out = await api("/llm/options", { method: "POST", body });
      this.ctx.setStatus(out.status);
    } catch (error) {
      toast(error.message, "error");
      await this.ctx.refreshStatus();
    }
  }

  render() {
    const rows = EFFORTS.slice();
    if (!rows.some(([v]) => v === this.effort)) rows.push([this.effort, effortLabel(this.effort), "Set in config.toml"]);
    const efforts = rows.map(([value, label, hint]) => el("button", {
      type: "button", role: "menuitemradio", "aria-checked": String(value === this.effort),
      class: `effort-row${value === this.effort ? " active" : ""}`,
      onclick: () => this.save({ effort: value }),
    }, el("span", { class: "effort-text" }, el("span", { class: "effort-name", text: label }), el("span", { class: "note", text: hint })),
    value === this.effort ? icon("check", 15) : null));

    const budgets = BUDGETS.slice();
    if (this.budget && !budgets.includes(this.budget)) { budgets.push(this.budget); budgets.sort((a, b) => a - b); }
    const select = el("select", { id: "max-output", "aria-label": "Max output tokens", disabled: !this.hasBudget,
      onchange: () => this.save({ max_tokens: Number(select.value) }) },
    budgets.map((n) => el("option", { value: String(n), text: `${tokensLabel(n)} tokens`, selected: n === this.budget })));

    this.menu.replaceChildren(
      el("div", { class: "menu-group", role: "group", "aria-label": "Thinking" },
        el("div", { class: "menu-title" }, icon("sparkle", 13), "Thinking"), efforts),
      el("div", { class: "menu-group" },
        el("label", { class: "menu-title", for: "max-output" }, icon("sliders", 13), "Max output"),
        el("div", { class: "budget-row" }, select),
        el("p", { class: "note pad", text: this.hasBudget
          ? "Thinking tokens count against this. If replies stop mid-way, raise it."
          : "The ChatGPT plan sets its own output limit." })),
      el("p", { class: "note pad opt-foot", text: "Applies to your next message, for every chat. Servers that don't support a level ignore it." }));
  }
}
