// Safe markdown renderer. marked (vendored, see vendor/marked.esm.js) only tokenizes;
// every node is built here with createElement/textContent, so untrusted model output
// can never inject markup: raw HTML in the text is shown as plain text, links are
// limited to http(s)/mailto, and remote images are never fetched (they become links).
import { Lexer } from "../vendor/marked.esm.js";
import { el } from "./dom.js";

const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

// Returns the normalized URL when it is safe to link to, otherwise "".
function safeHref(href) {
  try {
    const url = new URL(String(href || "").trim());
    return SAFE_PROTOCOLS.has(url.protocol) ? url.href : "";
  } catch { return ""; }
}

function link(href, title) {
  const safe = safeHref(href);
  if (!safe) return el("span", { class: "dead-link", title: "Blocked unsafe link" });
  return el("a", { href: safe, target: "_blank", rel: "noopener noreferrer nofollow", title });
}

function checkbox(checked) {
  return el("input", { type: "checkbox", disabled: true, checked, "aria-label": checked ? "done" : "not done" });
}

function inline(tokens, parent) {
  for (const t of tokens || []) {
    switch (t.type) {
      case "text": case "escape":
        if (t.tokens && t.tokens.length) inline(t.tokens, parent);
        else parent.append(t.text);
        break;
      case "strong": parent.append(inline(t.tokens, el("strong"))); break;
      case "em": parent.append(inline(t.tokens, el("em"))); break;
      case "del": parent.append(inline(t.tokens, el("del"))); break;
      case "checkbox": parent.append(checkbox(t.checked)); break;
      case "codespan": parent.append(el("code", { text: t.text })); break;
      case "br": parent.append(el("br")); break;
      case "link": parent.append(inline(t.tokens, link(t.href, t.title))); break;
      case "image": { const a = link(t.href, t.title); a.append(t.text || t.href); parent.append(a); break; }
      default: parent.append(t.raw ?? t.text ?? ""); // inline html and anything unknown: as text
    }
  }
  return parent;
}

function codeBlock(lang, code) {
  const copy = el("button", { class: "copy-btn", type: "button", text: "Copy", "aria-label": "Copy code" });
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(code); copy.textContent = "Copied"; }
    catch { copy.textContent = "Failed"; }
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  });
  return el("div", { class: "code-block" },
    el("div", { class: "code-head" }, el("span", { text: lang || "text" }), copy),
    el("pre", {}, el("code", { text: code })));
}

function tableNode(t) {
  const cell = (tag, c, align) => {
    const node = inline(c.tokens, el(tag));
    if (align) node.style.textAlign = align; // CSSOM is allowed by the CSP, style attributes are not
    return node;
  };
  const aligns = t.align || [];
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, t.header.map((c, i) => cell("th", c, aligns[i])))),
    el("tbody", {}, t.rows.map((row) => el("tr", {}, row.map((c, i) => cell("td", c, aligns[i])))))));
}

function listNode(t) {
  const list = el(t.ordered ? "ol" : "ul");
  if (t.ordered && typeof t.start === "number" && t.start !== 1) list.setAttribute("start", String(t.start));
  let tasks = false;
  for (const item of t.items) {
    const li = el("li");
    if (item.task) { tasks = true; li.className = "task"; }
    blocks(item.tokens, li, item.task || !item.loose);
    list.append(li);
  }
  if (tasks) list.className = "task-list";
  return list;
}

function blocks(tokens, parent, tight = false) {
  for (const t of tokens || []) {
    switch (t.type) {
      case "space": case "def": break;
      case "checkbox": parent.append(checkbox(t.checked)); break;
      case "heading": parent.append(inline(t.tokens, el(`h${Math.min(t.depth + 1, 6)}`))); break;
      case "paragraph": parent.append(tight ? inline(t.tokens, el("span", { class: "tight" })) : inline(t.tokens, el("p"))); break;
      case "text": // loose text inside a tight list item, or a stray block-level text
        if (t.tokens && t.tokens.length) inline(t.tokens, parent); else parent.append(t.text);
        break;
      case "code": parent.append(codeBlock((t.lang || "").trim().split(/\s+/)[0], t.text)); break;
      case "blockquote": parent.append(blocks(t.tokens, el("blockquote"))); break;
      case "list": parent.append(listNode(t)); break;
      case "table": parent.append(tableNode(t)); break;
      case "hr": parent.append(el("hr")); break;
      case "html": parent.append(el("p", { text: (t.raw || t.text || "").trim() })); break; // shown, never parsed
      default: if (t.raw) parent.append(el("p", { text: t.raw.trim() }));
    }
  }
  return parent;
}

export function renderMarkdown(source) {
  const root = el("div", { class: "md" });
  const text = String(source ?? "").replace(/\r\n?/g, "\n");
  let tokens;
  try { tokens = new Lexer({ gfm: true, breaks: true }).lex(text); }
  catch { return el("div", { class: "md" }, el("p", { text })); }
  return blocks(tokens, root);
}
