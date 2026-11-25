// Small time helpers for the Routines view: relative ("in 2 h"), absolute ("Wed 09:00") and durations.

const pad = (n) => String(n).padStart(2, "0");
const clock = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "Wed 09:00" within a week, otherwise "1 Dec 09:00" (with the year when it is not this one).
export function when(iso, now = new Date()) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.floor((d - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
  if (days === 0) return `Today ${clock(d)}`;
  if (days === 1) return `Tomorrow ${clock(d)}`;
  if (days > 1 && days < 7) return `${DAYS[d.getDay()]} ${clock(d)}`;
  const year = d.getFullYear() === now.getFullYear() ? "" : ` ${d.getFullYear()}`;
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${year} ${clock(d)}`;
}

// "in 2 h 5 min", "in 12 min", "due now"; far-away times fall back to when().
export function relative(iso, now = new Date()) {
  const seconds = (new Date(iso) - now) / 1000;
  if (Number.isNaN(seconds)) return "";
  if (seconds < 45) return seconds < -300 ? "overdue" : "due now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 3) return `in ${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ""}`;
  if (hours < 24) return `in ${hours} h`;
  return when(iso, now);
}

export function ago(iso, now = new Date()) {
  const seconds = (now - new Date(iso)) / 1000;
  if (Number.isNaN(seconds)) return "";
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export function duration(seconds) {
  if (!seconds) return "under 1 s";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

// Value for <input type="datetime-local"> from a Date.
export function localInput(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${clock(d)}`;
}

// Markdown to one plain line: for list rows, toasts and desktop notifications.
export function plainText(text) {
  return String(text || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+/gm, "")
    .replace(/[*_~`]+/g, "")
    .replace(/\|[-:| ]+\|/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}
