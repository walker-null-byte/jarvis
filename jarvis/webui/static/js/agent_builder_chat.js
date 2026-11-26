// The builder's chat: AgentChat (shared transcript, tool cards, approval cards, composer, stop)
// bound to the draft's builder session. Turns start through the draft's own chat route.
import { AgentChat } from "./agent_chat.js";
import { el } from "./dom.js";
import { aicon } from "./agent_icons.js";
import { request } from "./agent_forms.js";

const EDIT_IDEAS = ["Add a field to a table", "Add a button that…", "Make the messages shorter", "Add a setting for…"];
const NEW_IDEAS = ["What will it need from me?", "Add an export", "Keep it simpler"];

export class BuilderChat extends AgentChat {
  constructor({ draft, bus, toast, onSwitchModel }) {
    super({ agent: { id: draft.id, name: "Builder", manifest: { agent: {} } }, bus, toast, onSwitchModel });
    this.draft = draft;
    this.node.classList.add("bchat");
    this.input.placeholder = draft.mode === "edit" ? "Tell the builder what to change…" : "Reply to the builder, or ask for a change…";
    this.input.setAttribute("aria-label", "Message the builder (Enter to send, Shift+Enter for a new line)");
  }

  async loadSessions() { return []; }

  async start() {
    if (this.draft.session_id) await this.open(this.draft.session_id);
    else this.showEmpty();
  }

  newChat() { /* one session per draft */ }

  showEmpty() {
    if (this.transcript.querySelector(".achat-hero")) return;
    const edit = this.draft.mode === "edit";
    const ideas = (edit ? EDIT_IDEAS : NEW_IDEAS).map((text) =>
      el("button", { type: "button", class: "chip", text, onclick: () => { this.input.value = text.replace(/…$/, " "); this.input.focus(); this.autosize(); } }));
    this.transcript.prepend(el("section", { class: "achat-hero" },
      el("span", { class: "agent-tile bchat-tile", "aria-hidden": "true" }, aicon("sparkle", 28)),
      el("h3", { text: edit ? `What should I change in ${this.draft.target_name || this.draft.name}?` : "The builder is getting started…" }),
      el("p", { class: "note", text: edit ? "Describe the change in plain words. Try one of these." : "Describe anything else you want and I will add it." }),
      el("div", { class: "chips" }, ideas)));
  }

  async send() {
    const text = this.input.value.trim();
    if (!text) return;
    if (this.busy) { this.toast("The builder is still working. Wait, or press Stop.", "info"); return; }
    this.input.value = "";
    this.autosize();
    this.hideEmpty();
    try {
      const { session_id: sid } = await request(`/agent-drafts/${encodeURIComponent(this.draft.id)}/chat`, { method: "POST", body: { text } });
      if (sid && sid !== this.sid) {
        const first = !this.sid;
        this.sid = sid;
        if (first) this.chat.load([]);
      }
    } catch (error) {
      this.input.value = text;
      this.autosize();
      if (this.chat.isEmpty()) this.showEmpty();
      this.toast(error.message, "error");
    }
  }
}
