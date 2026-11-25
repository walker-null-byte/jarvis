import { api } from "./api.js";
import { el, toast } from "./dom.js";

function field(label, control) {
  return el("label", { class: "field" }, el("span", { text: label }), control);
}

export function renderSettings(container, ctx) {
  const provider = el("select", { "aria-label": "Provider" });
  const description = el("p", { class: "note" });
  const baseUrl = el("input", { type: "text", placeholder: "http://localhost:1234/v1", "aria-label": "Base URL" });
  const keyEnv = el("input", { type: "text", placeholder: "API key or env var name (custom only)", "aria-label": "API key", autocomplete: "off" });
  const model = el("input", { type: "text", list: "model-options", "aria-label": "Model", placeholder: "model id" });
  const models = el("datalist", { id: "model-options" });
  const result = el("pre", { class: "tool-result", hidden: true });
  const apply = el("button", { class: "primary", text: "Apply provider" });
  const test = el("button", { text: "Test connection" });
  const refreshModels = el("button", { text: "Refresh models" });
  const chatgpt = el("div", { class: "chatgpt", hidden: true });
  const toolMode = el("select", { "aria-label": "Tool mode" }, [
    el("option", { value: "search", text: "search (core tools + TurboMCP discovery)" }),
    el("option", { value: "all", text: "all (send every tool schema)" })]);
  const security = el("input", { type: "text", readonly: true, "aria-label": "Security mode" });
  let providers = [];
  let settings = {};

  container.append(
    el("h4", { text: "Provider" }), field("Provider", provider), description,
    field("Base URL", baseUrl), field("API key / env var", keyEnv), field("Model", model), models,
    el("div", { class: "panel-tools" }, apply, test, refreshModels), result, chatgpt,
    el("h4", { text: "Voice" }),
    el("p", { class: "note" }, "Voice engine, models and Auto-speak live in the ",
      el("button", { class: "link", type: "button", text: "Voice tab", onclick: () => ctx.openTab?.("voice") }), "."),
    el("h4", { text: "Welcome" }),
    el("div", { class: "panel-tools" }, el("button", { text: "Show welcome tour", onclick: () => ctx.showTour?.() })),
    el("h4", { text: "Agent" }), field("Tool mode", toolMode), field("Security mode (read-only)", security),
    el("div", { class: "panel-tools" }, el("button", { class: "primary", text: "Save settings", onclick: save })));

  function show(text) { result.hidden = false; result.textContent = text; }

  async function setSetting(path, value) {
    try {
      const out = await api("/settings", { method: "POST", body: { path, value } });
      toast(out.output);
      ctx.setStatus(out.status);
    } catch (error) { toast(error.message, "error"); await load(); }
  }

  async function save() {
    try { toast((await api("/settings/save", { method: "POST" })).output); }
    catch (error) { toast(error.message, "error"); }
  }

  function syncProviderFields() {
    const chosen = providers.find((p) => p.id === provider.value);
    description.textContent = chosen ? `${chosen.description} (${chosen.backend} backend)` : "";
    if (chosen && chosen.id !== "custom") baseUrl.value = chosen.base_url;
    baseUrl.readOnly = !!chosen && chosen.id !== "custom";
    keyEnv.disabled = !chosen || chosen.id !== "custom";
    renderChatgpt(chosen && chosen.backend === "chatgpt");
  }
  provider.addEventListener("change", syncProviderFields);

  apply.addEventListener("click", async () => {
    apply.disabled = true;
    try {
      const out = await api("/provider", { method: "POST", body: {
        provider: provider.value, base_url: baseUrl.value, model: model.value, api_key_env: keyEnv.value } });
      show(out.output);
      ctx.setStatus(out.status);
      keyEnv.value = "";
    } catch (error) { toast(error.message, "error"); }
    apply.disabled = false;
  });

  test.addEventListener("click", async () => {
    test.disabled = true;
    try {
      const out = await api("/provider/test", { method: "POST", body: {
        provider: provider.value, base_url: baseUrl.value, api_key_env: keyEnv.value } });
      show(`${out.ok ? "OK" : "Failed"} — ${out.message}`);
      fillModels(out.models);
    } catch (error) { toast(error.message, "error"); }
    test.disabled = false;
  });

  function fillModels(ids = []) {
    models.replaceChildren(...ids.map((id) => el("option", { value: id })));
  }

  refreshModels.addEventListener("click", async () => {
    try {
      const out = await api("/models");
      fillModels(out.models);
      show(out.models.length ? `${out.models.length} model(s) available` : out.message);
    } catch (error) { toast(error.message, "error"); }
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
      ? `Signed in${status.email ? ` as ${status.email}` : ""}${status.plan_ok === false ? " (plan does not include Codex access)" : ""}`
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

  async function load() {
    try {
      const [prov, set, status] = await Promise.all([api("/providers"), api("/settings"), api("/status")]);
      providers = prov.providers;
      provider.replaceChildren(...providers.map((p) => el("option", { value: p.id, text: p.label || p.id })));
      provider.value = prov.active;
      baseUrl.value = status.base_url || "";
      model.value = status.model || "";
      settings = set.settings;
      ctx.settings = settings;
      toolMode.value = settings["agent.tool_mode"] || "search";
      security.value = settings["security.mode"] || status.security_mode || "";
      syncProviderFields();
      if (!baseUrl.readOnly) baseUrl.value = status.base_url || "";
    } catch (error) { toast(error.message, "error"); }
  }
  load();
  return { refresh: load };
}
