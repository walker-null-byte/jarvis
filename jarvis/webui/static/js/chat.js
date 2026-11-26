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

// One-line summary of a tool call's arguments: path=".", name="x"
// A spawn_agent result is JSON that names the sub-agent's session.
function subagentOf(text) {
  const match = /"session_id":\s*"([^"]+)"/.exec(String(text || ""));
  return match ? match[1] : "";
}

function firstLine(args) {
  const parts = Object.entries(args || {}).map(([key, value]) => {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    const flat = String(text).replace(/\s+/g, " ");
    return `${key}=${flat.length > 48 ? `${flat.slice(0, 48)}…` : flat}`;
  });
  return parts.join("  ");
}

// Tool arguments as readable rows: strings keep their real line breaks instead of \n escapes.
function argsView(args) {
  const entries = Object.entries(args || {});
  if (!entries.length) return el("div", { class: "tool-args empty", text: "(no arguments)" });
  return el("div", { class: "tool-args" }, entries.map(([key, value]) => {
    let text = typeof value === "string" ? value : formatJson(value, 4000);
    if (text.length > 4000) text = `${text.slice(0, 4000)}\n… (truncated)`;
    return el("div", { class: "arg" }, el("span", { class: "arg-key", text: key }), el("pre", { class: "arg-val", text }));
  }));
}

function seconds(value) {
  if (value < 1) return "<1s";
  const n = Math.round(value);
  return n >= 60 ? `${Math.floor(n / 60)}m ${String(n % 60).padStart(2, "0")}s` : `${n}s`;
}

