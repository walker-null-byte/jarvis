// Icons for agents: a few extra glyphs on top of icons.js, plus a plugin's own icon.svg
// (shown through <img>, so an SVG can never run script in the page).
import { el } from "./dom.js";
import { icon } from "./icons.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const EXTRA = {
  target: [["c", 12, 12, 10], ["c", 12, 12, 6], ["c", 12, 12, 2]],
  mail: [["r", 2, 4, 20, 16, 2], "m22 7-10 6L2 7"],
  chart: ["M3 3v18h18", "M7 15v3", "M12 10v8", "M17 6v12"],
  globe: [["c", 12, 12, 10], "M2 12h20", "M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"],
  phone: ["M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z"],
  zap: ["M13 2 3 14h9l-1 8 10-12h-9l1-8z"],
  briefcase: [["r", 2, 7, 20, 14, 2], "M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"],
  calendar: [["r", 3, 4, 18, 18, 2], "M16 2v4", "M8 2v4", "M3 10h18"],
  pen: ["M12 20h9", "M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"],
  lock: [["r", 3, 11, 18, 11, 2], "M7 11V7a5 5 0 0 1 10 0v4"],
  code: ["m16 18 6-6-6-6", "m8 6-6 6 6 6"],
  filter: ["M22 3H2l8 9.5V19l4 2v-8.5z"],
  play: ["m6 3 14 9-14 9z"],
  shield: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"],
};

function build(spec, size) {
  const svg = icon("__none__", size); // an empty <svg class="icon"> with the shared attributes
  for (const part of spec) {
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

// An icon by name: our extras first, then the app's icon set (unknown names fall back to "agents").
export function aicon(name, size = 16) {
  if (EXTRA[name]) return build(EXTRA[name], size);
  const svg = icon(name, size);
  return svg.childNodes.length ? svg : icon("agents", size);
}

// The tile that represents an agent: its icon.svg if it ships one, else a named glyph.
export function agentTile(agent, size = 40) {
  const tile = el("span", { class: "agent-tile", "aria-hidden": "true" });
  tile.style.width = tile.style.height = `${size}px`;
  if (agent.has_icon_file) {
    tile.append(el("img", { src: `/api/agents/${encodeURIComponent(agent.id)}/icon.svg`, alt: "", width: Math.round(size * 0.6), height: Math.round(size * 0.6) }));
  } else {
    tile.append(aicon(agent.icon || "agents", Math.round(size * 0.5)));
  }
  return tile;
}
