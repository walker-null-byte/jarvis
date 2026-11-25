// The transcript: user bubbles, streaming assistant text, reasoning, tool cards,
// approvals and artifacts. All text is untrusted and goes through textContent
// or the safe markdown renderer (never innerHTML).
import { api } from "./api.js";
import { avatar } from "./avatar.js";
import { el, formatJson, iconBtn, toast } from "./dom.js";
import { icon, setIcon } from "./icons.js";
import { renderMarkdown } from "./markdown.js";

const SUMMARY_LIMIT = 600;
const SUGGESTIONS = [
  "What can you do?",
  "What do you remember about me?",
  "Help me plan my day",
  "Write a small script to tidy a folder",
];

function firstLine(args) {
  const values = Object.values(args || {});
  const pick = values.find((v) => typeof v === "string") ?? values[0];
  if (pick === undefined) return "";
  const text = typeof pick === "string" ? pick : JSON.stringify(pick);
  return text.replace(/\s+/g, " ").slice(0, 90);
}

export class Chat {
  // hooks: settings(), busy(), character(), speak(text, owner, host), regenerate(),
  //        edit(index, text), switchModel(), retry(), suggest(text), heroGone()
  constructor(root, jumpButton, hooks) {
    this.root = root;
    this.jump = jumpButton;
    this.hooks = hooks;
    this.records = new Map();
    this.hero = null;
    this.stick = true;
    root.addEventListener("scroll", () => {
      this.stick = this.nearBottom();
      this.jump.hidden = this.stick;
    }, { passive: true });
    jumpButton.addEventListener("click", () => {
      this.root.scrollTo({ top: this.root.scrollHeight, behavior: "smooth" });
    });
    this.reset();
  }

  reset() {
    this.clearHero();
    this.root.replaceChildren();
    this.cur = null;          // current assistant record
    this.streamed = false;    // text streamed since the last tool call
    this.running = new Map(); // tool name -> [cards waiting for a result]
    this.approvals = new Map();
    this.renderQueued = new Set();
    this.records = new Map();
    this.turnActive = false;
    this.loadedAt = 0;
    this.jump.hidden = true;
  }

  // ---------- scrolling ----------

  nearBottom() {
    const r = this.root;
    return r.scrollHeight - r.scrollTop - r.clientHeight < 120;
  }

  scrollDown(force = false) {
    if (force || this.stick) this.root.scrollTo({ top: this.root.scrollHeight, behavior: "instant" });
  }

  add(node) {
    this.clearHero();
    this.stick = this.nearBottom();
    this.root.append(node);
    this.scrollDown();
  }

  // ---------- snapshot ----------

  load(items, approvals = []) {
    this.reset();
    for (const item of items) this.renderItem(item);
    for (const approval of approvals) this.approvalRequested(approval);
    this.finishAssistants();
    this.loadedAt = Date.now();
    this.stick = true;
    this.scrollDown(true);
    this.syncActions();
  }

  isEmpty() { return ![...this.root.children].some((n) => !n.classList.contains("hero")); }

  // Map snapshot user items (which carry history `index`) onto the rendered user bubbles by order.
  assignIndexes(items) {
    const indexes = items.filter((i) => i.type === "user").map((i) => i.index);
    const nodes = [...this.root.querySelectorAll(".msg.user")];
    if (indexes.length !== nodes.length) return;
    nodes.forEach((node, n) => {
      if (indexes[n] === undefined || indexes[n] === null) delete node.dataset.index;
      else node.dataset.index = String(indexes[n]);
    });
    this.syncActions();
  }

  // ---------- empty-chat hero ----------

  showHero(character, greeting) {
    this.clearHero();
    const canvas = el("canvas", { class: "hero-orb", width: 160, height: 160, "aria-hidden": "true" });
    const chips = el("div", { class: "chips", role: "list", "aria-label": "Suggestions" },
      SUGGESTIONS.map((text) => el("button", { type: "button", class: "chip", role: "listitem", text, onclick: () => this.hooks.suggest(text) })));
    const name = (character && character.name) || "Jarvis";
    const node = el("section", { class: "hero", "aria-label": `Start a conversation with ${name}` },
      canvas,
      el("h2", { class: "hero-title", text: name }),
      el("div", { class: "hero-greeting md" }, renderMarkdown(greeting || "How can I help you today?")),
      chips);
    this.root.prepend(node);
    this.hero = { node, canvas };
    return canvas;
  }

