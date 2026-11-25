import { api } from "./api.js";
import { confirmBox, el, formatJson, toast } from "./dom.js";

const FILE_HINTS = {
  "user.md": "Add how you like Jarvis to behave under “## Preferences” (tone, how to address you, how long replies should be). It applies to every character.",
};

export function renderMind(container, ctx) {
  const select = el("select", { "aria-label": "Mind file" });
  const editor = el("textarea", { class: "editor", spellcheck: "false", "aria-label": "File contents" });
  const save = el("button", { class: "primary", text: "Save" });
  const hint = el("span", { class: "note" });
  const dream = el("button", { text: "Run dream cycle" });
  const dreamOut = el("pre", { class: "tool-result", hidden: true });
  const fileHint = el("p", { class: "note hint-box" });
  const pointer = el("p", { class: "note hint-box" }, "Personality now lives in ",
    el("button", { class: "link", type: "button", text: "Characters", onclick: () => ctx.openTab?.("characters") }),
    ". Edit a character there; what you write here applies to all of them.");
  container.append(
    pointer,
    el("div", { class: "panel-tools" }, select, save, hint),
    fileHint,
    editor,
    el("p", { class: "note", text: "The dream cycle consolidates today's episodes into facts, skills and memory. It can take a while." }),
    el("div", { class: "panel-tools" }, dream), dreamOut);

  let current = null;
  async function load(name) {
    try {
      const file = await api(`/mind/${encodeURIComponent(name)}`);
      current = file;
      editor.value = file.content;
      editor.readOnly = !file.editable;
      save.disabled = !file.editable;
      hint.textContent = file.editable ? "" : "Read-only (rebuilt by the memory system)";
      fileHint.textContent = FILE_HINTS[name] || "";
      fileHint.hidden = !FILE_HINTS[name];
    } catch (error) { toast(error.message, "error"); }
  }
  select.addEventListener("change", () => load(select.value));
  save.addEventListener("click", async () => {
    try {
      await api(`/mind/${encodeURIComponent(current.name)}`, { method: "PUT", body: { content: editor.value } });
      toast(`Saved ${current.name}`);
    } catch (error) { toast(error.message, "error"); }
  });
  dream.addEventListener("click", async () => {
    if (!(await confirmBox("Dream cycle", "Run memory consolidation now? It calls the language model."))) return;
    dream.disabled = true;
    dream.textContent = "Dreaming…";
    try {
      const result = await api("/mind/consolidate", { method: "POST" });
      dreamOut.hidden = false;
      dreamOut.textContent = formatJson(result);
    } catch (error) { toast(error.message, "error"); }
    dream.disabled = false;
    dream.textContent = "Run dream cycle";
  });

  async function refresh() {
    const { files } = await api("/mind");
    select.replaceChildren(...files.map((f) => el("option", { value: f.name, text: f.editable ? f.name : `${f.name} (read-only)` })));
    if (files.length) load(select.value);
  }
  refresh().catch((error) => toast(error.message, "error"));
  return { refresh };
}

export function renderMemory(container) {
  const search = el("input", { type: "search", placeholder: "Search or recall…", "aria-label": "Search memories" });
  const recallButton = el("button", { text: "Recall" });
  const list = el("div", { class: "fact-list" });
  const recallOut = el("div", { class: "fact-list" });
  container.append(el("div", { class: "panel-tools" }, search, recallButton), recallOut, list);
  let facts = [];

  function draw() {
    const needle = search.value.trim().toLowerCase();
    const shown = facts.filter((f) => !needle || `${f.name} ${f.memory} ${f.category}`.toLowerCase().includes(needle));
    list.replaceChildren(...(shown.length ? shown.map(factCard) : [el("p", { class: "note", text: "No memories." })]));
  }

  function factCard(fact) {
    const act = async (fn) => { try { await fn(); await refresh(); } catch (error) { toast(error.message, "error"); } };
    return el("article", { class: `fact ${fact.status}` },
      el("div", { class: "fact-head" }, el("strong", { text: fact.name }),
        el("span", { class: `badge status-${fact.status}`, text: fact.status }),
        el("span", { class: "badge", text: fact.category }),
        el("span", { class: "note", text: `conf ${Number(fact.confidence).toFixed(2)}` })),
      el("p", { text: fact.memory }),
      el("div", { class: "card-actions" },
        el("button", { text: "Approve", onclick: () => act(() => api(`/memory/${encodeURIComponent(fact.name)}/review`, { method: "POST", body: { decision: "approve" } })) }),
        el("button", { text: "Reject", onclick: () => act(() => api(`/memory/${encodeURIComponent(fact.name)}/review`, { method: "POST", body: { decision: "reject" } })) }),
        el("button", { class: "danger", text: "Forget", onclick: async () => {
          if (await confirmBox("Forget memory", `Delete “${fact.name}” permanently?`)) {
            act(() => api(`/memory/${encodeURIComponent(fact.name)}`, { method: "DELETE" }));
          }
        } })));
  }

  recallButton.addEventListener("click", async () => {
    const q = search.value.trim();
    if (!q) { recallOut.replaceChildren(); return; }
    try {
      const { results, note } = await api(`/memory/recall?q=${encodeURIComponent(q)}`);
      recallOut.replaceChildren(el("h4", { text: "Recalled" }),
        ...(results.length ? results.map((r) => el("article", { class: "fact" },
          el("strong", { text: r.name || "" }), el("p", { text: r.memory || r.fact || formatJson(r) }))) : [el("p", { class: "note", text: note || "Nothing recalled." })]));
    } catch (error) { toast(error.message, "error"); }
  });
  search.addEventListener("input", draw);

  async function refresh() {
    try { facts = (await api("/memory")).memories; draw(); }
    catch (error) { toast(error.message, "error"); }
  }
  refresh();
  return { refresh };
}
