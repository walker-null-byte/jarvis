// Inline SVG icon set (Lucide-style strokes). Built with createElementNS, never innerHTML.
// Spec entries: a string = <path d>, ["r",x,y,w,h,rx] = <rect>, ["c",cx,cy,r] = <circle>.
const SVG_NS = "http://www.w3.org/2000/svg";
const ARROW_BASE = "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4";

const ICONS = {
  copy: [["r", 9, 9, 13, 13, 2], "M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"],
  redo: ["M21 12a9 9 0 1 1-3-6.7L21 8", "M21 3v5h-5"],
  speaker: ["M11 5 6 9H2v6h4l5 4V5z", "M15.5 8.5a5 5 0 0 1 0 7", "M19 5a9 9 0 0 1 0 14"],
  "speaker-off": ["M11 5 6 9H2v6h4l5 4V5z", "M22 9l-6 6", "M16 9l6 6"],
  stop: [["r", 6, 6, 12, 12, 2]],
  pencil: ["M17 3a2.8 2.8 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5z"],
  check: ["M20 6 9 17l-5-5"],
  send: ["M12 19V5", "M5 12l7-7 7 7"],
  mic: ["M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z", "M19 10v2a7 7 0 0 1-14 0v-2", "M12 19v3"],
  menu: ["M4 6h16", "M4 12h16", "M4 18h16"],
  panel: [["r", 3, 3, 18, 18, 2], "M15 3v18"],
  plus: ["M12 5v14", "M5 12h14"],
  x: ["M18 6 6 18", "M6 6l12 12"],
  down: ["m6 9 6 6 6-6"],
  right: ["m9 18 6-6-6-6"],
  left: ["m15 18-6-6 6-6"],
  download: [ARROW_BASE, "M7 10l5 5 5-5", "M12 15V3"],
  upload: [ARROW_BASE, "M17 8l-5-5-5 5", "M12 3v12"],
  trash: ["M3 6h18", "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2", "M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"],
  sparkle: ["M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z", "M19 17v4", "M17 19h4"],
  terminal: ["M4 17l6-6-6-6", "M12 19h8"],
  sliders: ["M4 21v-7", "M4 10V3", "M12 21v-9", "M12 8V3", "M20 21v-5", "M20 12V3", "M1 14h6", "M9 8h6", "M17 16h6"],
  search: [["c", 11, 11, 8], "m21 21-4.3-4.3"],
  "arrow-down": ["M12 5v14", "M19 12l-7 7-7-7"],
  more: [["c", 5, 12, 1], ["c", 12, 12, 1], ["c", 19, 12, 1]],
  alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4", "M12 17h.01"],
  key: ["M21 2l-2 2", "M15.5 7.5 19 4l3 3-3.5 3.5", ["c", 7.5, 15.5, 5.5]],
  cloud: ["M17.5 19a4.5 4.5 0 1 0-1.4-8.8A6 6 0 1 0 6 17.5"],
  server: [["r", 2, 3, 20, 8, 2], ["r", 2, 13, 20, 8, 2], "M6 7h.01", "M6 17h.01"],
  user: ["M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2", ["c", 12, 7, 4]],
  users: ["M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2", ["c", 9, 7, 4], "M23 21v-2a4 4 0 0 0-3-3.9", "M16 3.1a4 4 0 0 1 0 7.8"],
  activity: ["M22 12h-4l-3 9L9 3l-3 9H2"],
  brain: ["M12 5a3 3 0 1 0-5.9.8A4 4 0 0 0 4 12a4 4 0 0 0 2.2 3.6A3 3 0 0 0 12 17V5z", "M12 5a3 3 0 1 1 5.9.8A4 4 0 0 1 20 12a4 4 0 0 1-2.2 3.6A3 3 0 0 1 12 17"],
  book: ["M4 19.5A2.5 2.5 0 0 1 6.5 17H20", "M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"],
  clock: [["c", 12, 12, 10], "M12 6v6l4 2"],
  wave: ["M2 12h2", "M6 8v8", "M10 4v16", "M14 8v8", "M18 6v12", "M22 12h-2"],
  info: [["c", 12, 12, 10], "M12 16v-4", "M12 8h.01"],
  agents: [["r", 4, 9, 16, 11, 3], "M12 9V5", ["c", 12, 4, 1], ["c", 9, 14, 1], ["c", 15, 14, 1], "M9.5 17.5h5"],
  discussions: ["M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z", "M18 9h2a2 2 0 0 1 2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1"],
  routines: ["M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8", "M3 3v5h5", "M12 7v5l4 2"],
  wrench: ["M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"],
  play: ["M6 4l14 8-14 8z"],
  bell: ["M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9", "M13.7 21a2 2 0 0 1-3.4 0"],
  external: ["M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6", "M15 3h6v6", "M10 14 21 3"],
};

export function icon(name, size = 16) {
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false", class: "icon",
  })) svg.setAttribute(key, String(value));
  for (const part of ICONS[name] || []) {
    let node;
    if (typeof part === "string") {
      node = document.createElementNS(SVG_NS, "path");
      node.setAttribute("d", part);
    } else if (part[0] === "r") {
      node = document.createElementNS(SVG_NS, "rect");
      ["x", "y", "width", "height", "rx"].forEach((k, i) => node.setAttribute(k, String(part[i + 1])));
    } else {
      node = document.createElementNS(SVG_NS, "circle");
      ["cx", "cy", "r"].forEach((k, i) => node.setAttribute(k, String(part[i + 1])));
    }
    svg.append(node);
  }
  return svg;
}

// Swap the icon inside a button (keeps any text label node).
export function setIcon(button, name, size = 16) {
  button.querySelector("svg.icon")?.replaceWith(icon(name, size));
}
