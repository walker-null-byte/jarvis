// Bootstrap and shared state: wires the event stream to the transcript, rail,
// header, orb, voice, composer and drawer.
import { api, EventStream } from "./api.js";
import { avatar } from "./avatar.js";
import { Chat } from "./chat.js";
import { el, toast } from "./dom.js";
import { icon, setIcon } from "./icons.js";
import { openOnboarding } from "./onboarding.js";
import { Orb } from "./orb.js";
import { Drawer, renderActivity } from "./panels.js";
import { renderCharacters } from "./panel_characters.js";
import { renderMemory, renderMind } from "./panel_mind.js";
import { renderSettings } from "./panel_settings.js";
import { renderVoice } from "./panel_voice.js";
import { navigate, parseHash, replaceRoute, startRouter } from "./router.js";
import { SessionList } from "./sessions.js";
import { Switcher } from "./switcher.js";
import { createBus, currentViewName, mountView, unmountView } from "./views.js";
import "./view_agents.js";
import "./view_discussions.js";
import "./view_routines.js";
import { initRoutineNotifier } from "./routine_notify.js";
import "./view_tools.js";
import { handleDownloadEvent, showDownloadPrompt, Voice } from "./voice.js";

const $ = (id) => document.getElementById(id);
const bus = createBus();
initRoutineNotifier(bus); // sidebar badge + toasts for finished routines, on every page

const ctx = {
  settings: {}, status: null, sessionId: "", busy: false, characters: [],
  commands: [], hasUserMessage: false, voiceInfo: null, providerLabels: {},
};

// ---------- icons in static markup ----------

for (const node of document.querySelectorAll("[data-icon]")) {
  node.prepend(icon(node.dataset.icon, Number(node.dataset.size) || 16));
}

// ---------- orb / busy state ----------

const orb = new Orb($("orb"));
let heroOrb = null;
const orbs = () => [orb, heroOrb].filter(Boolean);
window.addEventListener("resize", () => orbs().forEach((o) => o.resize()));

const voiceState = { listening: false, speaking: false };
let toolsRunning = 0;

function updateOrb() {
  let state = "idle";
  if (voiceState.listening) state = "listening";
  else if (voiceState.speaking) state = "speaking";
  else if (ctx.busy) state = toolsRunning > 0 ? "working" : "thinking";
  orbs().forEach((o) => o.setState(state));
  $("orb-state").textContent = state;
  const busy = $("busy");
  busy.className = `busy ${ctx.busy ? "active" : "idle"}`;
  $("busy-text").textContent = ctx.busy ? (toolsRunning > 0 ? "working" : "thinking") : "idle";
}

// ---------- voice (browser playback) ----------

const voice = new Voice({
  onSpeaking(on) { voiceState.speaking = on; updateOrb(); },
  onLevel(level) { orbs().forEach((o) => o.setAmplitude(level)); },
});

async function speakText(text, owner, host) {
  const result = await voice.speak(text, owner, host);
  if (result.status === "needs_download") {
    showDownloadPrompt(host, result.info, () => speakText(text, owner, host));
  }
  return result;
}
ctx.previewVoice = (text, host) => speakText(text, { setState() {} }, host);

function renderAutoSpeak() {
  const on = !!ctx.voiceInfo?.auto_speak;
  const btn = $("autospeak");
  btn.setAttribute("aria-pressed", String(on));
  btn.setAttribute("aria-label", on ? "Auto-speak is on. Click to turn off" : "Auto-speak is off. Click to turn on");
  btn.title = on ? "Auto-speak: on" : "Auto-speak: off";
  setIcon(btn, on ? "speaker" : "speaker-off", 17);
  btn.classList.toggle("on", on);
}

