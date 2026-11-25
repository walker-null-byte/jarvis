// Routine notifications that work on every page: sidebar badge, toast and (if allowed) a browser notification.
import { api } from "./api.js";
import { el, toast } from "./dom.js";
import { navigate } from "./router.js";
import { plainText } from "./routine_time.js";

const LABELS = { ok: "finished", error: "failed", needs_approval: "needs your approval", cancelled: "was cancelled" };
let unread = 0;

function paintBadge() {
  const badge = document.getElementById("routines-badge");
  if (!badge) return;
  badge.hidden = unread <= 0;
  badge.textContent = unread > 99 ? "99+" : String(unread);
  badge.setAttribute("aria-label", `${unread} unread routine results`);
}

const onRoutinesPage = () => window.location.hash.startsWith("#/routines");

/** The user opened the Routines view: everything counts as read. */
export async function markRoutinesSeen() {
  unread = 0;
  paintBadge();
  try { await api("/routines/seen", { method: "POST" }); } catch { /* the badge stays cleared locally */ }
}

export const notificationsSupported = () => typeof Notification !== "undefined";

export async function enableNotifications() {
  if (!notificationsSupported()) return "unsupported";
  return Notification.requestPermission();
}

function showToast(data) {
  const run = data.run || {};
  const text = `Routine “${data.name}” ${LABELS[run.status] || "finished"}${run.summary ? `: ${plainText(run.summary).slice(0, 120)}` : ""}`;
  const kind = run.status === "error" ? "error" : "info";
  if (onRoutinesPage()) { toast(text, kind); return; }
  const node = el("div", { class: `toast ${kind} routine-toast`, role: "status" },
    el("span", { text }),
    el("button", { class: "small", text: "View", onclick: () => { node.remove(); navigate("routines"); } }));
  document.getElementById("toasts").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 9000 : 6000);
}

function systemNotification(data) {
  if (!notificationsSupported() || Notification.permission !== "granted") return;
  if (document.hasFocus() && !document.hidden) return; // the toast is enough when you are looking
  const run = data.run || {};
  try {
    const note = new Notification(`Jarvis: ${data.name} ${LABELS[run.status] || "finished"}`, { body: plainText(run.summary) || run.error || "", tag: data.routine_id });
    note.onclick = () => { window.focus(); navigate("routines"); note.close(); };
  } catch { /* some browsers only allow notifications from a service worker */ }
}

export async function initRoutineNotifier(bus) {
  bus.on((msg) => {
    if (msg.kind !== "routine_run_finished") return;
    const data = msg.data || {};
    if (data.notify === false) return;
    if (onRoutinesPage()) markRoutinesSeen(); else { unread += 1; paintBadge(); }
    showToast(data);
    systemNotification(data);
  });
  window.addEventListener("hashchange", () => { if (onRoutinesPage()) markRoutinesSeen(); });
  try { unread = (await api("/routines/unread")).count; } catch { unread = 0; }
  if (onRoutinesPage()) markRoutinesSeen(); else paintBadge();
}
