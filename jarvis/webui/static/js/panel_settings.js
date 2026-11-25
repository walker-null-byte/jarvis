import { api } from "./api.js";
import { ComboBox, Select } from "./combo.js";
import { el, toast } from "./dom.js";
import { icon } from "./icons.js";
import { providerKind, providerOptions, shortName } from "./switcher.js";

function field(label, control) {
  return el("label", { class: "field" }, el("span", { text: label }), control);
}

const KIND_HELP = {
  subscription: "Uses your ChatGPT plan. No API key needed.",
  local: "Runs on this computer. Start the server first, then test the connection.",
  api_key: "Billed by the provider. Needs an API key.",
};
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function renderSettings(container, ctx) {
  // Set when the last Apply/Test failed in a way that points at the key (401/403, "API key"…).
  let keyRejected = false;
  const provider = new Select({ label: "Provider", placeholder: "Loading providers…", onchange: () => { dirty = true; keyRejected = false; syncProviderFields(); } });
  const description = el("p", { class: "note" });
  const baseUrl = el("input", { type: "url", placeholder: "https://host/v1", "aria-label": "Base URL", autocomplete: "off", spellcheck: false });
  const baseUrlField = el("label", { class: "field" }, el("span", { text: "Base URL (required)" }), baseUrl);
  const endpointShown = el("input", { type: "text", readonly: true, "aria-label": "Endpoint" });
  const advanced = el("details", { class: "disclosure" }, el("summary", { text: "Advanced" }),
    el("div", { class: "disclosure-body" }, el("label", { class: "field" }, el("span", { text: "Endpoint" }), endpointShown)));
  const keyStatus = el("p", { class: "key-status", id: "provider-key-status", role: "status" });
  const keyInput = el("input", { type: "password", autocomplete: "new-password", spellcheck: false,
    "aria-label": "API key", "aria-describedby": "provider-key-status" });
  const keyLabel = el("span", { text: "API key" });
  const keyField = el("label", { class: "field" }, keyLabel, keyInput);
  const modelBox = new ComboBox({ label: "Model", placeholder: "model id", onfocus: () => ensureModels() });
  const refreshModels = el("button", { type: "button", class: "icon-btn", "aria-label": "Reload model list", title: "Reload model list" }, icon("redo", 16));
  const modelField = el("div", { class: "field" }, el("label", { for: "settings-model" }, el("span", { text: "Model" })),
    el("div", { class: "input-row" }, modelBox.node, refreshModels));
  modelBox.input.id = "settings-model";
  modelBox.input.removeAttribute("aria-label");
  const result = el("div", { class: "status-line", role: "status", "aria-live": "polite" });
  const apply = el("button", { type: "button", class: "primary", text: "Apply provider" });
  const test = el("button", { type: "button", text: "Test connection" });
  const chatgpt = el("div", { class: "chatgpt", hidden: true });
  const toolMode = el("select", { "aria-label": "Tool mode" }, [
    el("option", { value: "search", text: "search (core tools + TurboMCP discovery)" }),
    el("option", { value: "all", text: "all (send every tool schema)" })]);
  const security = el("input", { type: "text", readonly: true, "aria-label": "Security mode" });
  let providers = [];
  let settings = {};
  let info = { session_key: false, active_key_env: "", active_key_env_set: false };
  let live = { provider: "", model: "", baseUrl: "" };  // what the agent is using right now
  let loadedFor = "";  // "provider|baseUrl|key?" the model list was fetched for
  let modelRun = 0;
  let dirty = false;  // unsaved edits: do not reload over them when the tab is re-shown

  const providerField = el("div", { class: "field" }, el("span", { text: "Provider" }), provider.node);
  container.append(
    el("h4", { text: "Provider" }), providerField, description,
    baseUrlField, keyField, keyStatus, modelField, advanced,
    el("div", { class: "btn-row" }, apply, test), result, chatgpt,
    el("h4", { text: "Voice" }),
    el("p", { class: "note" }, "Voice engine, models and Auto-speak live in the ",
      el("button", { class: "link", type: "button", text: "Voice tab", onclick: () => ctx.openTab?.("voice") }), "."),
    el("h4", { text: "Welcome" }),
    el("div", { class: "panel-tools" }, el("button", { type: "button", text: "Show welcome tour", onclick: () => ctx.showTour?.() })),
    el("h4", { text: "Agent" }), field("Tool mode", toolMode), field("Security mode (read-only)", security),
    el("div", { class: "panel-tools" }, el("button", { class: "primary", type: "button", text: "Save settings", onclick: save })));

  const chosen = () => providers.find((p) => p.id === provider.value);
  const isCustom = () => provider.value === "custom";
  const usesKey = (p) => !!p && providerKind(p) === "api_key";

  // ---- status line ----
  function show(kind, message, raw) {
    const icons = { ok: "check", bad: "alert", busy: null };
    result.replaceChildren(...[
      el("div", { class: `status-msg ${kind}` }, kind === "busy" ? el("span", { class: "spin on" }) : icon(icons[kind], 16), el("span", { text: message })),
      raw ? el("details", { class: "disclosure" }, el("summary", { text: "Details" }), el("div", { class: "disclosure-body" }, el("pre", { class: "raw-output", text: raw }))) : null].filter(Boolean));
  }
  const clearResult = () => result.replaceChildren();

  async function setSetting(path, value) {
    try {
      const out = await api("/settings", { method: "POST", body: { path, value } });
      toast(out.output);
      ctx.setStatus(out.status);
    } catch (error) { toast(error.message, "error"); await load(); }
  }

  async function save() {
    try { await api("/settings/save", { method: "POST" }); toast("Settings saved"); }
    catch (error) { toast(error.message, "error"); }
  }

  // ---- key status ----
  function setKeyStatus(kind, text) {
    const names = { ok: "check", bad: "alert", info: "info" };
    keyStatus.className = `key-status ${kind}`;
    keyStatus.replaceChildren(icon(names[kind], 15), el("span", { text }));
  }

  function hasKey(p) {
    if (!usesKey(p) || p.id === "custom") return true;  // custom endpoints often need no key
    if (keyInput.value.trim()) return true;
    if (p.env_set) return true;
    return p.id === live.provider && (info.session_key || info.active_key_env_set);
  }

  const KEY_FAILURE = /api[ _-]?key|unauthori[sz]ed|invalid.*key|permission|forbidden|HTTP 40[13]/i;
  function noteResult(out) {
    keyRejected = !out.ok && usesKey(chosen()) && KEY_FAILURE.test(out.message || "");
    updateKeyStatus();
  }

  function updateKeyStatus() {
    const p = chosen();
    keyStatus.hidden = !p || p.backend === "chatgpt";
    if (keyStatus.hidden) return;
    if (keyRejected && !keyInput.value.trim()) {
      setKeyStatus("bad", "The provider rejected this key. Paste a valid one and try again.");
      return;
    }
    if (!usesKey(p)) { setKeyStatus("info", "No API key needed for a local server."); return; }
    const typed = keyInput.value.trim();
    if (p.id === "custom" && !typed) {
      setKeyStatus("info", p.id === live.provider && info.session_key ? "Key set for this session." : "Optional. Only needed if your endpoint requires one.");
      return;
    }
    if (typed) {
      setKeyStatus("info", ENV_NAME.test(typed) && typed === typed.toUpperCase() && typed.length > 3
        ? `Will use the environment variable ${typed}.` : "Key ready. It is kept for this session only, never saved to disk.");
    } else if (p.env_set) setKeyStatus("ok", `Using ${p.api_key_env} from your environment.`);
    else if (p.id === live.provider && info.active_key_env_set) setKeyStatus("ok", `Using ${info.active_key_env} from your environment.`);
    else if (p.id === live.provider && info.session_key) setKeyStatus("ok", "Key set for this session.");
    else setKeyStatus("bad", p.api_key_env ? `No key yet. Paste one, or set ${p.api_key_env} and restart.` : "No key yet. Paste one above.");
  }

  function syncProviderFields() {
    const p = chosen();
    const kind = p ? (isCustom() ? "api_key" : providerKind(p)) : "";
    description.textContent = isCustom() ? "Any server that speaks the OpenAI chat API (vLLM, a proxy, a self-hosted gateway)." : KIND_HELP[kind] || "";
    // Base URL: editable and required for Custom only; presets show it under Advanced.
    baseUrlField.hidden = !isCustom();
    baseUrl.value = isCustom() ? (p && p.id === live.provider ? live.baseUrl : "") : "";
    advanced.hidden = isCustom() || !p || p.backend === "chatgpt";
    endpointShown.value = p ? p.base_url : "";
    advanced.open = false;
    keyField.hidden = !usesKey(p);
    keyLabel.textContent = isCustom() ? "API key (optional)" : "API key";
    keyInput.value = "";
    keyInput.placeholder = p && p.api_key_env ? `Paste your key (or leave empty to use ${p.api_key_env})` : "Paste your key";
    updateKeyStatus();
    const isLive = !!p && p.id === live.provider;
    modelBox.value = isLive ? live.model : (p && p.default_model) || "";
    modelBox.input.placeholder = p && p.default_model ? p.default_model : "model id";
    modelBox.setState({ items: [], error: "", loading: false });
    loadedFor = "";
    clearResult();
    renderChatgpt(!!p && p.backend === "chatgpt");
    if (p) ensureModels();
  }

  function requestBody(extra = {}) {
    const p = chosen();
    const body = { provider: provider.value, base_url: baseUrl.value.trim(), ...extra };
    if (usesKey(p) && keyInput.value.trim()) body.api_key_env = keyInput.value.trim();
    return body;
  }

  // ---- model list (follows the provider dropdown) ----
  const modelKey = () => `${provider.value}|${baseUrl.value.trim()}|${keyInput.value.trim() ? "k" : ""}`;

  async function ensureModels(force = false) {
    const p = chosen();
    if (!p) return;
    if (!force && loadedFor === modelKey()) return;
    if (isCustom() && !baseUrl.value.trim()) { modelBox.setState({ items: [], error: "Enter a base URL to list models.", loading: false }); return; }
    if (!hasKey(p)) { modelBox.setState({ items: [], error: "Add an API key to list models. You can still type a model id.", loading: false }); loadedFor = ""; return; }
    const run = ++modelRun;
    const key = modelKey();
    modelBox.setState({ loading: true, error: "" });
    refreshModels.disabled = true;
    try {
      const out = await api("/provider/test", { method: "POST", body: requestBody() });
      if (run !== modelRun) return;
      loadedFor = key;
      modelBox.setState({ loading: false, items: out.models || [], error: out.ok || (out.models || []).length ? "" : `Could not list models: ${out.message}` });
    } catch (error) {
      if (run !== modelRun) return;
      modelBox.setState({ loading: false, error: error.message });
    } finally { if (run === modelRun) refreshModels.disabled = false; }
  }
  refreshModels.addEventListener("click", () => { modelBox.input.focus(); ensureModels(true); });
  keyInput.addEventListener("input", () => { dirty = true; keyRejected = false; updateKeyStatus(); });
  modelBox.input.addEventListener("input", () => { dirty = true; });
  keyInput.addEventListener("change", () => ensureModels());
  baseUrl.addEventListener("change", () => ensureModels());

  // ---- apply / test ----
  function problem() {
    const p = chosen();
    if (!p) return "Choose a provider first.";
    if (isCustom() && !baseUrl.value.trim()) return "Enter the base URL of your endpoint.";
    return "";
  }

  async function run(button, work) {
    apply.disabled = test.disabled = true;
    button.classList.add("busy");
    try { await work(); } catch (error) { show("bad", error.message); toast(error.message, "error"); }
    apply.disabled = test.disabled = false;
    button.classList.remove("busy");
  }

  apply.addEventListener("click", () => {
    const issue = problem();
    if (issue) { show("bad", issue); return; }
    run(apply, async () => {
      const name = shortName(chosen());
      show("busy", `Switching to ${name}…`);
      const out = await api("/provider", { method: "POST", body: requestBody({ model: modelBox.value.trim() }) });
      ctx.setStatus(out.status);
      const model = out.status?.model ? ` · ${out.status.model}` : "";
      if (out.ok === false) { show("bad", `Using ${name}${model}, but the connection test failed: ${out.message}`, out.output) }
      else { show("ok", `Using ${name}${model}. Connection OK.`, out.output); toast(`Switched to ${name}`); }
      await load({ keepResult: true });
      noteResult(out);
    });
  });

  test.addEventListener("click", () => {
    const issue = problem();
    if (issue) { show("bad", issue); return; }
    run(test, async () => {
      show("busy", "Testing connection…");
      const out = await api("/provider/test", { method: "POST", body: requestBody() });
      loadedFor = modelKey();
      modelBox.setState({ items: out.models || [], error: "", loading: false });
      const count = (out.models || []).length;
      if (out.ok) show("ok", `Connected. ${count} model${count === 1 ? "" : "s"} available.`, out.message);
      else show("bad", `Could not connect: ${out.message}`);
      noteResult(out);
    });
  });

  toolMode.addEventListener("change", () => setSetting("agent.tool_mode", toolMode.value));

  // ---- Sign in with ChatGPT ----
  let pollTimer;
  async function renderChatgpt(visible) {
    chatgpt.hidden = !visible;
    clearTimeout(pollTimer);
    if (!visible) return;
    let status;
    try { status = await api("/auth/chatgpt"); }
    catch (error) { chatgpt.replaceChildren(el("p", { class: "note", text: error.message })); return; }
    const line = status.signed_in
      ? `Signed in${status.email ? ` as ${status.email}` : ""}${status.plan_ok === false ? ` — ${status.error || "this plan can't be used by Jarvis"}` : ""}`
      : status.pending ? "Waiting for the browser sign-in to finish…" : (status.error || "Not signed in");
    chatgpt.replaceChildren(el("p", { class: "note", role: "status", text: line }),
      status.signed_in
        ? el("button", { text: "Sign out", onclick: async () => { await api("/auth/chatgpt/logout", { method: "POST" }); renderChatgpt(true); } })
        : el("button", { class: "primary", text: "Sign in with ChatGPT", onclick: signIn }));
    if (status.pending) pollTimer = setTimeout(() => renderChatgpt(true), 2000);
  }

  async function signIn() {
    try {
      const { auth_url } = await api("/auth/chatgpt/login", { method: "POST" });
      window.open(auth_url, "_blank", "noopener");
      chatgpt.append(el("p", { class: "note" }, "If no tab opened, ",
        el("a", { href: auth_url, target: "_blank", rel: "noopener noreferrer", text: "open the sign-in page" }), "."));
      pollTimer = setTimeout(() => renderChatgpt(true), 2000);
    } catch (error) { toast(error.message, "error"); }
  }

  async function load({ keepResult = false } = {}) {
    try {
      const [prov, set, status] = await Promise.all([api("/providers"), api("/settings"), api("/status")]);
      providers = prov.providers;
      info = { session_key: prov.session_key, active_key_env: prov.active_key_env, active_key_env_set: prov.active_key_env_set };
      live = { provider: prov.active, model: status.model || "", baseUrl: status.base_url || "" };
      dirty = false;
      provider.setOptions(providerOptions(providers));
      provider.setValue(prov.active);
      settings = set.settings;
      ctx.settings = settings;
      toolMode.value = settings["agent.tool_mode"] || "search";
      security.value = settings["security.mode"] || status.security_mode || "";
      const kept = keepResult ? result.cloneNode(true) : null;
      syncProviderFields();
      if (kept) result.replaceChildren(...kept.childNodes);
    } catch (error) { toast(error.message, "error"); }
  }
  load();
  return { refresh: () => { if (!dirty) load({ keepResult: true }); } };
}
