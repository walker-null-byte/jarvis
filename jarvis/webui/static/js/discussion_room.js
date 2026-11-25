// A discussion room (#/discussions/<id>): agenda, live transcript, controls, composer, minutes.
import { api } from "./api.js";
import { statsLine } from "./chat.js";
import { confirmBox, el, formatJson } from "./dom.js";
import { ico, MODE_LABEL, modelBadge, partAvatar, statusChip } from "./discussion_common.js";
import { icon } from "./icons.js";
import { renderMarkdown } from "./markdown.js";

const MODERATOR = { name: "Moderator", color: "#94a6c3" };

// Follow the stream only while the reader is at the bottom (same behaviour as the chat).
class Follow {
  constructor(root, jump) {
    this.root = root; this.jump = jump; this.stick = true; this.last = 0;
    root.addEventListener("scroll", () => {
      const top = root.scrollTop;
      if (top < this.last - 1) this.stick = false;
      this.last = top;
      if (this.near()) this.stick = true;
      jump.hidden = this.stick;
    }, { passive: true });
    jump.addEventListener("click", () => { this.stick = true; root.scrollTo({ top: root.scrollHeight, behavior: "smooth" }); });
  }
  near() { return this.root.scrollHeight - this.root.scrollTop - this.root.clientHeight < 32; }
  down(force = false) {
    if (force) this.stick = true;
    else if (this.root.scrollTop < this.last - 1 && !this.near()) this.stick = false;
    if (this.stick) { this.root.scrollTo({ top: this.root.scrollHeight, behavior: "instant" }); this.last = this.root.scrollTop; }
  }
}

function toolChip(tool) {
  const state = tool.status || "done";
  const first = Object.values(tool.args || {}).find((v) => typeof v === "string") || "";
  const status = el("span", { class: `tool-status ${state}`, text: state });
  const result = el("pre", { class: "tool-result", text: tool.result ?? "" });
  const node = el("details", { class: `tool d-tool ${state}` },
    el("summary", {}, el("span", { class: "tool-icon" }, icon("terminal", 13)), el("span", { class: "tool-name", text: tool.name || "tool" }),
      el("span", { class: "tool-hint", text: first.split("\n")[0].slice(0, 90) }), el("span", { class: "spin", "aria-hidden": "true" }), status),
    el("div", { class: "tool-detail" }, el("div", { class: "tool-label", text: "arguments" }), el("pre", { class: "tool-result", text: formatJson(tool.args || {}, 1500) }),
      el("div", { class: "tool-label", text: "result" }), result));
  node.update = (t) => {
    const s = t.status || "done";
    node.className = `tool d-tool ${s}`;
    status.className = `tool-status ${s}`;
    status.textContent = s;
    result.textContent = t.result ?? "";
    if (s === "error") node.open = true;
  };
  return node;
}

