// Safe markdown renderer. It builds DOM nodes directly (no innerHTML anywhere),
// so untrusted model output can never inject markup.
import { el } from "./dom.js";

const INLINE = new RegExp([
  "`([^`\\n]+)`",                                   // 1 inline code
  "\\*\\*([^*\\n]+?)\\*\\*",                        // 2 bold
  "(?<![\\w*])\\*([^*\\s][^*\\n]*?)\\*(?![\\w*])",  // 3 italic
  "\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)", // 4,5 link
  "(https?:\\/\\/[^\\s<>()]+[^\\s<>().,;:!?'\"])",   // 6 bare url
].join("|"), "g");

function link(href, label) {
  return el("a", { href, target: "_blank", rel: "noopener noreferrer", text: label });
}

export function renderInline(text, parent) {
  let last = 0;
  const pattern = new RegExp(INLINE.source, "g"); // own lastIndex: renderInline recurses
  for (let m = pattern.exec(text); m; m = pattern.exec(text)) {
    if (m.index > last) parent.append(text.slice(last, m.index));
    if (m[1] !== undefined) parent.append(el("code", { text: m[1] }));
    else if (m[2] !== undefined) parent.append(renderInline(m[2], el("strong")));
    else if (m[3] !== undefined) parent.append(renderInline(m[3], el("em")));
    else if (m[4] !== undefined) parent.append(link(m[5], m[4]));
    else parent.append(link(m[6], m[6]));
    last = pattern.lastIndex;
  }
  if (last < text.length) parent.append(text.slice(last));
  return parent;
}

function codeBlock(lang, code) {
  const copy = el("button", { class: "copy-btn", text: "Copy", "aria-label": "Copy code" });
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(code); copy.textContent = "Copied"; }
    catch { copy.textContent = "Failed"; }
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  });
  return el("div", { class: "code-block" },
    el("div", { class: "code-head" }, el("span", { text: lang || "text" }), copy),
    el("pre", {}, el("code", { text: code })));
}

const FENCE = /^\s*```\s*([\w+-]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function splitRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function table(lines) {
  const head = splitRow(lines[0]);
  const rows = lines.slice(2).map(splitRow);
  const cell = (tag, text) => el(tag, {}, renderInline(text, el("span")));
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, head.map((c) => cell("th", c)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, head.map((_, i) => cell("td", r[i] || "")))))));
}

function startsBlock(line, next) {
  return FENCE.test(line) || HEADING.test(line) || LIST.test(line) || RULE.test(line)
    || /^\s*>/.test(line) || (line.includes("|") && next !== undefined && TABLE_SEP.test(next));
}

export function renderMarkdown(source) {
  const root = el("div", { class: "md" });
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (!line.trim()) { i++; continue; }
    if ((m = FENCE.exec(line))) {
      const body = [];
      for (i++; i < lines.length && !FENCE.test(lines[i]); i++) body.push(lines[i]);
      i++; // closing fence (may be absent while streaming)
      root.append(codeBlock(m[1], body.join("\n")));
    } else if ((m = HEADING.exec(line))) {
      const level = Math.min(m[1].length + 1, 6);
      root.append(renderInline(m[2], el(`h${level}`)));
      i++;
    } else if (RULE.test(line)) {
      root.append(el("hr")); i++;
    } else if (/^\s*>/.test(line)) {
      const quote = [];
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.push(lines[i].replace(/^\s*>\s?/, ""));
      root.append(el("blockquote", {}, renderMarkdown(quote.join("\n"))));
    } else if (line.includes("|") && TABLE_SEP.test(lines[i + 1] || "")) {
      const rows = [];
      for (; i < lines.length && lines[i].includes("|"); i++) rows.push(lines[i]);
      root.append(table(rows));
    } else if ((m = LIST.exec(line))) {
      const ordered = /\d/.test(m[2]);
      const list = el(ordered ? "ol" : "ul");
      for (; i < lines.length && (m = LIST.exec(lines[i])); i++) {
        const depth = Math.min(Math.floor(m[1].replace(/\t/g, "  ").length / 2), 4);
        const item = renderInline(m[3], el("li"));
        if (depth) item.style.marginLeft = `${depth * 1.2}rem`; // CSSOM is allowed by the CSP, attributes are not
        list.append(item);
      }
      root.append(list);
    } else {
      const para = [];
      for (; i < lines.length && lines[i].trim() && (para.length === 0 || !startsBlock(lines[i], lines[i + 1])); i++) {
        para.push(lines[i]);
      }
      const p = el("p");
      para.forEach((text, index) => { if (index) p.append(el("br")); renderInline(text, p); });
      root.append(p);
    }
  }
  return root;
}