async function setAutoSpeak(on) {
  voice.unlock();
  if (!on) {
    voice.stop();
    try { ctx.voiceInfo = { ...(ctx.voiceInfo || {}), ...(await api("/voice/settings", { method: "POST", body: { auto_speak: false } })) }; }
    catch (error) { toast(error.message, "error"); }
    ctx.voiceInfo.auto_speak = false;
    renderAutoSpeak();
    return;
  }
  let info = ctx.voiceInfo;
  try { info = await api("/voice"); ctx.voiceInfo = info; } catch (error) { toast(error.message, "error"); return; }
  const enable = async () => {
    try {
      await api("/voice/settings", { method: "POST", body: { auto_speak: true } });
      ctx.voiceInfo = { ...ctx.voiceInfo, auto_speak: true };
      renderAutoSpeak();
      toast("Auto-speak is on");
    } catch (error) { toast(error.message, "error"); }
  };
  if (info.ready === false) {
    const item = (info.models || []).find((m) => m.id === info.missing);
    const dialog = el("dialog", { class: "modal", "aria-label": "Download a voice model" });
    const host = el("div", {});
    const close = () => { dialog.close(); dialog.remove(); };
    dialog.append(el("h3", { text: "Turn on Auto-speak" }),
      el("p", { class: "note", text: "Jarvis needs a speech voice before it can read replies aloud. It downloads once and stays on your computer." }),
      host, el("div", { class: "modal-actions" }, el("button", { text: "Not now", onclick: close })));
    document.body.append(dialog);
    dialog.addEventListener("cancel", close);
    dialog.showModal();
    showDownloadPrompt(host, { needs_download: info.missing, label: item?.label, size_mb: item?.size_mb }, async () => { close(); await enable(); });
    return;
  }
  await enable();
}
ctx.requestAutoSpeak = () => setAutoSpeak(true);
$("autospeak").addEventListener("click", () => setAutoSpeak(!ctx.voiceInfo?.auto_speak));

function autoSpeakReply() {
  if (!ctx.voiceInfo?.auto_speak) return;
  const rec = chat.lastAssistant();
  if (rec && rec.text()) speakText(rec.text(), rec.owner, rec.host);
}

// ---------- chat ----------

const chat = new Chat($("transcript"), $("jump"), {
  settings: () => ctx.settings,
  busy: () => ctx.busy,
  character: () => ctx.status?.character,
  speak: speakText,
  regenerate: async () => {
    voice.stop();
    try { await api("/regenerate", { method: "POST" }); } catch (error) { toast(error.message, "error"); }
  },
  edit: async (index, text) => {
    voice.stop();
    await api(`/messages/${index}/edit`, { method: "POST", body: { text } });
  },
  switchModel: () => ctx.switcher.open(),
  retry: async () => {
    try { await api("/retry", { method: "POST" }); } catch (error) { toast(error.message, "error"); }
  },
  suggest: (text) => { input.value = text; autosize(); send(); },
  heroGone: () => { heroOrb?.stop(); heroOrb = null; },
});

const sessions = new SessionList($("sessions"), $("session-search"), ctx);

function setBusy(value) {
  ctx.busy = value;
  if (!value) toolsRunning = 0;
  $("stop").hidden = !value;
  $("send").hidden = value;
  $("stop").disabled = false;
  $("stop").title = "Stop (Esc)";
  chat.syncActions();
  updateOrb();
}

// ---------- header ----------

function shortLabel(id) {
  return (ctx.providerLabels[id] || id || "").split(" — ")[0];
}

function updateHeader() {
  const s = ctx.status;
  if (!s) return;
  ctx.sessionId = s.session?.id || ctx.sessionId;
  if (!viewName) {
    $("title").textContent = s.session?.title || "New conversation";
    document.title = `${s.session?.title || "Jarvis"} · Jarvis`;
    if (routeReady && ctx.sessionId) replaceRoute(`chat/${encodeURIComponent(ctx.sessionId)}`);
  }
  $("model-label").textContent = `${shortLabel(s.provider)} · ${s.model || "no model"}`;
  $("model-pill").title = "Switch model or provider";
  const btn = $("character-btn");
  btn.replaceChildren(avatar(s.character || { name: "J" }, 22), el("span", { class: "pill-text", text: s.character?.name || "Jarvis" }), icon("down", 13));
}

async function loadProviderLabels() {
  try {
    const { providers } = await api("/providers");
    ctx.providerLabels = Object.fromEntries(providers.map((p) => [p.id, p.label || p.id]));
    updateHeader();
  } catch { /* labels fall back to ids */ }
}

async function refreshStatus() {
  try {
    ctx.status = await api("/status");
    updateHeader();
  } catch (error) { handleApiError(error); }
}
ctx.refreshStatus = refreshStatus;
ctx.setStatus = (status) => { ctx.status = status; updateHeader(); };

function handleApiError(error) {
  if (error.status === 401) showBanner("Session expired. Open the launch URL printed in the terminal again.");
}

function showBanner(text) {
  const banner = $("banner");
  banner.hidden = !text;
  banner.textContent = text || "";
}

