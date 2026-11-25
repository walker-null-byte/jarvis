// Characters: gallery of cards plus a small editor (the few fields that matter).
import { api } from "./api.js";
import { avatar } from "./avatar.js";
import { confirmBox, el, iconBtn, toast } from "./dom.js";
import { icon } from "./icons.js";

export { avatar };

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("Could not read the file"));
    reader.readAsDataURL(file);
  });
}

const FIELDS = [
  ["name", "Name", "input", "What the character is called.", ""],
  ["description", "Description", "textarea", "Who they are: background, role, how they come across.", "A calm, precise assistant who…"],
  ["personality", "Personality", "textarea", "Traits and temperament. How they think and feel.", "Dry wit, patient, direct…"],
  ["greeting", "Greeting", "textarea", "The first thing they say in an empty chat.", "Good evening. How can I help?"],
  ["example_dialogue", "Example dialogue", "textarea", "A few sample exchanges that show the voice. Optional.", "User: hi\nCharacter: …"],
  ["instructions", "Instructions", "textarea", "Style rules: length, formatting, things to always or never do.", "Keep replies short. Never use emoji."],
  ["tags", "Tags", "input", "Comma-separated labels, for example: helper, formal.", "helper, formal"],
];

export function renderCharacters(container, ctx) {
  const view = el("div", { class: "chars" });
  container.append(view);
  let active = "";

  // ---------- gallery ----------

  async function gallery() {
    const picker = el("input", { type: "file", accept: ".json,.png,application/json,image/png", hidden: true, "aria-label": "Import a character card" });
    picker.addEventListener("change", async () => {
      const file = picker.files[0];
      picker.value = "";
      if (!file) return;
      try {
        const data_base64 = await readAsBase64(file);
        const card = await api("/characters/import", { method: "POST", body: { filename: file.name, data_base64 } });
        toast(`Imported ${card.name || file.name}`);
        gallery();
      } catch (error) { toast(error.message, "error"); }
    });
    const grid = el("div", { class: "card-grid" });
    view.replaceChildren(
      el("div", { class: "panel-tools" },
        el("button", { class: "primary", onclick: () => editor(null) }, icon("plus", 14), "New character"),
        el("button", { onclick: () => picker.click() }, icon("upload", 14), "Import…"), picker),
      el("p", { class: "note" }, icon("info", 13), " Imported cards are plain text added to the prompt. They cannot change tools or permissions, but still only import cards you trust."),
      grid);
    try {
      const data = await api("/characters");
      active = data.active;
      ctx.characters = data.characters;
      ctx.onCharacters?.(data.characters, active);
      grid.replaceChildren(...data.characters.map(card));
    } catch (error) { grid.replaceChildren(el("p", { class: "note", text: error.message })); }
  }

  function card(c) {
    const isActive = c.id === active;
    return el("article", { class: `char-card${isActive ? " active" : ""}` },
      avatar(c, 52),
      el("div", { class: "char-info" },
        el("div", { class: "char-title" },
          el("h4", { text: c.name }),
          isActive ? el("span", { class: "badge ok", text: "Active" }) : null,
          c.overridden ? el("span", { class: "badge warn", text: "edited" }) : null,
          c.builtin && !c.overridden ? el("span", { class: "badge", text: "built-in" }) : null),
        c.tags && c.tags.length ? el("div", { class: "tags" }, c.tags.slice(0, 5).map((t) => el("span", { class: "tag", text: t }))) : null,
        el("p", { text: c.description || "No description." }),
        el("div", { class: "card-actions" },
          el("button", { class: isActive ? "" : "primary small", disabled: isActive, text: isActive ? "In use" : "Activate", onclick: () => activate(c) }),
          el("button", { class: "small", text: "Edit", onclick: () => editor(c.id) }))));
  }

  async function activate(c) {
    try {
      await api(`/characters/${encodeURIComponent(c.id)}/activate`, { method: "POST" });
      await ctx.reloadSnapshot();
      gallery();
    } catch (error) { toast(error.message, "error"); }
  }

  // ---------- editor ----------

  async function editor(id) {
    let data = { name: "", description: "", personality: "", greeting: "", example_dialogue: "", instructions: "", tags: [], builtin: false, overridden: false };
    if (id) {
      try { data = { ...data, ...(await api(`/characters/${encodeURIComponent(id)}`)) }; }
      catch (error) { toast(error.message, "error"); return; }
    }
    const inputs = {};
    const fields = FIELDS.map(([key, label, kind, help, placeholder]) => {
      const value = key === "tags" ? (data.tags || []).join(", ") : (data[key] ?? (key === "greeting" ? data.first_mes : "") ?? "");
      const control = el(kind === "input" ? "input" : "textarea", { id: `char-${key}`, placeholder, spellcheck: "true", "aria-describedby": `char-${key}-help`, ...(kind === "input" ? { type: "text" } : { rows: key === "description" || key === "greeting" ? 3 : 4 }) });
      control.value = value || "";
      inputs[key] = control;
      return el("div", { class: "form-field" },
        el("label", { for: `char-${key}`, text: label }), control, el("p", { id: `char-${key}-help`, class: "help", text: help }));
    });
    const collect = () => {
      const out = {};
      for (const [key] of FIELDS) out[key] = key === "tags" ? inputs.tags.value.split(",").map((t) => t.trim()).filter(Boolean) : inputs[key].value;
      return out;
    };
    const save = async (activateAfter = false) => {
      const fieldsOut = collect();
      if (!fieldsOut.name.trim()) { toast("A character needs a name.", "error"); inputs.name.focus(); return; }
      try {
        const saved = id
          ? await api(`/characters/${encodeURIComponent(id)}`, { method: "PUT", body: { fields: fieldsOut } })
          : await api("/characters", { method: "POST", body: { fields: fieldsOut } });
        toast("Saved");
        const savedId = saved.id || id;
        if (activateAfter && savedId) {
          await api(`/characters/${encodeURIComponent(savedId)}/activate`, { method: "POST" });
          await ctx.reloadSnapshot();
        } else if (savedId === active) await ctx.reloadSnapshot();
        gallery();
      } catch (error) { toast(error.message, "error"); }
    };
    const reset = async () => {
      if (!(await confirmBox("Reset to default", `Discard your changes to “${data.name}”?`))) return;
      try { await api(`/characters/${encodeURIComponent(id)}/reset`, { method: "POST" }); toast("Reset"); if (id === active) await ctx.reloadSnapshot(); gallery(); }
      catch (error) { toast(error.message, "error"); }
    };
    const remove = async () => {
      if (!(await confirmBox("Delete character", `Delete “${data.name}”?`))) return;
      try { await api(`/characters/${encodeURIComponent(id)}`, { method: "DELETE" }); gallery(); }
      catch (error) { toast(error.message, "error"); }
    };
    const exportLink = id ? el("a", { class: "button small", href: `/api/characters/${encodeURIComponent(id)}/export`, download: `${id}.json` }, icon("download", 14), "Export JSON") : null;
    view.replaceChildren(
      el("div", { class: "editor-head" },
        iconBtn("left", "Back to characters", gallery),
        el("h3", { text: id ? `Edit ${data.name}` : "New character" }),
        data.overridden ? el("span", { class: "badge warn", text: "edited" }) : null),
      el("form", { class: "char-form", onsubmit: (e) => { e.preventDefault(); save(); } },
        fields,
        el("div", { class: "form-actions" },
          el("button", { type: "submit", class: "primary", text: "Save" }),
          el("button", { type: "button", text: "Save & activate", onclick: () => save(true) }),
          id && data.builtin && data.overridden ? el("button", { type: "button", text: "Reset to default", onclick: reset }) : null,
          id && !data.builtin ? el("button", { type: "button", class: "danger", onclick: remove }, icon("trash", 14), "Delete") : null,
          exportLink)));
    inputs.name.focus();
  }

  gallery();
  return { refresh: gallery };
}
