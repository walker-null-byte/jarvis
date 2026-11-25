// Small pieces shared by the Discussions list, create form and room.
import { el } from "./dom.js";
import { icon } from "./icons.js";

const SVG_NS = "http://www.w3.org/2000/svg";
// Control icons the shared icon set lacks (same Lucide-style strokes).
const EXTRA = {
  play: ["M6 4l14 8-14 8z"],
  pause: ["M8 5v14", "M16 5v14"],
  skip: ["M5 4l10 8-10 8z", "M19 5v14"],
  flag: ["M4 22V4", "M4 4h13l-2 4 2 4H4"],
  grip: ["M9 6h.01", "M15 6h.01", "M9 12h.01", "M15 12h.01", "M9 18h.01", "M15 18h.01"],
  up: ["m18 15-6-6-6 6"],
};

export function ico(name, size = 16) {
  if (!EXTRA[name]) return icon(name, size);
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor", "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false", class: "icon",
  })) svg.setAttribute(key, String(value));
  for (const d of EXTRA[name]) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

export const STATUS_LABEL = {
  draft: "Draft", running: "Running", paused: "Paused", waiting: "Waiting for you", finished: "Finished",
};

export function statusChip(status) {
  return el("span", { class: `d-chip s-${status}`, text: STATUS_LABEL[status] || status });
}

// Colored initials disc for a participant ({name, color}).
export function partAvatar(person, size = 28) {
  const initials = (person.name || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  const node = el("div", { class: "d-avatar", "aria-hidden": "true", text: initials, title: person.name });
  node.style.setProperty("--c", person.color || "#2dd4e8");
  node.style.width = node.style.height = `${size}px`;
  node.style.fontSize = `${Math.round(size * 0.4)}px`;
  return node;
}

export function avatarStack(people, max = 5) {
  const shown = people.slice(0, max);
  const stack = el("div", { class: "d-stack", role: "img", "aria-label": `Participants: ${people.map((p) => p.name).join(", ")}` },
    shown.map((p) => partAvatar(p, 26)));
  if (people.length > max) stack.append(el("span", { class: "d-more", text: `+${people.length - max}` }));
  return stack;
}

export const MODE_LABEL = { round_robin: "Round robin", parallel: "Parallel", moderated: "Moderated", debate: "Debate" };

// "gemini · gemini-2.5-flash" (provider id; the model may be empty for "current model")
export function modelBadge(provider, model) {
  const text = [provider, model].filter(Boolean).join(" · ") || "current model";
  return el("span", { class: "badge d-model", text, title: text });
}

export function timeAgo(iso) {
  const then = Date.parse(iso || "");
  if (!then) return "";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(then).toLocaleDateString();
}

// A native-looking labelled field: <label><span>text</span>control</label> with optional help and error lines.
export function field(label, control, { help = "", required = false, id = "" } = {}) {
  const error = el("span", { class: "d-error", role: "alert", hidden: true });
  const node = el("div", { class: "field d-field" },
    el("label", { class: "d-label", for: id || undefined }, el("span", { text: label }), required ? el("span", { class: "d-req", text: "required" }) : null),
    control, help ? el("span", { class: "note", text: help }) : null, error);
  node.setError = (text) => { error.textContent = text || ""; error.hidden = !text; };
  return node;
}
