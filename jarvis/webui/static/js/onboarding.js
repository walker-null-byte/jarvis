// First-run wizard: welcome, connect a model, pick a character, quick tour.
import { api } from "./api.js";
import { avatar } from "./avatar.js";
import { signInFlow, signInLink } from "./auth.js";
import { Select } from "./combo.js";
import { el, toast } from "./dom.js";
import { icon } from "./icons.js";
import { providerOptions, shortName } from "./switcher.js";

const STEPS = ["Welcome", "Connect a model", "Choose a character", "Quick tour"];

const TIPS = [
  ["sliders", "Switch model any time", "The model pill in the header changes provider or model for your next message, handy when a quota runs out."],
  ["alert", "You stay in control", "Risky commands ask for approval first. You approve or deny them right in the chat."],
  ["terminal", "Slash commands", "Type / in the message box to see commands for settings, memory, characters and more."],
  ["speaker", "Voice is off by default", "Turn on Auto-speak with the speaker toggle in the header. Models download only when you ask."],
  ["pencil", "Edit, Re-do, Speak", "Hover a message to copy it, edit your own message and resend, re-do a reply, or hear it read aloud."],
];

export function openOnboarding(ctx) {
  return new Promise((resolve) => {
    const dialog = el("dialog", { class: "modal wizard", "aria-label": "Welcome to Jarvis" });
    const state = { step: 0, connected: false, character: null, characters: [], active: null };
    const body = el("div", { class: "wizard-body" });
    const dots = el("ol", { class: "wizard-steps", "aria-label": "Progress" });
    const back = el("button", { type: "button", text: "Back" });
    const next = el("button", { type: "button", class: "primary", text: "Next" });
    const skip = el("button", { type: "button", class: "ghost", text: "Skip for now" });
    dialog.append(dots, body, el("div", { class: "wizard-actions" }, skip, el("span", { class: "spacer" }), back, next));

    let finished = false;
    async function finish() {
      if (finished) return;
      finished = true;
      try { await api("/onboarding/done", { method: "POST" }); } catch (error) { toast(error.message, "error"); }
      dialog.close();
      dialog.remove();
      ctx.reloadSnapshot?.().catch(() => {});
      resolve();
    }
    skip.addEventListener("click", finish);
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); finish(); });

    function go(step) {
      state.step = step;
      dots.replaceChildren(...STEPS.map((label, i) => el("li", {
        class: i === step ? "current" : i < step ? "done" : "", "aria-current": i === step ? "step" : null,
      }, el("span", { class: "step-num" }, i < step ? icon("check", 12) : String(i + 1)), el("span", { class: "step-label", text: label }))));
      back.hidden = step === 0;
      next.textContent = step === STEPS.length - 1 ? "Start chatting" : step === 0 ? "Get started" : "Next";
      skip.hidden = step === STEPS.length - 1;
      body.replaceChildren();
      [stepWelcome, stepModel, stepCharacter, stepTour][step](body, state);
      body.querySelector("h2")?.setAttribute("tabindex", "-1");
      body.querySelector("h2")?.focus({ preventScroll: true });
    }

    back.addEventListener("click", () => go(state.step - 1));
    next.addEventListener("click", async () => {
      if (state.step === 2 && state.character && state.character !== state.active) {
        try { await api(`/characters/${encodeURIComponent(state.character)}/activate`, { method: "POST" }); state.active = state.character; }
        catch (error) { toast(error.message, "error"); }
      }
      if (state.step === STEPS.length - 1) finish(); else go(state.step + 1);
    });

    document.body.append(dialog);
    dialog.showModal();
    go(0);
  });

  // ---------- steps ----------

  function stepWelcome(root) {
    root.append(
      el("div", { class: "wizard-hero" }, el("span", { class: "wizard-mark" }, icon("sparkle", 28))),
      el("h2", { text: "Welcome to Jarvis" }),
      el("p", { class: "lead", text: "Jarvis is a local AI agent that runs on your computer. It can use tools, keep a memory of what matters to you, and write reusable skills for itself." }),
      el("ul", { class: "feature-list" },
        feature("terminal", "Tools", "Reads files, runs commands (with your approval), searches, and more."),
        feature("brain", "Memory", "Remembers your preferences and facts across conversations."),
        feature("sparkle", "Learns over time", "Reviews its own work and turns what works into skills.")),
      el("p", { class: "note", text: "Setup takes about a minute. You can change everything later." }));
  }

  function feature(name, title, text) {
    return el("li", {}, el("span", { class: "feature-icon" }, icon(name, 18)),
      el("div", {}, el("strong", { text: title }), el("div", { class: "note", text })));
  }

  function stepModel(root, state) {
    root.append(el("h2", { text: "Connect a model" }),
      el("p", { class: "lead", text: "Jarvis needs a language model to think with. Pick one way to connect." }));
    const result = el("div", { class: "connect-result", role: "status" });
    const picker = el("div", { class: "connect-picker" });

    // --- primary: ChatGPT ---
    const chatNote = el("span", { class: "note", role: "status" });
    const chatBtn = el("button", { type: "button", class: "primary big", text: "Sign in with ChatGPT" });
    chatBtn.addEventListener("click", async () => {
      chatBtn.disabled = true;
      chatNote.textContent = "Opening the sign-in page…";
      try {
        await signInFlow({
          onLink: (f) => chatNote.replaceChildren("Waiting for you to finish in the new tab. If none opened, ", signInLink(f.auth_url), "."),
        });
        chatNote.textContent = "Signed in. Selecting the provider…";
        await useProvider({ provider: "chatgpt" }, "ChatGPT");
      } catch (error) { chatNote.textContent = error.message; }
      chatBtn.disabled = false;
    });
    const primary = el("section", { class: "connect primary-card" },
      el("div", { class: "connect-head" }, el("span", { class: "feature-icon" }, icon("cloud", 18)), el("h3", { text: "Sign in with ChatGPT" })),
      el("p", { text: "Use your existing ChatGPT Plus or Pro plan — no API key, no separate billing." }),
      chatBtn, chatNote);

    // --- local ---
    const localBox = el("div", { class: "connect-body" });
    const presets = [["lmstudio", "LM Studio"], ["ollama", "Ollama"]];
    let localChoice = "lmstudio";
    const localTabs = el("div", { class: "seg-control", role: "radiogroup", "aria-label": "Local server" },
      presets.map(([id, label]) => el("button", {
        type: "button", role: "radio", "aria-checked": String(id === localChoice), class: id === localChoice ? "active" : "", text: label,
        onclick: (e) => {
          localChoice = id;
          localTabs.querySelectorAll("button").forEach((b) => { b.classList.toggle("active", b === e.currentTarget); b.setAttribute("aria-checked", String(b === e.currentTarget)); });
        },
      })));
    const localOut = el("span", { class: "note", role: "status" });
    const localTest = el("button", { type: "button", text: "Test connection" });
    const localUse = el("button", { type: "button", class: "primary", text: "Use this", hidden: true });
    localTest.addEventListener("click", async () => {
      localTest.disabled = true;
      localOut.textContent = "Testing…";
      try {
        const out = await api("/provider/test", { method: "POST", body: { provider: localChoice } });
        localOut.textContent = out.ok ? `Connected. ${out.models?.length || 0} model(s) found.` : `Not reachable: ${out.message}`;
        localUse.hidden = !out.ok;
      } catch (error) { localOut.textContent = error.message; }
      localTest.disabled = false;
    });
    localUse.addEventListener("click", () => useProvider({ provider: localChoice }, presets.find((p) => p[0] === localChoice)[1]));
    localBox.append(el("p", { class: "note", text: "Run a model on your own machine. Start the server first, then test the connection." }),
      localTabs, el("div", { class: "row" }, localTest, localUse), localOut);

    // --- API key ---
    const keyBox = el("div", { class: "connect-body" });
    const providerSel = new Select({ label: "API provider", placeholder: "Loading…", onchange: () => syncKeyHint() });
    const keyNote = el("p", { class: "key-status", role: "status" });
    const keyInput = el("input", { type: "password", autocomplete: "new-password", spellcheck: false, placeholder: "Paste your key", "aria-label": "API key" });
    const remember = el("input", { type: "checkbox", checked: true, id: "onboarding-remember" });
    const rememberRow = el("label", { class: "check-row", for: "onboarding-remember" }, remember,
      el("span", { text: "Remember on this computer" }));
    const keyUse = el("button", { type: "button", class: "primary", text: "Use this provider" });
    let providers = [];
    api("/providers").then((data) => {
      providers = data.providers.filter((p) => p.kind === "api_key" && p.id !== "custom");
      providerSel.setOptions(providerOptions(providers, { skipCustom: true }).filter((o) => o.value !== "chatgpt"));
      providerSel.setValue(providers[0]?.id || "");
      syncKeyHint();
    }).catch(() => {});
    function syncKeyHint() {
      const p = providers.find((q) => q.id === providerSel.value);
      const envName = p?.api_key_env;
      keyInput.placeholder = envName ? `Paste your key (or leave empty to use ${envName})` : "Paste your key";
      const have = p?.saved || p?.env_set;
      keyNote.className = `key-status ${have ? "ok" : ""}`;
      const text = p?.saved ? `Saved key ${p.saved_hint}` : p?.env_set ? `Using ${envName} from your environment.`
        : "Paste a key. Tick the box to keep it on this computer; otherwise it lasts for this session only.";
      keyNote.replaceChildren(...(p ? [icon(have ? "check" : "info", 15), el("span", { text })] : []));
    }
    keyUse.addEventListener("click", () => {
      const chosen = providers.find((p) => p.id === providerSel.value);
      if (!chosen) return;
      const typed = keyInput.value.trim();
      if (!typed && !chosen.env_set && !chosen.saved) {
        keyNote.className = "key-status bad";
        keyNote.replaceChildren(icon("alert", 15), el("span", { text: "Paste an API key first." }));
        keyInput.focus();
        return;
      }
      // Keep the real preset (its backend matters, e.g. Anthropic); the server
      // applies the typed env var name or session-only key on top of it.
      const body = typed ? { provider: chosen.id, api_key_env: typed, remember: remember.checked } : { provider: chosen.id };
      useProvider(body, shortName(chosen)).then(() => { keyInput.value = ""; return api("/providers"); })
        .then((data) => { providers = data.providers.filter((p) => p.kind === "api_key" && p.id !== "custom"); syncKeyHint(); })
        .catch(() => {});
    });
    keyBox.append(el("p", { class: "note", text: "Pay-as-you-go keys from OpenAI, Google, Anthropic, OpenRouter and others." }),
      el("div", { class: "field" }, el("span", { text: "Provider" }), providerSel.node),
      el("label", { class: "field" }, el("span", { text: "API key" }), keyInput),
      rememberRow, keyNote, el("div", { class: "row" }, keyUse));

    async function useProvider(body, label) {
      result.replaceChildren(el("span", { class: "note", text: `Connecting to ${label}…` }));
      try {
        const out = await api("/provider", { method: "POST", body });
        ctx.setStatus(out.status);
        state.connected = true;
        const data = await api("/models").catch(() => ({ models: [], current: "" }));
        const known = (data.models || []).includes(data.current);
        const choices = known ? data.models : ["", ...(data.models || [])];
        const sel = el("select", { "aria-label": "Model" }, choices.map((m) => el("option", {
          value: m, text: m || "Choose a model…", selected: m === (known ? data.current : "") })));
        sel.addEventListener("change", async () => {
          if (!sel.value) return;
          try { const o = await api("/provider", { method: "POST", body: { provider: out.status.provider, model: sel.value } }); ctx.setStatus(o.status); }
          catch (error) { toast(error.message, "error"); }
        });
        const status = out.ok === false
          ? el("div", { class: "err-line" }, icon("alert", 16),
            el("span", { text: `${label} is selected, but the connection test failed: ${out.message}` }))
          : el("div", { class: "ok-line" }, icon("check", 16), el("strong", { text: `Connected to ${label}` }));
        result.replaceChildren(
          status,
          (data.models || []).length
            ? el("label", { class: "field" }, el("span", { text: "Model" }), sel)
            : el("p", { class: "note", text: data.message || "No model list available. You can set a model later in Settings." }));
      } catch (error) {
        result.replaceChildren(el("div", { class: "err-line" }, icon("alert", 16), el("span", { text: error.message })));
      }
    }

    root.append(primary, result, el("div", { class: "alt-heading", text: "Or use something else" }),
      collapsible("server", "Local model", "LM Studio or Ollama on this computer", localBox),
      collapsible("key", "API key", "OpenAI, Anthropic, OpenRouter and others", keyBox), picker);
  }

  function collapsible(iconName, title, subtitle, content) {
    return el("details", { class: "connect alt" },
      el("summary", {}, el("span", { class: "feature-icon" }, icon(iconName, 16)),
        el("span", { class: "alt-text" }, el("strong", { text: title }), el("span", { class: "note", text: subtitle })),
        icon("down", 16)), content);
  }

  function stepCharacter(root, state) {
    root.append(el("h2", { text: "Choose a character" }),
      el("p", { class: "lead", text: "A character sets Jarvis's voice and personality. Your memory and tools stay the same." }));
    const grid = el("div", { class: "char-pick", role: "radiogroup", "aria-label": "Characters" });
    root.append(grid);
    api("/characters").then(({ characters, active }) => {
      state.active = active;
      state.character = state.character || active;
      const builtin = characters.filter((c) => c.builtin);
      const shown = builtin.length ? builtin : characters;
      const draw = () => grid.replaceChildren(...shown.map((card) => el("button", {
        type: "button", role: "radio", "aria-checked": String(card.id === state.character),
        class: `char-option${card.id === state.character ? " selected" : ""}`,
        onclick: () => { state.character = card.id; draw(); },
      }, avatar(card, 40),
        el("span", { class: "char-text" }, el("strong", { text: card.name }), el("span", { class: "note", text: card.description || "" })),
        card.id === state.character ? icon("check", 16) : null)));
      draw();
    }).catch((error) => grid.replaceChildren(el("p", { class: "note", text: error.message })));
  }

  function stepTour(root) {
    root.append(el("h2", { text: "Quick tour" }),
      el("ul", { class: "tips" }, TIPS.map(([name, title, text]) => feature(name, title, text))));
  }
}