// ---------- snapshot ----------

let tourShown = false;
let joinedMidTurn = false;

async function applySnapshot(snap) {
  voice.stop();
  ctx.status = snap.status;
  ctx.sessionId = snap.session.id;
  if (snap.voice) ctx.voiceInfo = snap.voice;
  ctx.hasUserMessage = snap.history.some((item) => item.type === "user");
  chat.load(snap.history, snap.approvals || []);
  chat.assignIndexes(snap.history);
  setBusy(!!snap.busy);
  joinedMidTurn = !!snap.busy; // what streamed before we connected is only in the saved history
  if (snap.busy) chat.beginTurn();
  updateHeader();
  renderAutoSpeak();
  if (chat.isEmpty()) showHero(snap.character);
  sessions.refresh();
  if (snap.onboarded === false && !tourShown) { tourShown = true; openOnboarding(ctx); }
}
ctx.applySnapshot = applySnapshot;
ctx.reloadSnapshot = async () => applySnapshot(await api("/snapshot"));
ctx.navigate = navigate;
ctx.closeRail = () => document.body.classList.remove("rail-open");
ctx.showTour = () => openOnboarding(ctx);

const cardCache = new Map();
async function showHero(character) {
  let greeting = "";
  if (character) {
    try {
      let card = cardCache.get(character.id);
      if (!card) { card = await api(`/characters/${encodeURIComponent(character.id)}`); cardCache.set(character.id, card); }
      greeting = card.greeting || card.first_mes || "";
    } catch { /* characters unavailable: default greeting */ }
  }
  if (!chat.isEmpty()) return;
  const canvas = chat.showHero(character, greeting);
  heroOrb = new Orb(canvas);
  updateOrb();
}

// ---------- live events ----------

// Events that concern the whole app, not one chat session.
const GLOBAL_KINDS = new Set([
  "snapshot", "session_started", "config_changed", "character_changed",
  "voice_download", "voice_listening", "voice_amplitude", "voice_state",
]);
const approvalToasts = new Map();

function onMessage(msg) {
  bus.emit(msg);
  const d = msg.data || {};
  if (!GLOBAL_KINDS.has(msg.kind) && msg.session_id && ctx.sessionId && msg.session_id !== ctx.sessionId) {
    otherSessionEvent(msg);
    return;
  }
  if (msg.kind === "session_started" && d.session_kind && d.session_kind !== "chat") return; // a background agent, not this chat
  switch (msg.kind) {
    case "snapshot": applySnapshot(d); showBanner(""); return;
    case "session_started": if (msg.session_id !== ctx.sessionId) ctx.reloadSnapshot().catch(handleApiError); break;
    case "character_changed": cardCache.clear(); ctx.reloadSnapshot().catch(handleApiError); break;
    case "history_truncated": ctx.reloadSnapshot().catch(handleApiError); break;
    case "config_changed": refreshStatus(); break;
    case "turn_started": setBusy(true); break;
    case "tool_started": toolsRunning++; updateOrb(); break;
    case "tool_finished": toolsRunning = Math.max(0, toolsRunning - 1); updateOrb(); break;
    case "user_message": ctx.hasUserMessage = true; voice.stop(); refreshStatus(); sessions.refresh(); break; // the title is set by the first message
    case "busy_changed": setBusy(!!d.busy); if (!d.busy) chat.endTurn(); break;
    case "voice_download": handleDownloadEvent(d); break;
    case "voice_listening": voiceState.listening = !!d.active; updateOrb(); break;
    case "voice_amplitude": case "voice_state": return; // the browser drives the orb while speaking
  }
  if (msg.kind === "approval_requested") toast("Jarvis needs your approval", "info");
  chat.handle(msg);
  if (msg.kind === "turn_finished") {
    chat.endTurn();
    if (joinedMidTurn) { joinedMidTurn = false; ctx.reloadSnapshot().catch(handleApiError); }
    updateOrb();
    refreshIndexes();
    sessions.refresh();
    if (d.status !== "cancelled" && d.status !== "error") autoSpeakReply();
  }
  drawer.notify(msg);
}