  clearHero() {
    if (!this.hero) return;
    this.hero.node.remove();
    this.hero = null;
    this.hooks.heroGone?.();
  }

  renderItem(item) {
    if (item.type === "user") this.userMessage(item.content, item.index);
    else if (item.type === "summary") this.summaryNote(item.content);
    else if (item.type === "assistant") this.addText(item.content);
    else if (item.type === "tool") this.toolCard(item);
    else if (item.type === "artifact") this.artifactCard(item.artifact);
  }

  // ---------- message actions ----------

  actionButton(name, label, onclick) {
    return iconBtn(name, label, onclick, "act", 15);
  }

  copyButton(getText) {
    const button = this.actionButton("copy", "Copy", async () => {
      try { await navigator.clipboard.writeText(getText()); setIcon(button, "check", 15); toast("Copied"); }
      catch { toast("Could not copy to the clipboard", "error"); }
      setTimeout(() => setIcon(button, "copy", 15), 1500);
    });
    return button;
  }

  speakButton(getText, host) {
    const button = this.actionButton("speaker", "Speak", () => this.hooks.speak(getText(), owner, host));
    const owner = {
      button,
      setState: (state) => {
        const playing = state === "playing" || state === "loading";
        button.classList.toggle("loading", state === "loading");
        button.dataset.state = state;
        setIcon(button, playing ? "stop" : "speaker", 15);
        const label = playing ? "Stop speaking" : "Speak";
        button.setAttribute("aria-label", label);
        button.title = label;
      },
    };
    return { button, owner };
  }

  // ---------- user messages ----------

  userMessage(text, index) {
    this.cur = null;
    const body = el("div", { class: "msg-body plain", text });
    const edit = this.actionButton("pencil", "Edit message", () => this.startEdit(node, text));
    edit.classList.add("act-edit");
    const actions = el("div", { class: "msg-actions", role: "group", "aria-label": "Message actions" },
      this.copyButton(() => text), edit);
    const node = el("div", { class: "msg user" },
      el("div", { class: "sr-only", text: "You" }),
      el("div", { class: "msg-main" }, body, actions));
    if (index !== undefined && index !== null) node.dataset.index = String(index);
    this.add(node);
    this.syncActions();
    return node;
  }