// "gemma4 · 312 tokens · 41.2 tok/s · 7.6 s" (one or several model calls of a reply)
export function statsLine(list) {
  if (!list.length) return { text: "", title: "" };
  const tokens = list.reduce((sum, s) => sum + (s.output_tokens || 0), 0);
  const approx = list.some((s) => s.approx);
  const timed = list.filter((s) => s.tok_per_s && s.gen_seconds);
  const rate = timed.length
    ? timed.reduce((sum, s) => sum + s.output_tokens, 0) / timed.reduce((sum, s) => sum + s.gen_seconds, 0)
    : null;
  const total = list.reduce((sum, s) => sum + (s.seconds || 0), 0);
  const parts = [list[list.length - 1].model || ""];
  if (tokens) parts.push(`${approx ? "~" : ""}${tokens.toLocaleString()} tokens`);
  if (rate) parts.push(`${rate.toFixed(1)} tok/s`);
  if (total) parts.push(`${total.toFixed(1)} s`);
  if (list.length > 1) parts.push(`${list.length} steps`);
  const ttft = list[0].ttft;
  const input = list.reduce((sum, s) => sum + (s.input_tokens || 0), 0);
  const title = [
    ttft != null ? `First token after ${ttft.toFixed(2)} s` : "",
    input ? `${input.toLocaleString()} prompt tokens` : "",
    approx ? "Token count is an estimate (the server did not report usage)" : "",
  ].filter(Boolean).join(" · ");
  return { text: parts.filter(Boolean).join(" · "), title };
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
    this.lastTop = 0;
    // Follow the stream only while the reader is at the bottom. Content growth never
    // lowers scrollTop, so a scroll *up* means the reader took over; reaching the
    // bottom again hands control back.
    root.addEventListener("scroll", () => {
      const top = root.scrollTop;
      if (top < this.lastTop - 1) this.stick = false;
      this.lastTop = top;
      if (this.nearBottom()) this.stick = true;
      this.jump.hidden = this.stick;
    }, { passive: true });
    jumpButton.addEventListener("click", () => {
      this.stick = true;
      this.root.scrollTo({ top: this.root.scrollHeight, behavior: "smooth" });
    });
    this.reset();
  }

  reset() {
    for (const rec of this.records?.values() || []) if (rec.reasoning) clearInterval(rec.reasoning.interval);
    for (const entry of this.approvals?.values() || []) clearInterval(entry.timer);
    this.clearHero();
    this.root.replaceChildren();
    this.cur = null;          // current assistant record
    this.streamed = false;    // text streamed since the last tool call
    this.running = new Map(); // tool name -> [cards waiting for a result]
    this.byCall = new Map();  // call_id -> running card (parallel tool calls finish out of order)
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
    return r.scrollHeight - r.scrollTop - r.clientHeight < 32;
  }

  scrollDown(force = false) {
    if (force) this.stick = true;
    // The reader may have scrolled up since our last jump, before the scroll event was delivered.
    else if (this.root.scrollTop < this.lastTop - 1 && !this.nearBottom()) this.stick = false;
    if (this.stick) {
      this.root.scrollTo({ top: this.root.scrollHeight, behavior: "instant" });
      this.lastTop = this.root.scrollTop;
    }
  }

  add(node, before = null) {
    this.clearHero();
    if (before) before.before(node); else this.root.append(node);
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
    else if (item.type === "assistant") this.assistantItem(item);
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

  userMessage(text, index, before = null) {
    if (!before) this.closeCur();
    const body = el("div", { class: "msg-body plain", text });
    const edit = this.actionButton("pencil", "Edit message", () => this.startEdit(node, text));
    edit.classList.add("act-edit");
    const actions = el("div", { class: "msg-actions", role: "group", "aria-label": "Message actions" },
      this.copyButton(() => text), edit);
    const node = el("div", { class: "msg user" },
      el("div", { class: "sr-only", text: "You" }),
      el("div", { class: "msg-main" }, body, actions));
    if (index !== undefined && index !== null) node.dataset.index = String(index);
    this.add(node, before);
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
    this.closeCur();
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
      const statsNode = el("span", { class: "msg-stats" });
      const root = el("div", { class: `msg assistant${this.turnActive ? " streaming" : ""}` },
        el("div", { class: "msg-avatar" }, avatar(character, 28)),
        el("div", { class: "msg-main" },
          el("div", { class: "msg-role", text: (character && character.name) || "Jarvis" }),
          body, typing, el("div", { class: "msg-foot" }, statsNode, actions), host));
      Object.assign(rec, { root, text, owner, actions, regen, host, typing, statsNode, statsList: [] });
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

  // Ends the current assistant record: a still-running thinking block is closed first.
  closeCur() {
    if (this.cur) this.finishReasoning(this.cur);
    this.cur = null;
  }

  endTurn() {
    this.turnActive = false;
    for (const [node, rec] of [...this.records]) {
      this.finishReasoning(rec);
      rec.typing.remove();
      node.classList.remove("streaming");
      if (!rec.body.children.length && !rec.text()) { node.remove(); this.records.delete(node); }
    }
    this.cur = null;
    this.finishAssistants();
    this.syncActions();
  }

  // A saved assistant message from the snapshot: thinking, then text, then its speed footer.
  assistantItem(item) {
    if (item.reasoning) this.reasoningBlock(item.reasoning, item.stats?.thinking_seconds);
    if (item.content && item.content.trim()) this.addText(item.content);
    if (item.stats) this.addStats(item.stats);
  }

  addText(text) {
    this.finishReasoning(this.assistant());
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
      seg.node.replaceChildren(renderMarkdown(seg.text));
      this.scrollDown();
    });
  }

  // Live thinking: open, streaming, "Thinking… 3s"; collapses to "Thought for 3s" when the answer starts.
  addReasoning(text) {
    const cur = this.assistant();
    let r = cur.reasoning;
    if (!r) {
      const label = el("span", { class: "r-label", text: "Thinking…" });
      const timer = el("span", { class: "r-time", text: "0s" });
      const summary = el("summary", {}, icon("sparkle", 13), label, timer);
      const content = el("div", { class: "reasoning-text" });
      const node = el("details", { class: "reasoning live", open: true }, summary, content);
      r = { node, content, label, timer, started: performance.now(), manual: false, interval: 0 };
      summary.addEventListener("click", () => { r.manual = true; }); // the reader's choice wins over auto-collapse
      r.interval = setInterval(() => { timer.textContent = seconds((performance.now() - r.started) / 1000); }, 250);
      cur.reasoning = r;
      cur.root.classList.add("thinking");
      cur.body.append(node);
      cur.seg = null; // text after the thinking starts a new segment
    }
    r.content.textContent += r.content.textContent ? text : text.replace(/^\s+/, "");
    r.content.scrollTop = r.content.scrollHeight;
    this.scrollDown();
  }

  finishReasoning(cur) {
    const r = cur && cur.reasoning;
    if (!r) return;
    clearInterval(r.interval);
    r.label.textContent = `Thought for ${seconds((performance.now() - r.started) / 1000)}`;
    r.timer.textContent = "";
    r.node.classList.remove("live");
    if (!r.manual) r.node.open = false;
    cur.root.classList.remove("thinking");
    cur.lastThought = r.label; // the server's own timing replaces our estimate when stats arrive
    cur.reasoning = null;
  }

  // Saved thinking (after a reload): collapsed, with the recorded duration when known.
  reasoningBlock(text, secs) {
    const cur = this.assistant();
    const label = secs != null ? `Thought for ${seconds(secs)}` : "Reasoning";
    cur.body.append(el("details", { class: "reasoning" },
      el("summary", {}, icon("sparkle", 13), el("span", { class: "r-label", text: label })),
      el("div", { class: "reasoning-text", text })));
    cur.seg = null;
  }

  addStats(stats) {
    const cur = this.cur || this.lastAssistant();
    if (!cur || !stats) return;
    cur.statsList.push(stats);
    if (cur.lastThought && stats.thinking_seconds != null) {
      cur.lastThought.textContent = `Thought for ${seconds(stats.thinking_seconds)}`;
    }
    cur.lastThought = null;
    const { text, title } = statsLine(cur.statsList);
    cur.statsNode.textContent = text;
    cur.statsNode.title = title;
  }

  // ---------- tools ----------

  toolCard(item, running = false) {
    const cur = this.assistant();
    this.finishReasoning(cur);
    const state = running ? "running" : (item.status === "pending" ? "done" : item.status || "done");
    const status = el("span", { class: `tool-status ${state}`, text: state });
    const spin = el("span", { class: "spin", "aria-hidden": "true" });
    const hint = el("span", { class: "tool-hint", text: firstLine(item.args) });
    const summary = el("summary", {},
      el("span", { class: "tool-icon" }, icon("terminal", 13)),
      el("span", { class: "tool-name", text: item.name }),
      item.via ? el("span", { class: "badge", text: `via ${item.via}` }) : null,
      hint,
      spin, status, el("span", { class: "tool-time" }));
    const result = el("pre", { class: "tool-result", text: item.result ?? "" });
    const card = el("details", { class: `tool ${state}` }, summary,
      el("div", { class: "tool-detail" },
        el("div", { class: "tool-label", text: "arguments" }),
        argsView(item.args),
        el("div", { class: "tool-label", text: "result" }), result));
    cur.body.append(card);
    cur.seg = null;
    this.scrollDown();
    const entry = { card, summary, hint, status, result, time: summary.querySelector(".tool-time"), started: performance.now() };
    if (item.name === "spawn_agent" && item.result) this.subagentLink(entry, subagentOf(item.result));
    return entry;
  }

  toolStarted(data) {
    this.streamed = false;
    const card = this.toolCard({ name: data.name, args: data.args, via: data.via, status: "running" }, true);
    card.callId = data.call_id || "";
    if (card.callId) this.byCall.set(card.callId, card);
    if (!this.running.has(data.name)) this.running.set(data.name, []);
    this.running.get(data.name).push(card);
  }

  // "Open transcript" link on a spawn_agent card (the sub-agent's chat is #/chat/<session>).
  subagentLink(entry, sessionId) {
    if (!sessionId || entry.link) return;
    entry.link = el("a", { class: "tool-link", href: `#/chat/${encodeURIComponent(sessionId)}`, text: "Open transcript",
      onclick: (event) => event.stopPropagation() });
    entry.hint.after(entry.link);
  }

  subagentEvent(data, started) {
    const entry = this.byCall.get(data.call_id);
    if (!entry) return;
    this.subagentLink(entry, data.session_id);
    if (!started) {
      const n = data.tool_calls || 0;
      entry.hint.textContent = `${n} tool call${n === 1 ? "" : "s"}…${data.last_tool ? ` ${data.last_tool}` : ""}`;
    }
  }

  toolFinished(data) {
    // Match by call_id (parallel calls finish in any order); older events fall back to the name queue.
    let card = data.call_id ? this.byCall.get(data.call_id) : null;
    const queue = this.running.get(data.name) || [];
    if (card) {
      this.byCall.delete(data.call_id);
      const at = queue.indexOf(card);
      if (at >= 0) queue.splice(at, 1);
    } else {
      card = queue.shift() || this.toolCard({ name: data.name, args: data.args, via: data.via });
    }
    const text = typeof data.result === "string" ? data.result : formatJson(data.result);
    if (data.name === "spawn_agent") {
      this.subagentLink(card, subagentOf(text));
      card.hint.textContent = firstLine(data.args);
    }
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
    this.closeCur();
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
    this.finishReasoning(cur);
    cur.body.append(el("div", { class: "artifact" },
      el("div", { class: "artifact-title" }, el("strong", { text: artifact.title || "Artifact" }),
        el("span", { class: "badge", text: artifact.type })),
      artifactBody(artifact)));
    cur.seg = null;
    this.scrollDown();
  }

  errorBanner(data) {
    this.closeCur();
    const { message = "Unknown error", details = "", status } = typeof data === "string" ? { message: data } : data;
    const extra = details && details.trim() && details.trim() !== message.trim() ? details : "";
    this.add(el("div", { class: "error-banner", role: "alert" },
      el("div", { class: "error-head" }, icon("alert", 16), el("strong", { text: "Something went wrong" }),
        status ? el("span", { class: "badge", text: `HTTP ${status}` }) : null),
      el("p", { class: "error-text", text: message }),
      extra ? el("details", { class: "error-details" }, el("summary", { text: "Details" }), el("pre", { text: extra })) : null,
      el("div", { class: "error-actions" },
        el("button", { type: "button", class: "primary small", text: "Switch model/provider", onclick: () => this.hooks.switchModel() }),
        el("button", { type: "button", class: "small", text: "Retry", onclick: () => this.hooks.retry() }))));
  }

  commandOutput(command, output) {
    this.closeCur();
    this.add(el("div", { class: "cmd-output" },
      el("div", { class: "tool-label", text: command }),
      el("pre", { class: "tool-result", text: output || "(no output)" })));
    this.scrollDown(true);
  }

  note(text) {
    this.add(el("div", { class: "turn-note", text }));
  }

  // The tool budget ran out: offer to carry on (sends a normal message).
  continueNote() {
    const button = el("button", { type: "button", class: "small", text: "Continue", onclick: () => {
      node.remove();
      this.hooks.suggest("Continue where you left off.");
    } });
    const node = el("div", { class: "turn-note continue-note" },
      el("span", { text: "Tool budget for this turn was used up. " }), button);
    this.add(node);
  }

  // The model's reply was cut off at its output limit and is being retried with a larger one:
  // clear the half-streamed bubble (the cut-off text is discarded server-side too) and say why.
  supersede(data) {
    const cur = this.cur;
    if (!cur) return;
    this.finishReasoning(cur);
    cur.body.replaceChildren();
    cur.segs = [];
    cur.seg = null;
    cur.root.classList.remove("thinking");
    this.streamed = false;
    cur.body.append(el("div", { class: "retry-note", role: "status" }, icon("info", 14),
      el("span", { text: `The reply was cut off at ${Number(data.limit).toLocaleString()} tokens; retrying with a larger limit (${Number(data.new_limit).toLocaleString()}).` })));
    this.scrollDown();
  }

  // The reply was cut off again: say how to fix it and offer Continue.
  lengthNote(limit) {
    const button = el("button", { type: "button", class: "small", text: "Continue", onclick: () => {
      node.remove();
      this.hooks.suggest("Continue where you left off.");
    } });
    const message = limit
      ? [`The model ran out of output tokens (limit ${Number(limit).toLocaleString()}) before finishing. Raise `,
        el("em", { text: "Max output" }), " in the composer's model options, then press Continue."]
      : ["The model ran out of output tokens before finishing. Press Continue."];
    const node = el("div", { class: "turn-note continue-note length-note", role: "status" },
      icon("alert", 15), el("span", {}, ...message), button);
    this.add(node);
  }

  // ---------- live events ----------

  handle(msg) {
    const data = msg.data || {};
    switch (msg.kind) {
      case "user_message": {
        const users = this.root.querySelectorAll(".msg.user");
        const last = users[users.length - 1];
        const fresh = Date.now() - this.loadedAt < 2000;
        // A snapshot refetch may already contain this message: don't show it twice.
        if (fresh && last && last.querySelector(".msg-body").textContent === data.content) break;
        this.streamed = false;
        this.stick = true; // your own message always scrolls to the bottom
        // If the typing placeholder is already up (older event order), slot the message above it.
        const placeholder = this.turnActive && this.cur && !this.cur.body.children.length ? this.cur.root : null;
        this.userMessage(data.content, undefined, placeholder);
        break;
      }
      case "turn_started":
        for (const old of this.root.querySelectorAll(".error-banner, .continue-note")) old.remove(); // a retry replaces the old error
        this.beginTurn();
        break;
      case "assistant_delta": this.streamed = true; this.addText(data.content); break;
      case "assistant_reasoning_delta": this.addReasoning(data.content); break;
      case "assistant_message":
        if (data.content && !this.streamed) this.addText(data.content);
        break;
      case "tool_started": this.toolStarted(data); break;
      case "tool_finished": this.toolFinished(data); break;
      case "subagent_started": this.subagentEvent(data, true); break;
      case "subagent_progress": this.subagentEvent(data, false); break;
      case "artifact_created": this.artifactCard(data.artifact); break;
      case "approval_requested": this.approvalRequested(data); break;
      case "approval_finished": this.approvalFinished(data); break;
      case "output_truncated": if (data.retrying) this.supersede(data); break;
      case "assistant_stats": this.addStats(data.stats); break;
      case "error": this.errorBanner(data); break;
      case "turn_finished":
        if (data.status === "cancelled") this.note("Turn cancelled");
        if (data.status === "budget_exhausted") this.continueNote();
        if (data.status === "length") this.lengthNote(data.limit);
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
