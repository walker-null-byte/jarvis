// Voice tab: engine choice, model catalog (speech voices, recognition, tool search), previews.
import { api } from "./api.js";
import { el, toast } from "./dom.js";
import { icon } from "./icons.js";
import { downloadItem, onDownload, progressText, downloads } from "./voice.js";

const GROUPS = [
  ["tts", "Speech voices", "Voices Jarvis speaks with."],
  ["stt", "Speech recognition", "Turns your microphone into text (push-to-talk)."],
  ["embeddings", "Smarter tool search", "Helps Jarvis find the right tool by meaning. Optional."],
];
const KOKORO_VOICES = ["af_sky", "af_bella", "af_nicole", "af_sarah", "am_adam", "am_michael", "bf_emma", "bm_george"];
const SAMPLE = "Hello, I'm Jarvis. This is how I sound.";

export function renderVoice(container, ctx) {
  const root = el("div", { class: "voice-tab" });
  container.append(root);
  let info = null;
  const offs = [];

  async function load() {
    try {
      info = await api("/voice");
      ctx.voiceInfo = info;
      for (const [id, d] of Object.entries(info.downloads || {})) if (!downloads.has(id)) downloads.set(id, d);
      draw();
    } catch (error) {
      root.replaceChildren(el("p", { class: "note", text: `Voice settings are unavailable: ${error.message}` }));
    }
  }

  async function settings(body) {
    try {
      const out = await api("/voice/settings", { method: "POST", body });
      info = { ...info, ...out };
      ctx.voiceInfo = info;
      ctx.onVoiceSettings?.(info);
      await load();
    } catch (error) { toast(error.message, "error"); await load(); }
  }

  function draw() {
    offs.splice(0).forEach((off) => off());
    const models = info.models || [];
    const kokoro = models.find((m) => m.id.startsWith("kokoro"));
    const parts = [
      el("p", { class: "note" }, icon("info", 13),
        " Everything here is off until you choose it. Models download only when you click Download."),
      el("label", { class: "check switch-row" },
        el("input", { type: "checkbox", checked: info.auto_speak, "aria-label": "Auto-speak replies", onchange: (e) => toggleAuto(e.target.checked) }),
        el("span", {}, el("strong", { text: "Auto-speak replies" }), el("span", { class: "note block", text: "Read each finished reply aloud." }))),
      engineBox(kokoro),
    ];
    for (const [kind, title, help] of GROUPS) {
      const rows = models.filter((m) => m.kind === kind);
      if (!rows.length) continue;
      parts.push(el("h4", { text: title }), el("p", { class: "note", text: help }),
        el("div", { class: "model-rows" }, rows.map((m) => modelRow(m))));
    }
    root.replaceChildren(...parts);
  }

  async function toggleAuto(on) {
    if (on && !info.ready) { ctx.requestAutoSpeak?.(); return; }
    await settings({ auto_speak: on });
  }

  function engineBox(kokoro) {
    const piperDesc = "Small and fast. Recommended.";
    const kokoroDesc = kokoro ? `Higher quality, optional install (${kokoro.size_mb} MB).` : "Higher quality, optional install.";
    const choose = async (engine) => {
      if (engine === info.engine) return;
      if (engine === "kokoro" && kokoro && !(kokoro.installed && kokoro.package_installed !== false)) {
        const status = el("span", { class: "note", role: "status", text: "Installing Kokoro…" });
        box.append(status);
        try {
          await downloadItem(kokoro.id, (d) => { status.textContent = progressText(d); });
        } catch (error) { toast(error.message, "error"); await load(); return; }
      }
      await settings({ engine });
    };
    const option = (id, title, desc) => el("label", { class: `engine${info.engine === id ? " selected" : ""}` },
      el("input", { type: "radio", name: "engine", value: id, checked: info.engine === id, onchange: () => choose(id) }),
      el("span", {}, el("strong", { text: title }), el("span", { class: "note block", text: desc })));
    const box = el("div", { class: "engine-box", role: "radiogroup", "aria-label": "Voice engine" },
      option("piper", "Piper", piperDesc), option("kokoro", "Kokoro", kokoroDesc));
    if (info.engine === "kokoro") {
      box.append(el("label", { class: "field" }, el("span", { text: "Kokoro voice" }),
        el("select", { "aria-label": "Kokoro voice", onchange: (e) => settings({ kokoro_voice: e.target.value }) },
          KOKORO_VOICES.map((v) => el("option", { value: v, text: v, selected: v === info.kokoro_voice })))));
    }
    return box;
  }

  function modelRow(m) {
    const progress = el("progress", { max: "1", hidden: true, "aria-label": `Download ${m.label}` });
    const status = el("span", { class: "note", role: "status" });
    const actions = el("div", { class: "row-actions" });
    const isVoice = m.kind === "tts" && m.engine === "piper";
    const selected = isVoice && info.piper_voice === m.id.replace(/^piper:/, "");

    function draw(d) {
      if (d && (d.status === "installing" || d.status === "downloading")) {
        progress.hidden = false;
        if (d.progress === null || d.progress === undefined) progress.removeAttribute("value"); else progress.value = d.progress;
        status.textContent = progressText(d);
      } else if (d && d.status === "error") {
        progress.hidden = true; status.textContent = progressText(d);
      } else { progress.hidden = true; status.textContent = ""; }
    }
    const last = downloads.get(m.id);
    draw(last && last.status !== "done" ? last : null);
    offs.push(onDownload((d) => {
      if (d.item_id !== m.id) return;
      if (d.status === "done") load(); else draw(d);
    }));

    if (m.installed) {
      if (isVoice) {
        actions.append(el("button", {
          class: "small", text: "Preview", "aria-label": `Preview ${m.label}`,
          onclick: async () => {
            if (!selected) await settings({ piper_voice: m.id.replace(/^piper:/, "") });
            ctx.previewVoice?.(SAMPLE, row);
          },
        }), selected ? el("span", { class: "badge ok", text: "In use" })
          : el("button", { class: "small", text: "Use", onclick: () => settings({ piper_voice: m.id.replace(/^piper:/, "") }) }));
      } else if (m.kind === "tts") {
        actions.append(el("button", { class: "small", text: "Preview", onclick: () => ctx.previewVoice?.(SAMPLE, row) }));
      }
    } else {
      actions.append(el("button", {
        class: "small primary", text: "Download", "aria-label": `Download ${m.label} (${m.size_mb} MB)`,
        onclick: async (e) => {
          e.currentTarget.disabled = true;
          draw({ status: "downloading", progress: null, message: "Starting…" });
          try { await downloadItem(m.id, draw); await load(); }
          catch (error) { toast(error.message, "error"); await load(); }
        },
      }));
    }
    const row = el("div", { class: `model-item${m.installed ? " installed" : ""}${selected ? " selected" : ""}` },
      el("div", { class: "model-main" },
        el("span", { class: `model-check${m.installed ? " on" : ""}`, role: "img", "aria-label": m.installed ? "Installed" : "Not installed" }, m.installed ? icon("check", 13) : null),
        el("div", { class: "model-text" },
          el("strong", { text: m.label }),
          el("span", { class: "note", text: `${m.size_mb} MB${m.default ? " · default" : ""}` }))),
      actions, progress, status);
    return row;
  }

  load();
  return { refresh: load };
}