// An approval for a session that is not on screen must not hang silently: show a toast.
function otherSessionEvent(msg) {
  const d = msg.data || {};
  if (msg.kind === "approval_finished") { approvalToasts.get(d.request_id)?.remove(); approvalToasts.delete(d.request_id); return; }
  if (msg.kind !== "approval_requested" || approvalToasts.has(d.request_id)) return;
  const decide = async (approved) => {
    node.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try { await api(`/approvals/${d.request_id}`, { method: "POST", body: { approved } }); }
    catch (error) { toast(error.message, "error"); }
    node.remove();
    approvalToasts.delete(d.request_id);
  };
  const node = el("div", { class: "toast approval-toast", role: "alertdialog", "aria-label": "Approval required" },
    el("span", { text: `A background session needs approval: ${d.action || "action"}` }),
    el("div", { class: "approval-actions" },
      el("button", { class: "primary small", text: "Approve", onclick: () => decide(true) }),
      el("button", { class: "danger small", text: "Deny", onclick: () => decide(false) })));
  $("toasts").append(node);
  approvalToasts.set(d.request_id, node);
  const limit = (Number(ctx.settings["security.approval_timeout"]) || 120) * 1000 + 5000;
  setTimeout(() => { node.remove(); approvalToasts.delete(d.request_id); }, limit);
}

// After a turn, message indexes are needed for editing; the DOM is not re-rendered.
async function refreshIndexes() {
  try {
    const snap = await api("/snapshot");
    ctx.status = snap.status;
    updateHeader();
    chat.assignIndexes(snap.history);
  } catch (error) { handleApiError(error); }
}

new EventStream(onMessage, (connected) => {
  if (!connected) showBanner("Connection lost. Reconnecting…");
}).start();

// ---------- drawer ----------

const drawer = new Drawer($("drawer"), [
  { id: "activity", label: "Activity", icon: "activity", render: renderActivity },
  { id: "characters", label: "Characters", icon: "users", render: renderCharacters },
  { id: "voice", label: "Voice", icon: "speaker", render: renderVoice },
  { id: "mind", label: "Mind", icon: "brain", render: renderMind },
  { id: "memory", label: "Memory", icon: "book", render: renderMemory },
  { id: "settings", label: "Settings", icon: "sliders", render: renderSettings },
], ctx);

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage blocked */ } },
};
function setDrawer(open) {
  document.body.classList.toggle("drawer-open", open);
  if (!window.matchMedia("(max-width: 1100px)").matches) store.set("jarvis.drawer", open ? "open" : "closed");
  $("toggle-drawer").setAttribute("aria-expanded", String(open));
}
ctx.closeDrawer = () => setDrawer(false);
ctx.openTab = (tab) => { setDrawer(true); drawer.show(tab); };
const TABS = ["activity", "characters", "voice", "mind", "memory", "settings"];
const savedTab = store.get("jarvis.tab");
drawer.show(TABS.includes(savedTab) ? savedTab : "characters");
setDrawer(store.get("jarvis.drawer") === "open" && !window.matchMedia("(max-width: 1100px)").matches);
$("toggle-drawer").addEventListener("click", () => setDrawer(!document.body.classList.contains("drawer-open")));
$("toggle-rail").addEventListener("click", () => {
  const open = document.body.classList.toggle("rail-open");
  $("toggle-rail").setAttribute("aria-expanded", String(open));
});
$("scrim").addEventListener("click", () => { ctx.closeRail(); setDrawer(false); });

// ---------- model switcher & character switcher ----------

ctx.switcher = new Switcher($("model-pill"), $("model-menu"), ctx);

const characterMenu = $("character-menu");
function closeCharacterMenu() {
  characterMenu.hidden = true;
  $("character-btn").setAttribute("aria-expanded", "false");
}
$("character-btn").addEventListener("click", async (event) => {
  event.stopPropagation();
  if (!characterMenu.hidden) { closeCharacterMenu(); return; }
  try {
    const { characters, active } = await api("/characters");
    characterMenu.replaceChildren(...characters.map((card) => el("button", {
      role: "option", "aria-selected": String(card.id === active), class: card.id === active ? "active" : "",
      onclick: async () => {
        closeCharacterMenu();
        try { await ctx.applySnapshot(await api(`/characters/${encodeURIComponent(card.id)}/activate`, { method: "POST" })); }
        catch (error) { toast(error.message, "error"); }
      },
    }, avatar(card, 22), el("span", { text: card.name }), card.id === active ? icon("check", 14) : null)));
    characterMenu.hidden = false;
    $("character-btn").setAttribute("aria-expanded", "true");
  } catch (error) { toast(error.message, "error"); }
});
document.addEventListener("click", closeCharacterMenu);