export function mountRoom(container, id, ctx) {
  const host = container;
  host.classList.add("d-room-host");
  let data = null;                 // {discussion, transcript, live, running}
  let destroyed = false;
  const records = new Map();       // message id -> live/rendered bubble record
  const rendered = new Set();      // transcript entry ids already on screen
  const speaking = new Set();
  let people = new Map();
  let moderatorNote = null;
  const renderQueue = new Set();

  // ---------- skeleton ----------
  const back = el("button", { type: "button", class: "icon-btn", "aria-label": "Back to discussions", title: "Back", onclick: () => ctx.navigate("discussions") }, icon("left", 18));
  const titleNode = el("h2", { class: "d-title" });
  const modeNode = el("span", { class: "badge" });
  const statusHost = el("span", { class: "d-status-host" });
  const controls = el("div", { class: "d-controls", role: "toolbar", "aria-label": "Discussion controls" });
  const top = el("header", { class: "d-top" }, back, el("div", { class: "d-top-text" }, titleNode, el("div", { class: "d-top-meta" }, statusHost, modeNode)), controls);
  const side = el("aside", { class: "d-side", "aria-label": "Agenda" });
  const sideToggle = el("button", { type: "button", class: "d-side-toggle", "aria-expanded": "false", onclick: () => {
    const open = !side.classList.contains("open");
    side.classList.toggle("open", open);
    sideToggle.setAttribute("aria-expanded", String(open));
  } }, icon("book", 15), el("span", { class: "d-side-toggle-text" }), icon("down", 14));
  const transcript = el("div", { class: "d-transcript", role: "log", "aria-label": "Discussion transcript", "aria-live": "polite", tabindex: "0" });
  const jump = el("button", { type: "button", class: "jump", hidden: true, "aria-label": "Jump to latest message" }, icon("arrow-down", 15), "Jump to latest");
  const follow = new Follow(transcript, jump);
  const banner = el("div", { class: "d-banner", role: "status" });
  const input = el("textarea", { class: "d-input", rows: 1, placeholder: "Speak to the group…  (@Name makes someone answer next)", "aria-label": "Message to the group (Enter to send, Shift+Enter for a new line)", maxlength: 4000 });
  const send = el("button", { type: "submit", class: "round primary", "aria-label": "Send", title: "Send (Enter)" }, icon("send", 18));
  const mentions = el("div", { class: "suggest d-mentions", role: "listbox", "aria-label": "Mention a participant", hidden: true });
  const composer = el("form", { class: "d-composer", autocomplete: "off", onsubmit: (e) => { e.preventDefault(); sendMessage(); } },
    mentions, el("div", { class: "composer-box" }, input, el("div", { class: "composer-actions" }, send)),
    el("p", { class: "composer-hint", text: "Enter to send · Shift+Enter for a new line · @Name to ask someone directly" }));
  host.replaceChildren(el("div", { class: "d-room" }, top, sideToggle,
    el("div", { class: "d-body" }, side, el("section", { class: "d-main" }, el("div", { class: "d-stage" }, transcript, jump), banner, composer))));
  const d = () => data.discussion;

  // ---------- loading ----------
  async function load() {
    try {
      const fresh = await api(`/discussions/${encodeURIComponent(id)}`);
      if (destroyed) return;
      data = fresh;
      people = new Map(d().participants.map((p) => [p.id, p]));
      records.clear(); rendered.clear(); speaking.clear();
      moderatorNote = null;
      transcript.replaceChildren();
      data.transcript.forEach(addEntry);
      data.live.forEach((l) => liveBubble(l.participant, l.message_id, l.text));
      ctx.setTitle(d().title);
      drawAll();
      follow.down(true);
    } catch (error) {
      if (destroyed) return;
      host.replaceChildren(el("div", { class: "d-room" }, el("div", { class: "view-empty" }, el("h3", { text: error.status === 404 ? "Discussion not found" : "Could not open the discussion" }),
        el("p", { class: "note", text: error.message }), el("button", { type: "button", onclick: () => ctx.navigate("discussions") }, "Back to discussions"))));
    }
  }

  // ---------- header, controls, banner ----------
  function drawAll() { drawTop(); drawSide(); drawBanner(); }

  function drawTop() {
    titleNode.textContent = d().title;
    modeNode.textContent = MODE_LABEL[d().mode] || d().mode;
    statusHost.replaceChildren(statusChip(d().status));
    drawControls();
  }

  async function act(action, label) {
    try { await api(`/discussions/${id}/${action}`, { method: "POST" }); }
    catch (error) { ctx.toast(error.message, "error"); }
  }

  function button(label, iconName, onclick, cls = "", hint = "") {
    return el("button", { type: "button", class: `d-ctl ${cls}`.trim(), title: hint || label, "aria-label": label, onclick }, ico(iconName, 16), el("span", { class: "d-ctl-text", text: label }));
  }

  function drawControls() {
    const s = d().status;
    const items = d().agenda.items.length;
    const active = ["running", "waiting", "paused"].includes(s);
    const list = [];
    if (s === "draft") list.push(button("Start", "play", () => act("start"), "primary", "Start the discussion"));
    if (s === "running" || s === "waiting") list.push(button("Pause", "pause", () => act("pause"), "", "Pause after the current speaker"));
    if (s === "paused") list.push(button("Resume", "play", () => act("resume"), "primary"));
    if (active && d().current_item < items) list.push(button("Next item", "skip", () => act("next"), "", "Wrap up this item and move on"));
    if (active || (s === "finished" && !d().minutes)) list.push(button("Conclude", "flag", () => act("conclude"), "", "Stop discussing and write the minutes now"));
    if (active) list.push(button("Stop", "stop", async () => {
      if (await confirmBox("Stop the discussion?", "The current speaker is cancelled and the discussion ends without minutes. You can still press Conclude afterwards to write them.")) act("stop");
    }, "danger", "Cancel the speaker and end the discussion"));
    if (s !== "draft") list.push(el("a", { class: "button d-ctl", href: `/api/discussions/${encodeURIComponent(id)}/export.md`, download: "", title: "Export transcript and minutes as Markdown", "aria-label": "Export Markdown" }, icon("download", 16), el("span", { class: "d-ctl-text", text: "Export" })));
    controls.replaceChildren(...list);
  }

  function drawBanner() {
    const s = d().status;
    const text = { draft: "Press Start, or send your opening message below to begin.",
      paused: "Paused. Press Resume to continue; you can still send messages.",
      waiting: "The moderator is waiting for your answer.",
      finished: d().minutes ? "This discussion is finished. Add an agenda item in the sidebar to continue it." : "This discussion has ended. Press Conclude to write the minutes." }[s] || "";
    banner.textContent = text;
    banner.hidden = !text;
    banner.className = `d-banner ${s}`;
    const locked = s === "finished";
    input.disabled = locked;
    send.disabled = locked;
    input.placeholder = locked ? "Finished" : s === "draft" ? "Your first message opens the discussion…" : "Speak to the group…  (@Name makes someone answer next)";
  }

  // ---------- agenda sidebar ----------
  function drawSide() {
    const dd = d();
    const finished = dd.status === "finished";
    const items = dd.agenda.items;
    const current = dd.status === "draft" ? -1 : dd.current_item;
    sideToggle.querySelector(".d-side-toggle-text").textContent = finished ? `Agenda · ${items.length}/${items.length} done` : `Agenda · item ${Math.min(current + 1, items.length) || 1} of ${items.length}`;
    const list = el("ol", { class: "d-agenda" }, items.map((item, i) => {
      const state = i < current || (finished && i < items.length) ? "done" : i === current ? "current" : "upcoming";
      return el("li", { class: `d-ag ${state}`, "aria-current": state === "current" ? "step" : null },
        el("span", { class: "d-ag-mark", "aria-hidden": "true" }, state === "done" ? icon("check", 12) : String(i + 1)),
        el("div", { class: "d-ag-text" }, el("span", { class: "d-ag-title", text: item.title }), el("span", { class: "sr-only", text: ` (${state})` }),
          item.notes ? el("span", { class: "note", text: item.notes }) : null,
          item.summary ? el("details", { class: "d-ag-summary" }, el("summary", { text: "Summary" }), el("div", { class: "md" }, renderMarkdown(item.summary))) : null));
    }));
    const members = el("ul", { class: "d-members" }, dd.participants.map((p) => el("li", { class: speaking.has(p.id) ? "speaking" : "", dataset: { pid: p.id } },
      partAvatar(p, 24), el("span", { class: "d-member-name", text: p.name }),
      el("span", { class: "d-member-model", text: [p.provider, p.model].filter(Boolean).join(" · ") || "current model" }),
      p.stance ? el("span", { class: "badge", text: p.stance }) : null)));
    const addTitle = el("input", { type: "text", placeholder: "New agenda item", "aria-label": "New agenda item", maxlength: 300 });
    const addForm = ["finished", "paused", "draft"].includes(dd.status) ? el("form", { class: "d-add-item", onsubmit: async (e) => {
      e.preventDefault();
      if (!addTitle.value.trim()) return;
      try { await api(`/discussions/${id}/items`, { method: "POST", body: { title: addTitle.value } }); addTitle.value = ""; await load(); }
      catch (error) { ctx.toast(error.message, "error"); }
    } }, addTitle, el("button", { type: "submit", class: "small", "aria-label": "Add agenda item" }, icon("plus", 14), finished ? "Continue" : "Add")) : null;
    side.replaceChildren(
      el("h4", { text: "Goal" }), el("p", { class: "d-goal-full", text: dd.agenda.goal }),
      el("h4", { text: "Agenda" }), list, ...(addForm ? [addForm] : []),
      el("h4", { text: "Participants" }), members);
  }

  function markSpeaking() {
    side.querySelectorAll(".d-members li").forEach((li) => li.classList.toggle("speaking", speaking.has(li.dataset.pid)));
  }

  // ---------- transcript ----------
  const personOf = (e) => people.get(e.speaker) || (e.speaker === "moderator" ? MODERATOR : { name: e.name || "?", color: "#94a6c3" });

  function queueRender(rec) {
    if (renderQueue.has(rec)) return;
    renderQueue.add(rec);
    requestAnimationFrame(() => {
      renderQueue.delete(rec);
      rec.body.replaceChildren(renderMarkdown(rec.text));
      follow.down();
    });
  }

  function bubble(person, mid, extra = {}) {
    const body = el("div", { class: "msg-body d-text" });
    const chips = el("div", { class: "d-chips" });
    const thinking = el("div", { class: "d-thinking", role: "status", "aria-label": `${person.name} is thinking` }, el("span", { class: "typing" }, el("span"), el("span"), el("span")), "thinking…");
    const stats = el("span", { class: "msg-stats" });
    const tags = el("span", { class: "d-tags" });
    const head = el("div", { class: "d-msg-head" }, el("span", { class: "d-name", text: person.name }),
      modelBadge(extra.provider ?? person.provider, extra.model ?? person.model), tags);
    const node = el("article", { class: "d-msg", dataset: { id: mid } },
      partAvatar(person, 32),
      el("div", { class: "msg-main" }, head, chips, body, thinking, el("div", { class: "msg-foot" }, stats)));
    node.style.setProperty("--c", person.color || "#2dd4e8");
    const rec = { node, body, chips, thinking, stats, tags, head, text: "", tools: [] };
    records.set(mid, rec);
    transcript.append(node);
    follow.down();
    return rec;
  }

  function tag(rec, text, cls = "") { rec.tags.append(el("span", { class: `d-tag ${cls}`.trim(), text })); }

  function liveBubble(pid, mid, text = "") {
    if (records.has(mid)) return records.get(mid);
    const person = people.get(pid) || { name: pid, color: "#94a6c3" };
    const rec = bubble(person, mid);
    rec.node.classList.add("streaming");
    if (text) { rec.text = text; rec.thinking.hidden = true; queueRender(rec); }
    speaking.add(pid);
    rec.pid = pid;
    markSpeaking();
    return rec;
  }

  function finalize(rec, e) {
    rec.node.classList.remove("streaming");
    rec.thinking.remove();
    rec.text = e.text || "";
    if (e.pass) { rec.node.classList.add("pass"); tag(rec, "passes", "mute"); }
    if (e.interrupted) tag(rec, "interrupted", "warn");
    if (e.forced) tag(rec, "asked by you");
    if (e.round !== undefined && e.slot && /^\d+:\d+:/.test(e.slot) && d().rounds_per_item > 1) tag(rec, `round ${Number(e.slot.split(":")[1]) + 1}`, "mute");
    if (!rec.tools.length) (e.tools || []).forEach((t) => { const chip = toolChip(t); rec.chips.append(chip); rec.tools.push({ t, chip }); });
    const { text, title } = statsLine(e.stats || []);
    rec.stats.textContent = text;
    rec.stats.title = title;
    rec.body.replaceChildren(renderMarkdown(rec.text));
    if (rec.pid) { speaking.delete(rec.pid); markSpeaking(); }
  }

  function addEntry(e) {
    if (rendered.has(e.id)) return;
    rendered.add(e.id);
    clearModeratorNote();
    let node = null;
    if (e.kind === "message") {
      const person = personOf(e);
      const rec = records.get(e.id) || bubble(person, e.id, { provider: e.provider, model: e.model });
      rec.pid = rec.pid || e.speaker;
      finalize(rec, e);
      if (e.asks_user) tag(rec, "asks you", "warn");
      follow.down();
      return;
    }
    if (e.kind === "user") {
      node = el("div", { class: "msg user d-user" }, el("div", { class: "msg-main" },
        e.opening ? el("div", { class: "d-tag mute", text: "opening remark" }) : null,
        el("div", { class: "msg-body plain", text: e.text })));
    } else if (e.kind === "system") {
      node = el("div", { class: `d-note ${e.level === "error" ? "error" : ""}`, role: e.level === "error" ? "alert" : null },
        icon(e.level === "error" ? "alert" : "info", 14), el("span", { text: e.text }));
    } else if (e.kind === "moderator") {
      node = el("div", { class: "d-mod" }, partAvatar(MODERATOR, 18), el("span", { text: e.text }));
    } else if (e.kind === "summary") {
      const item = d().agenda.items[e.item];
      node = el("section", { class: "d-summary", "aria-label": `Summary of item ${e.item + 1}` },
        el("div", { class: "d-summary-head" }, icon("check", 14), el("strong", { text: `Item ${e.item + 1} summary` }), item ? el("span", { class: "note", text: item.title }) : null,
          el("span", { class: "note d-by", text: `by ${e.name}` })),
        el("div", { class: "md" }, renderMarkdown(e.text)));
    } else if (e.kind === "minutes") {
      node = minutesCard(e.text);
    }
    if (node) { transcript.append(node); follow.down(); }
  }

  function minutesCard(text) {
    transcript.querySelector(".d-minutes")?.remove();
    const copy = el("button", { type: "button", class: "small", onclick: async () => {
      try { await navigator.clipboard.writeText(text); ctx.toast("Minutes copied"); } catch { ctx.toast("Could not copy", "error"); }
    } }, icon("copy", 14), "Copy");
    return el("section", { class: "d-minutes", "aria-label": "Minutes" },
      el("div", { class: "d-minutes-head" }, icon("book", 16), el("h3", { text: "Minutes" }), el("span", { class: "spacer" }), copy,
        el("a", { class: "button small", href: `/api/discussions/${encodeURIComponent(id)}/export.md`, download: "" }, icon("download", 14), "Export .md")),
      el("div", { class: "md" }, renderMarkdown(text)));
  }

  function clearModeratorNote() { moderatorNote?.remove(); moderatorNote = null; }
  function showModeratorNote(who) {
    clearModeratorNote();
    moderatorNote = el("div", { class: "d-mod live", role: "status" }, el("span", { class: "typing" }, el("span"), el("span"), el("span")), el("span", { text: `${who} is writing…` }));
    transcript.append(moderatorNote);
    follow.down();
  }

  // ---------- live events ----------
  function onEvent(msg) {
    if (msg.kind === "snapshot") { load(); return; }  // the stream reconnected: re-sync
    const m = msg.data || {};
    if (m.discussion_id !== id || !data) return;
    switch (msg.kind) {
      case "discussion_status":
        if (m.updated) { load(); return; }
        d().status = m.status;
        d().current_item = m.current_item;
        if (m.status !== "running") { speaking.clear(); markSpeaking(); }
        drawTop(); drawSide(); drawBanner();
        break;
      case "discussion_speaker":
        if (!m.participant) { clearModeratorNote(); break; }
        if (m.participant === "moderator") showModeratorNote(m.who || "Moderator");
        else liveBubble(m.participant, m.message_id);
        break;
      case "discussion_delta": {
        const rec = records.get(m.message_id) || liveBubble(m.participant, m.message_id);
        rec.text += m.content;
        rec.thinking.hidden = true;
        queueRender(rec);
        break;
      }
      case "discussion_tool": {
        const rec = records.get(m.message_id);
        if (!rec) break;
        const t = m.tool || {};
        if (m.phase === "tool_started") {
          const chip = toolChip(t);
          rec.chips.append(chip);
          rec.tools.push({ t, chip });
          rec.thinking.hidden = true;
        } else {
          const open = rec.tools.find((x) => x.t.name === t.name && x.t.status === "running") || rec.tools.find((x) => x.t.name === t.name);
          if (open) { open.t.status = t.status; open.chip.update(t); }
          else { const chip = toolChip(t); rec.chips.append(chip); rec.tools.push({ t, chip }); }
          rec.thinking.hidden = !!rec.text;
        }
        follow.down();
        break;
      }
      case "discussion_message": addEntry(m.message); break;
      case "discussion_dropped": {
        const rec = records.get(m.message_id);
        if (rec) { rec.node.remove(); records.delete(m.message_id); }
        speaking.delete(m.participant); markSpeaking();
        break;
      }
      case "discussion_item": {
        const item = d().agenda.items[m.item];
        if (item) item.summary = m.summary;
        d().current_item = m.current_item;
        drawSide();
        break;
      }
      case "discussion_minutes": d().minutes = m.minutes; drawControls(); drawBanner(); break;
    }
  }

  // ---------- composer ----------
  async function sendMessage() {
    const text = input.value.trim();
    if (!text || input.disabled) return;
    input.value = "";
    autosize();
    closeMentions();
    try {
      const out = await api(`/discussions/${id}/message`, { method: "POST", body: { text } });
      if (out.message) addEntry(out.message);
    } catch (error) { input.value = text; autosize(); ctx.toast(error.message, "error"); }
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  let mentionIndex = 0;
  let mentionRange = null;
  function mentionMatches() {
    const before = input.value.slice(0, input.selectionStart);
    const match = /(^|\s)@([\w'’ -]*)$/.exec(before);
    if (!match) return null;
    const needle = match[2].toLowerCase();
    const list = d().participants.filter((p) => p.name.toLowerCase().startsWith(needle) || p.name.toLowerCase().split(" ").some((w) => w.startsWith(needle)));
    return { list, start: before.length - match[2].length - 1, end: input.selectionStart };
  }
  function closeMentions() { mentions.hidden = true; mentionRange = null; input.removeAttribute("aria-activedescendant"); }
  function drawMentions() {
    const found = data && mentionMatches();
    if (!found || !found.list.length) { closeMentions(); return; }
    mentionRange = found;
    mentionIndex = Math.min(mentionIndex, found.list.length - 1);
    mentions.replaceChildren(...found.list.map((p, i) => el("div", { class: `suggest-item${i === mentionIndex ? " hl" : ""}`, role: "option", id: `d-mention-${i}`, "aria-selected": String(i === mentionIndex),
      onpointerdown: (e) => { e.preventDefault(); pickMention(p); } }, partAvatar(p, 20), el("span", { text: p.name }))));
    input.setAttribute("aria-activedescendant", `d-mention-${mentionIndex}`);
    mentions.hidden = false;
  }
  function pickMention(person) {
    const r = mentionRange;
    if (!r) return;
    input.value = `${input.value.slice(0, r.start)}@${person.name} ${input.value.slice(r.end)}`;
    const caret = r.start + person.name.length + 2;
    input.setSelectionRange(caret, caret);
    closeMentions();
    input.focus();
  }
  input.addEventListener("input", () => { autosize(); mentionIndex = 0; drawMentions(); });
  input.addEventListener("click", drawMentions);
  input.addEventListener("keydown", (e) => {
    if (!mentions.hidden && mentionRange) {
      const count = mentions.children.length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); mentionIndex = (mentionIndex + (e.key === "ArrowDown" ? 1 : count - 1)) % count; drawMentions(); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pickMention(mentionMatches().list[mentionIndex]); return; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMentions(); return; }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
  });
  input.addEventListener("blur", () => setTimeout(closeMentions, 120));

  const off = ctx.bus.on(onEvent);
  load();
  return () => { destroyed = true; off(); host.classList.remove("d-room-host"); };
}