  startEdit(node, original) {
    const main = node.querySelector(".msg-main");
    const body = node.querySelector(".msg-body");
    const actions = node.querySelector(".msg-actions");
    const area = el("textarea", { class: "edit-area", "aria-label": "Edit message", rows: 2 });
    area.value = original;
    const fit = () => { area.style.height = "auto"; area.style.height = `${Math.min(area.scrollHeight + 2, 320)}px`; };
    const close = () => { box.remove(); body.hidden = false; actions.hidden = false; };
    const save = el("button", { type: "button", class: "primary small", text: "Save & resend" });
    const cancel = el("button", { type: "button", class: "ghost small", text: "Cancel", onclick: close });
    const submit = async () => {
      const text = area.value.trim();
      if (!text) return;
      save.disabled = true;
      try {
        await this.hooks.edit(Number(node.dataset.index), text);
        close();
      } catch (error) {
        toast(error.message, "error");
        save.disabled = false;
      }
    };
    save.addEventListener("click", submit);
    area.addEventListener("input", fit);
    area.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); close(); }
      else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
    });
    const box = el("div", { class: "edit-box" }, area,
      el("p", { class: "note" }, icon("info", 13), " Messages after this one will be replaced by the new reply."),
      el("div", { class: "edit-actions" }, cancel, save));
    body.hidden = true;
    actions.hidden = true;
    main.append(box);
    fit();
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
  }

  summaryNote(text) {
    this.cur = null;
    this.add(el("details", { class: "summary-note" },
      el("summary", { text: "Earlier conversation was summarized" }),
      el("div", { class: "md" }, renderMarkdown(text.slice(0, SUMMARY_LIMIT * 4)))));
  }

  // ---------- assistant messages ----------

  assistant() {
    if (!this.cur) {
      const character = this.hooks.character();
      const body = el("div", { class: "msg-body" });
      const rec = { segs: [], reasoning: null, seg: null, body };
      const host = el("div", { class: "msg-extra" });
      const text = () => rec.segs.map((s) => s.text).join("\n\n").trim();
      const { button: speak, owner } = this.speakButton(text, host);
      const regen = this.actionButton("redo", "Re-do response", () => this.hooks.regenerate());
      regen.classList.add("act-regen");
      const actions = el("div", { class: "msg-actions", role: "group", "aria-label": "Reply actions", hidden: true },
        this.copyButton(text), regen, speak);
      const typing = el("div", { class: "typing", role: "status", "aria-label": "Jarvis is working" },
        el("span"), el("span"), el("span"));
      const root = el("div", { class: `msg assistant${this.turnActive ? " streaming" : ""}` },
        el("div", { class: "msg-avatar" }, avatar(character, 28)),
        el("div", { class: "msg-main" },
          el("div", { class: "msg-role", text: (character && character.name) || "Jarvis" }),
          body, typing, actions, host));
      Object.assign(rec, { root, text, owner, actions, regen, host, typing });
      this.records.set(root, rec);
      this.cur = rec;
      this.add(root);
    }
    return this.cur;
  }

  lastAssistant() {
    const nodes = [...this.root.querySelectorAll(".msg.assistant")];
    const last = nodes[nodes.length - 1];
    return last ? this.records.get(last) : null;
  }

  syncActions() {
    const busy = this.hooks.busy();
    const nodes = [...this.root.querySelectorAll(".msg.assistant")];
    const last = nodes[nodes.length - 1];
    for (const node of nodes) {
      const rec = this.records.get(node);
      if (!rec) continue;
      rec.regen.hidden = node !== last;
      rec.regen.disabled = busy;
    }
    for (const edit of this.root.querySelectorAll(".act-edit")) {
      const user = edit.closest(".msg.user");
      edit.disabled = busy || user.dataset.index === undefined;
      edit.title = edit.disabled && !busy ? "Editing is unavailable for this message" : "Edit message";
    }
  }

  finishAssistants() {
    for (const rec of this.records.values()) rec.actions.hidden = !rec.text();
  }

  beginTurn() {
    this.turnActive = true;
    this.assistant(); // shows the typing indicator right away
    this.syncActions();
  }

  endTurn() {
    this.turnActive = false;
    for (const [node, rec] of [...this.records]) {
      rec.typing.remove();
      node.classList.remove("streaming");
      if (!rec.body.children.length && !rec.text()) { node.remove(); this.records.delete(node); }
    }
    this.cur = null;
    this.finishAssistants();
    this.syncActions();
  }

  addText(text) {
    const seg = this.textSegment();
    seg.text += text;
    this.renderSegment(seg);
    this.cur.actions.hidden = false;
  }

  textSegment() {
    const cur = this.assistant();
    if (!cur.seg) {
      cur.seg = { text: "", node: el("div", { class: "seg" }) };
      cur.segs.push(cur.seg);
      cur.body.append(cur.seg.node);
    }
    return cur.seg;
  }

  renderSegment(seg) {
    if (this.renderQueued.has(seg)) return;
    this.renderQueued.add(seg);
    requestAnimationFrame(() => {
      this.renderQueued.delete(seg);
      this.stick = this.nearBottom();
      seg.node.replaceChildren(renderMarkdown(seg.text));
      this.scrollDown();
    });
  }

  addReasoning(text) {
    const cur = this.assistant();
    if (!cur.reasoning) {
      const content = el("div", { class: "reasoning-text" });
      const node = el("details", { class: "reasoning" }, el("summary", {}, icon("sparkle", 13), "Reasoning"), content);
      cur.reasoning = { node, content };
      cur.body.append(node);
      cur.seg = null; // text after the reasoning starts a new segment
    }
    this.stick = this.nearBottom();
    cur.reasoning.content.textContent += text;
    this.scrollDown();
  }

  // ---------- tools ----------

  toolCard(item, running = false) {
    const cur = this.assistant();
    const state = running ? "running" : (item.status === "pending" ? "done" : item.status || "done");
    const status = el("span", { class: `tool-status ${state}`, text: state });
    const spin = el("span", { class: "spin", "aria-hidden": "true" });
    const summary = el("summary", {},
      el("span", { class: "tool-icon" }, icon("terminal", 13)),
      el("span", { class: "tool-name", text: item.name }),
      item.via ? el("span", { class: "badge", text: `via ${item.via}` }) : null,
      el("span", { class: "tool-hint", text: firstLine(item.args) }),
      spin, status, el("span", { class: "tool-time" }));
    const result = el("pre", { class: "tool-result", text: item.result ?? "" });
    const card = el("details", { class: `tool ${state}` }, summary,
      el("div", { class: "tool-detail" },
        el("div", { class: "tool-label", text: "arguments" }),
        el("pre", { class: "tool-args", text: formatJson(item.args || {}, 3000) }),
        el("div", { class: "tool-label", text: "result" }), result));
    cur.body.append(card);
    cur.seg = null;
    this.scrollDown();
    return { card, status, result, time: summary.querySelector(".tool-time"), started: performance.now() };
  }

  toolStarted(data) {
    this.streamed = false;
    const card = this.toolCard({ name: data.name, args: data.args, via: data.via, status: "running" }, true);
    if (!this.running.has(data.name)) this.running.set(data.name, []);
    this.running.get(data.name).push(card);
  }

  toolFinished(data) {
    const queue = this.running.get(data.name) || [];
    const card = queue.shift() || this.toolCard({ name: data.name, args: data.args, via: data.via });
    const text = typeof data.result === "string" ? data.result : formatJson(data.result);
    const failed = /^(Error|Tool arguments were not valid)/.test(text);
    const state = failed ? "error" : "done";
    card.status.className = `tool-status ${state}`;
    card.status.textContent = state;
    card.card.className = `tool ${state}`;
    card.time.textContent = `${((performance.now() - card.started) / 1000).toFixed(1)}s`;
    card.result.textContent = text;
    if (failed) card.card.open = true;
  }

  // ---------- approvals ----------

  approvalRequested(data) {
    if (this.approvals.has(data.request_id)) return;
    const timeout = Number(this.hooks.settings()["security.approval_timeout"]) || 120;
    const started = data.requested_at ? data.requested_at * 1000 : Date.now();
    const countdown = el("span", { class: "countdown" });
    const outcome = el("span", { class: "approval-outcome" });
    const decide = async (approved) => {
      buttons.forEach((b) => { b.disabled = true; });
      try { await api(`/approvals/${data.request_id}`, { method: "POST", body: { approved } }); }
      catch (error) { toast(error.message, "error"); }
    };
    const approve = el("button", { class: "primary small", text: "Approve", onclick: () => decide(true) });
    const deny = el("button", { class: "danger small", text: "Deny", onclick: () => decide(false) });
    const buttons = [approve, deny];
    const card = el("div", { class: "approval", role: "alertdialog", "aria-label": "Approval required" },
      el("div", { class: "approval-head" }, icon("alert", 16), el("strong", { text: "Approval required" }),
        el("span", { class: "badge", text: data.action }), countdown),
      el("pre", { class: "tool-args", text: formatJson(data.payload || {}, 2000) }),
      el("div", { class: "approval-actions" }, approve, deny, outcome));
    const tick = () => {
      const left = Math.max(0, Math.round(timeout - (Date.now() - started) / 1000));
      countdown.textContent = `${left}s left`;
      if (left === 0) clearInterval(timer);
    };
    const timer = setInterval(tick, 1000);
    tick();
    this.approvals.set(data.request_id, { card, buttons, outcome, countdown, timer });
    this.cur = null;
    this.add(card);
  }

  approvalFinished(data) {
    const entry = this.approvals.get(data.request_id);
    if (!entry) return;
    clearInterval(entry.timer);
    entry.buttons.forEach((b) => { b.disabled = true; });
    entry.outcome.textContent = data.approved ? "Approved" : "Denied or expired";
    entry.card.classList.add(data.approved ? "approved" : "denied");
    entry.countdown.textContent = "";
  }

  // ---------- artifacts & errors ----------

  artifactCard(artifact) {
    if (!artifact) return;
    const cur = this.assistant();
    cur.body.append(el("div", { class: "artifact" },
      el("div", { class: "artifact-title" }, el("strong", { text: artifact.title || "Artifact" }),
        el("span", { class: "badge", text: artifact.type })),
      artifactBody(artifact)));
    cur.seg = null;
    this.scrollDown();
  }

  errorBanner(message) {
    this.cur = null;
    this.add(el("div", { class: "error-banner", role: "alert" },
      el("div", { class: "error-head" }, icon("alert", 16), el("strong", { text: "Something went wrong" })),
      el("p", { class: "error-text", text: message }),
      el("div", { class: "error-actions" },
        el("button", { type: "button", class: "primary small", text: "Switch model/provider", onclick: () => this.hooks.switchModel() }),
        el("button", { type: "button", class: "small", text: "Retry", onclick: () => this.hooks.retry() }))));
  }

  commandOutput(command, output) {
    this.cur = null;
    this.add(el("div", { class: "cmd-output" },
      el("div", { class: "tool-label", text: command }),
      el("pre", { class: "tool-result", text: output || "(no output)" })));
    this.scrollDown(true);
  }

  note(text) {
    this.add(el("div", { class: "turn-note", text }));
  }

  // ---------- live events ----------

  handle(msg) {
    const data = msg.data || {};
    switch (msg.kind) {
      case "user_message": {
        const last = this.root.lastElementChild;
        const fresh = Date.now() - this.loadedAt < 2000;
        // A snapshot refetch may already contain this message: don't show it twice.
        if (fresh && last && last.classList.contains("user")
            && last.querySelector(".msg-body").textContent === data.content) break;
        this.streamed = false;
        this.stick = true; // your own message always scrolls to the bottom
        this.userMessage(data.content);
        break;
      }
      case "turn_started": this.beginTurn(); break;
      case "assistant_delta": this.streamed = true; this.addText(data.content); break;
      case "assistant_reasoning_delta": this.addReasoning(data.content); break;
      case "assistant_message":
        if (data.content && !this.streamed) this.addText(data.content);
        break;
      case "tool_started": this.toolStarted(data); break;
      case "tool_finished": this.toolFinished(data); break;
      case "artifact_created": this.artifactCard(data.artifact); break;
      case "approval_requested": this.approvalRequested(data); break;
      case "approval_finished": this.approvalFinished(data); break;
      case "error": this.errorBanner(data.message || "Unknown error"); break;
      case "turn_finished":
        if (data.status === "cancelled") this.note("Turn cancelled");
        if (data.status === "budget_exhausted") this.note("Tool budget for this turn was used up");
        break;
    }
  }
}

