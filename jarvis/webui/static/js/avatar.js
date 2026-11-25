import { el } from "./dom.js";

// Character avatar: the card image, or the name's initial on a tinted disc.
export function avatar(card, size = 24) {
  const px = `${size}px`;
  let node;
  if (card && card.has_avatar) {
    node = el("img", { class: "avatar", alt: "", src: `/api/characters/${encodeURIComponent(card.id)}/avatar` });
  } else {
    node = el("div", { class: "avatar placeholder", "aria-hidden": "true", text: ((card && card.name) || "?").trim().slice(0, 1).toUpperCase() });
  }
  node.style.width = px;
  node.style.height = px;
  node.style.fontSize = `${Math.round(size * 0.46)}px`;
  return node;
}
