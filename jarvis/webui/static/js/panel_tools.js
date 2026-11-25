import { api } from "./api.js";
import { confirmBox, el, formatJson, toast } from "./dom.js";
import { renderMarkdown } from "./markdown.js";

export function renderTools(container) {
  const filter = el("input", { type: "search", placeholder: "Filter tools…", "aria-label": "Filter tools" });
  const query = el("input", { type: "search", placeholder: "TurboMCP search (e.g. read a file)…", "aria-label": "Search tools" });
  const results = el("div", { class: "fact-list" });
  const list = el("div", { class: "tool-list" });
  const skills = el("div", { class: "tool-list" });
  const viewer = el("div", { class: "skill-viewer", hidden: true });
  const codify = el("button", { text: "Codify skills" });
  const codifyOut = el("pre", { class: "tool-result", hidden: true });
  container.append(
    el("h4", { text: "Search tools" }), el("div", { class: "panel-tools" }, query), results,
    el("h4", { text: "All tools" }), el("div", { class: "panel-tools" }, filter), list,
    el("h4", { text: "Skills" }), skills, viewer,
    el("div", { class: "panel-tools" }, codify), codifyOut);
  let tools = [];

  function drawTools() {
    const needle = filter.value.trim().toLowerCase();
    list.replaceChildren(...tools
      .filter((t) => !needle || `${t.name} ${t.description}`.toLowerCase().includes(needle))
      .map((t) => el("div", { class: "tool-row" },
        el("span", { class: "tool-name", text: t.name }),
        t.core ? el("span", { class: "badge core", text: "core" }) : null,
        el("div", { class: "note", text: t.description || "" }))));
  }
  filter.addEventListener("input", drawTools);

  let timer;
  query.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = query.value.trim();
      if (!q) { results.replaceChildren(); return; }
      try {
        const found = await api(`/tools/search?q=${encodeURIComponent(q)}`);
        results.replaceChildren(...(found.tools || []).map((t) => el("div", { class: "tool-row" },
          el("span", { class: "tool-name", text: t.name }),
          el("span", { class: "badge", text: `score ${Number(t.score).toFixed(2)}` }),
          el("div", { class: "note", text: t.description || "" }))));
        if (!(found.tools || []).length) results.replaceChildren(el("p", { class: "note", text: "No matching tools." }));
      } catch (error) { toast(error.message, "error"); }
    }, 250);
  });

  codify.addEventListener("click", async () => {
    if (!(await confirmBox("Codify skills", "Generate tool code from skill files? New tools are smoke-tested before loading."))) return;
    codify.disabled = true;
    try {
      codifyOut.hidden = false;
      codifyOut.textContent = formatJson(await api("/skills/codify", { method: "POST" }));
      refresh();
    } catch (error) { toast(error.message, "error"); }
    codify.disabled = false;
  });

  async function refresh() {
    try {
      tools = (await api("/tools")).tools;
      drawTools();
      const { skills: items } = await api("/skills");
      skills.replaceChildren(...(items.length ? items.map((s) => el("button", {
        class: "skill-row", text: s.title || s.name,
        onclick: () => { viewer.hidden = false; viewer.replaceChildren(el("h4", { text: s.name }), renderMarkdown(s.content)); },
      })) : [el("p", { class: "note", text: "No skills yet." })]));
    } catch (error) { toast(error.message, "error"); }
  }
  refresh();
  return { refresh };
}