// ---------- artifact bodies ----------

function artifactBody(artifact) {
  const { type, content, data } = artifact;
  if (type === "image") {
    return el("img", { class: "artifact-image", src: `/api/artifacts/${encodeURIComponent(artifact.id)}/file`, alt: artifact.title || "image" });
  }
  if (type === "markdown") return renderMarkdown(content || "");
  if (type === "text") return el("pre", { class: "tool-result", text: content || "" });
  if (type === "table" || type === "chart") {
    const table = dataToTable(data);
    if (table) return table;
  }
  return el("pre", { class: "tool-result", text: formatJson(data ?? content ?? "") });
}

// Render common table/chart data shapes as a plain table.
export function dataToTable(data) {
  let columns, rows;
  if (Array.isArray(data) && data.length && typeof data[0] === "object" && !Array.isArray(data[0])) {
    columns = [...new Set(data.flatMap((row) => Object.keys(row)))];
    rows = data.map((row) => columns.map((c) => row[c]));
  } else if (Array.isArray(data) && data.length && Array.isArray(data[0])) {
    columns = data[0].map(String);
    rows = data.slice(1);
  } else if (data && Array.isArray(data.columns) && Array.isArray(data.rows)) {
    columns = data.columns.map(String);
    rows = data.rows;
  } else if (data && typeof data === "object" && !Array.isArray(data)
             && Object.values(data).every(Array.isArray)) {
    columns = Object.keys(data);
    const length = Math.max(...Object.values(data).map((v) => v.length));
    rows = Array.from({ length }, (_, i) => columns.map((c) => data[c][i]));
  } else {
    return null;
  }
  const cell = (tag, value) => el(tag, { text: value === undefined || value === null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value) });
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, columns.map((c) => cell("th", c)))),
    el("tbody", {}, rows.slice(0, 200).map((row) => el("tr", {}, row.map((v) => cell("td", v)))))));
}
