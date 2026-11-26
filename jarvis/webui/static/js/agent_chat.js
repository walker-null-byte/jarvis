// Chat with an agent: the normal chat.js transcript bound to one of its runtime sessions,
// a picker for its earlier chats, and a composer. Events come from the shared bus,
// filtered by session id. Starting a new chat (by message or action) buffers events until
// the server says which session it created, so nothing streamed in between is lost.
import { api } from "./api.js";
import { Chat } from "./chat.js";
import { Select } from "./combo.js";
import { el, iconBtn } from "./dom.js";
import { icon } from "./icons.js";
import { renderMarkdown } from "./markdown.js";
import { agentTile } from "./agent_icons.js";
import { ago, request } from "./agent_forms.js";

export class AgentChat {
  constructor({ agent, bus, toast, onSwitchModel, onSessionChange }) {
    this.agent = agent;
    this.toast = toast;
    this.sid = "";
    this.busy = false;
    this.buffer = null;       // events captured while a new session is being created
    this.joinedMidTurn = false;
    this.onSwitchModel = onSwitchModel;
    this.onSessionChange = onSessionChange;

    this.picker = new Select({ label: "Earlier chats with this agent", options: [], value: "", placeholder: "New chat",
      onchange: (sid) => this.open(sid) });
    this.picker.node.classList.add("achat-picker");
    const newBtn = iconBtn("plus", "New chat with this agent", () => this.newChat(), "", 17);
    this.transcript = el("div", { class: "achat-log", role: "log", "aria-label": `Chat with ${agent.name}`, "aria-live": "polite", tabindex: "0" });
    const jump = el("button", { class: "jump", type: "button", hidden: true, "aria-label": "Jump to latest message" }, icon("arrow-down", 15), "Jump to latest");
    this.input = el("textarea", { rows: 1, placeholder: `Message ${agent.name}…`, "aria-label": `Message ${agent.name} (Enter to send, Shift+Enter for a new line)` });
    this.sendBtn = el("button", { type: "submit", class: "round primary", "aria-label": "Send", title: "Send (Enter)" }, icon("send", 18));
    this.stopBtn = el("button", { type: "button", class: "round danger", hidden: true, "aria-label": "Stop", title: "Stop", onclick: () => this.stop() }, icon("stop", 16));
    const form = el("form", { class: "achat-composer", autocomplete: "off" },
      el("div", { class: "composer-box" }, this.input, el("div", { class: "composer-actions" }, this.stopBtn, this.sendBtn)));
    form.addEventListener("submit", (e) => { e.preventDefault(); this.send(); });
    this.input.addEventListener("input", () => this.autosize());
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.send(); }
      else if (e.key === "Escape" && this.busy) { e.stopPropagation(); this.stop(); }
    });
    this.node = el("section", { class: "achat", "aria-label": `Chat with ${agent.name}` },
      el("div", { class: "achat-head" }, this.picker.node, newBtn),
      el("div", { class: "achat-stage" }, this.transcript, jump), form);

    this.chat = new Chat(this.transcript, jump, {
      settings: () => ({}),
      busy: () => this.busy,
      character: () => ({ id: agent.id, name: agent.name }),
      speak: () => {},
      regenerate: () => {},
      edit: async () => {},
      switchModel: () => this.onSwitchModel?.(),
      retry: () => this.retry(),
      suggest: (text) => { this.input.value = text; this.send(); },
      heroGone: () => {},
    });
    this.unsubscribe = bus.on((msg) => this.onEvent(msg));
  }

  destroy() { this.unsubscribe?.(); }

  autosize() {
    this.input.style.height = "auto";
    this.input.style.height = `${Math.min(this.input.scrollHeight, 180)}px`;
  }

  setBusy(value) {
    this.busy = value;
    this.stopBtn.hidden = !value;
    this.sendBtn.hidden = value;
    this.stopBtn.disabled = false;
    this.chat.syncActions();
    this.node.classList.toggle("is-busy", value);
    this.onBusy?.(value);
  }

  // ---------- sessions ----------

  async loadSessions() {
    try {
      const { sessions } = await api(`/sessions?kind=agent&owner=${encodeURIComponent(this.agent.id)}`);
      this.sessions = sessions;
      const options = sessions.map((s) => ({ value: s.id, label: s.title || "Chat", sub: ago(s.updated) }));
      if (this.sid && !options.some((o) => o.value === this.sid)) options.unshift({ value: this.sid, label: "Current chat", sub: "now" });
      this.picker.setOptions(options);
      this.picker.setValue(this.sid);
      return sessions;
    } catch (error) { this.toast(error.message, "error"); return []; }
  }

  async start(preferred = "") {
    const sessions = await this.loadSessions();
    const sid = preferred || sessions[0]?.id || "";
    if (sid) await this.open(sid); else this.showEmpty();
  }

  async open(sid) {
    this.sid = sid;
    this.picker.setValue(sid);
    this.onSessionChange?.(sid);
    try {
      const epoch = this.busyEvents = (this.busyEvents || 0);
      const data = await api(`/runtime/${encodeURIComponent(sid)}/history`);
      if (this.sid !== sid) return;
      // A busy_changed event that arrived while the history was in flight is newer than the history.
      if (this.busyEvents !== epoch) data.busy = this.busy;
      this.chat.load(data.history, data.approvals || []);
      decorateActions(this.transcript);
      this.setBusy(!!data.busy);
      this.joinedMidTurn = !!data.busy;
      if (data.busy) this.chat.beginTurn();
      if (!data.history.length) this.showEmpty();
    } catch (error) {
      this.toast(error.message, "error");
      this.newChat();
    }
  }

  newChat() {
    this.sid = "";
    this.picker.setValue("");
    this.onSessionChange?.("");
    this.chat.load([]);
    this.setBusy(false);
    this.showEmpty();
    this.input.focus();
  }

  showEmpty() {
    if (this.transcript.querySelector(".achat-hero")) return;
    const a = this.agent;
    const chips = (a.manifest.agent.suggestions || []).map((text) =>
      el("button", { type: "button", class: "chip", text, onclick: () => { this.input.value = text; this.send(); } }));
    this.transcript.prepend(el("section", { class: "achat-hero" },
      agentTile(a, 56),
      el("h3", { text: a.name }),
      el("div", { class: "md note" }, renderMarkdown(a.manifest.agent.greeting || a.description || "How can I help?")),
      chips.length ? el("div", { class: "chips" }, chips) : null));
  }

  hideEmpty() { this.transcript.querySelector(".achat-hero")?.remove(); }

  // ---------- sending ----------

  // Run fn() that returns {session_id}; while it runs, buffer events of an unknown session.
  async post(fn) {
    const fresh = !this.sid;
    if (fresh) { this.buffer = []; this.chat.load([]); }
    this.hideEmpty();
    try {
      const { session_id: sid } = await fn(this.sid || null);
      if (fresh) {
        this.sid = sid;
        this.picker.setValue(sid);
        this.onSessionChange?.(sid);
        const missed = (this.buffer || []).filter((m) => m.session_id === sid);
        this.buffer = null;
        missed.forEach((m) => this.onEvent(m));
        this.loadSessions();
      }
      return sid;
    } catch (error) {
      this.buffer = null;
      if (fresh && this.chat.isEmpty()) this.showEmpty();
      throw error;
    }
  }

  async send() {
    const text = this.input.value.trim();
    if (!text) return;
    if (this.busy) { this.toast(`${this.agent.name} is still working. Wait, or press Stop.`, "info"); return; }
    this.input.value = "";
    this.autosize();
    try {
      await this.post((sid) => request(`/agents/${encodeURIComponent(this.agent.id)}/chat`, { method: "POST", body: { text, session_id: sid } }));
    } catch (error) {
      this.input.value = text;
      this.autosize();
      this.toast(error.message, "error");
    }
  }

  // Run an action in the current chat (or a new one). Throws the request error (with .data).
  async runAction(actionId, inputs) {
    if (this.busy) throw Object.assign(new Error(`${this.agent.name} is still working. Wait for it to finish, or start a new chat.`), { data: {} });
    return this.post((sid) => request(`/agents/${encodeURIComponent(this.agent.id)}/actions/${encodeURIComponent(actionId)}`,
      { method: "POST", body: { inputs, session_id: sid } }));
  }

  async stop() {
    if (!this.sid) return;
    this.stopBtn.disabled = true;
    try { await api(`/runtime/${encodeURIComponent(this.sid)}/cancel`, { method: "POST" }); }
    catch (error) { this.stopBtn.disabled = false; this.toast(error.message, "error"); }
  }

  async retry() {
    if (!this.sid) return;
    try { await api(`/runtime/${encodeURIComponent(this.sid)}/retry`, { method: "POST" }); }
    catch (error) { this.toast(error.message, "error"); }
  }

  // ---------- events ----------

  onEvent(msg) {
    if (this.buffer && msg.session_id && msg.session_id !== this.sid) { this.buffer.push(msg); return; }
    if (!this.sid || msg.session_id !== this.sid) return;
    const d = msg.data || {};
    if (msg.kind === "session_started") return;
    if (msg.kind === "turn_started") { this.hideEmpty(); this.setBusy(true); }
    if (msg.kind === "busy_changed") { this.busyEvents = (this.busyEvents || 0) + 1; this.setBusy(!!d.busy); if (!d.busy) this.chat.endTurn(); return; }
    this.chat.handle(msg);
    if (msg.kind === "user_message") decorateActions(this.transcript);
    if (msg.kind === "turn_finished") {
      this.chat.endTurn();
      if (this.joinedMidTurn) { this.joinedMidTurn = false; this.open(this.sid); }
      this.loadSessions();
    }
  }
}

// An action's chat message starts with "⚡ Label\ninputs…\n\nprompt": show a compact card
// with the full instructions folded away.
export function decorateActions(root) {
  for (const body of root.querySelectorAll(".msg.user .msg-body:not(.action-done)")) {
    body.classList.add("action-done");
    const text = body.textContent || "";
    if (!text.startsWith("⚡ ")) continue;
    const cut = text.indexOf("\n\n");
    const head = (cut < 0 ? text : text.slice(0, cut)).split("\n");
    const prompt = cut < 0 ? "" : text.slice(cut + 2);
    body.classList.add("action-msg");
    body.replaceChildren(
      el("div", { class: "action-title" }, icon("sparkle", 14), el("strong", { text: head[0].slice(2) })),
      head[1] ? el("div", { class: "action-inputs", text: head.slice(1).join(" ") }) : null,
      prompt ? el("details", { class: "action-prompt" }, el("summary", { text: "Instructions sent" }), el("div", { class: "action-prompt-text", text: prompt })) : null);
  }
}