// ---------- composer ----------

const input = $("composer-input");
let suggestions = [];
let suggestIndex = 0;

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

function computeSuggestions() {
  const value = input.value;
  if (!value.startsWith("/") || value.includes("\n")) return [];
  const parts = value.split(/\s+/);
  const typing = parts[0].slice(1).toLowerCase();
  if (parts.length === 1) {
    return ctx.commands.filter((c) => c.name.startsWith(typing)).map((c) => ({
      insert: `/${c.name} `, label: `/${c.name}`, description: c.description }));
  }
  const command = ctx.commands.find((c) => c.name === typing);
  if (!command || parts.length > 2) return [];
  return command.subcommands.filter((s) => s.name.startsWith(parts[1].toLowerCase())).map((s) => ({
    insert: `/${command.name} ${s.name} `, label: `/${command.name} ${s.name}`, description: s.description }));
}

function drawSuggestions() {
  suggestions = computeSuggestions();
  suggestIndex = Math.min(suggestIndex, Math.max(0, suggestions.length - 1));
  const box = $("suggest");
  box.hidden = suggestions.length === 0;
  box.replaceChildren(...suggestions.map((s, i) => el("button", {
    type: "button", role: "option", class: i === suggestIndex ? "active" : "", "aria-selected": String(i === suggestIndex),
    onmousedown: (e) => { e.preventDefault(); accept(i); },
  }, el("span", { class: "tool-name", text: s.label }), el("span", { class: "note", text: s.description }))));
}

function accept(index) {
  input.value = suggestions[index].insert;
  autosize();
  drawSuggestions();
  input.focus();
}

input.addEventListener("input", () => { autosize(); suggestIndex = 0; drawSuggestions(); });
input.addEventListener("keydown", (event) => {
  if (!$("suggest").hidden) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      suggestIndex = (suggestIndex + (event.key === "ArrowDown" ? 1 : -1) + suggestions.length) % suggestions.length;
      drawSuggestions();
      return;
    }
    const chosen = suggestions[suggestIndex];
    if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey && chosen.insert.trim() !== input.value.trim())) {
      event.preventDefault();
      accept(suggestIndex);
      return;
    }
    if (event.key === "Escape") { event.stopPropagation(); $("suggest").hidden = true; return; }
  }
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    send();
  }
});

async function send() {
  const text = input.value.trim();
  if (!text) return;
  if ((ctx.busy && !text.startsWith("/"))) { toast("Jarvis is busy. Wait, or press Stop.", "info"); return; }
  voice.unlock();
  voice.stop();
  input.value = "";
  autosize();
  drawSuggestions();
  try {
    const out = await api("/chat", { method: "POST", body: { text } });
    if (out.command_output !== undefined) {
      chat.commandOutput(text, out.command_output);
      await refreshStatus();
      if (/^\/(provider|settings|model|character|login)/i.test(text)) ctx.reloadSnapshot().catch(() => {});
    }
  } catch (error) {
    input.value = text;
    autosize();
    toast(error.message, "error");
    handleApiError(error);
  }
}

$("composer").addEventListener("submit", (event) => { event.preventDefault(); send(); });
$("stop").addEventListener("click", cancelTurn);

async function cancelTurn() {
  voice.stop();
  $("stop").disabled = true; // Stopping… until the turn actually ends
  $("stop").title = "Stopping…";
  try { await api("/cancel", { method: "POST" }); }
  catch (error) { $("stop").disabled = false; toast(error.message, "error"); }
}

async function newChat() {
  try {
    await applySnapshot(await api("/sessions", { method: "POST" }));
    navigate(`chat/${encodeURIComponent(ctx.sessionId)}`);
    input.focus();
  } catch (error) { toast(error.message, "error"); }
}
$("new-chat").addEventListener("click", () => { newChat(); ctx.closeRail(); });

// ---------- search & recents (rail) ----------

function openSearch(focus = true) {
  $("search-wrap").hidden = false;
  $("nav-search").setAttribute("aria-expanded", "true");
  if (focus) { $("session-search").focus(); $("session-search").select(); }
}
function closeSearch() {
  $("session-search").value = "";
  $("search-wrap").hidden = true;
  $("nav-search").setAttribute("aria-expanded", "false");
  sessions.refresh();
}
$("nav").addEventListener("click", (event) => { if (event.target.closest("a.nav-item")) ctx.closeRail(); });
$("nav-search").addEventListener("click", () => ($("search-wrap").hidden ? openSearch() : closeSearch()));
$("session-search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") { event.stopPropagation(); closeSearch(); $("nav-search").focus(); }
});
$("recents-toggle").addEventListener("click", () => {
  const open = $("recents-toggle").getAttribute("aria-expanded") !== "true";
  $("recents-toggle").setAttribute("aria-expanded", String(open));
  $("sessions").hidden = !open;
  store.set("jarvis.recents", open ? "open" : "closed");
});
if (store.get("jarvis.recents") === "closed") {
  $("recents-toggle").setAttribute("aria-expanded", "false");
  $("sessions").hidden = true;
}

// ---------- view router ----------

let viewName = "";
let routeReady = false;
const VIEW_TITLES = { agents: "Agents", discussions: "Discussions", routines: "Routines", tools: "Tools" };
const viewCtx = {
  api, bus, toast, navigate,
  setTitle(text) { if (viewName) { $("title").textContent = text; document.title = `${text} · Jarvis`; } },
};

function markNav(name) {
  for (const item of document.querySelectorAll("#nav [data-route]")) {
    const on = item.dataset.route === name;
    item.classList.toggle("active", on);
    if (on) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
  }
}

async function onRoute(route) {
  if (route.view) {
    viewName = route.name;
    document.body.classList.add("view-mode");
    $("view").hidden = false;
    $("title").textContent = VIEW_TITLES[route.name];
    document.title = `${VIEW_TITLES[route.name]} · Jarvis`;
    markNav(route.name);
    mountView(route.name, $("view"), route.params, viewCtx);
    return;
  }
  const wasView = viewName;
  viewName = "";
  unmountView();
  $("view").hidden = true;
  document.body.classList.remove("view-mode");
  markNav("");
  const sid = route.params.sid;
  if (sid && sid !== ctx.sessionId) {
    try { await applySnapshot(await api(`/sessions/${encodeURIComponent(sid)}/resume`, { method: "POST" })); }
    catch (error) { toast(error.message, "error"); await refreshStatus(); }
  } else if (wasView) { updateHeader(); sessions.refresh(); }
  if (ctx.sessionId) replaceRoute(`chat/${encodeURIComponent(ctx.sessionId)}`);
  routeReady = true;
  if (wasView) input.focus();
}

// ---------- push-to-talk ----------

const mic = $("mic");
let micActive = false;

async function micStart(event) {
  event.preventDefault();
  if (micActive) return;
  micActive = true;
  mic.classList.add("hot");
  try { await api("/voice/listen/start", { method: "POST" }); }
  catch (error) { micActive = false; mic.classList.remove("hot"); toast(error.message, "error"); }
}

async function micStop() {
  if (!micActive) return;
  micActive = false;
  mic.classList.remove("hot");
  try {
    const { transcript } = await api("/voice/listen/stop", { method: "POST" });
    if (transcript) { input.value = transcript; autosize(); send(); }
    else toast("Heard nothing.", "info");
  } catch (error) { toast(error.message, "error"); }
}
mic.addEventListener("pointerdown", micStart);
for (const name of ["pointerup", "pointercancel", "pointerleave"]) mic.addEventListener(name, micStop);
mic.addEventListener("keydown", (e) => { if (e.key === " " && !e.repeat) micStart(e); });
mic.addEventListener("keyup", (e) => { if (e.key === " ") micStop(); });

// ---------- keyboard shortcuts ----------

document.addEventListener("keydown", (event) => {
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.key.toLowerCase() === "k") { event.preventDefault(); openSearch(); }
  else if (mod && event.key.toLowerCase() === "n") { event.preventDefault(); newChat(); }
  else if (event.key === "Escape") {
    if (document.querySelector("dialog[open]")) return; // dialogs close themselves
    if (!characterMenu.hidden) closeCharacterMenu();
    else if (ctx.switcher.isOpen) ctx.switcher.close();
    else if (document.body.classList.contains("rail-open")) ctx.closeRail();
    else if (ctx.busy) cancelTurn();
  }
});

// ---------- boot ----------

async function boot() {
  try {
    ctx.commands = (await api("/commands")).commands;
    ctx.settings = (await api("/settings")).settings;
  } catch (error) { handleApiError(error); }
  loadProviderLabels();
  await refreshStatus();
  startRouter(onRoute);
  sessions.refresh();
  if (!viewName) input.focus();
}

boot();
