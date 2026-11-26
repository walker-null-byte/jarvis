// Provider + model choice for one agent or builder: "same as the main chat" or an explicit pair.
// modelPicker({ initial }) -> { node, get() -> {provider, model} | null }
import { api } from "./api.js";
import { Select } from "./combo.js";
import { el } from "./dom.js";
import { providerOptions } from "./switcher.js";

export function modelPicker({ initial = {}, idPrefix = "mp" } = {}) {
  const provider = new Select({ label: "Provider", options: [{ value: "", label: "Same as the main chat" }], value: initial.provider || "" });
  const model = el("input", { type: "text", id: `${idPrefix}-model`, placeholder: "Provider's default model", autocomplete: "off", spellcheck: "false", "aria-label": "Model" });
  model.value = initial.model || "";
  api("/providers").then(({ providers }) => {
    provider.setOptions([{ value: "", label: "Same as the main chat" }, ...providerOptions(providers)]);
    provider.setValue(initial.provider || "");
  }).catch(() => {});
  const help = el("p", { class: "help", text: "The builder works best with a capable model. Leave on the main chat model unless you want a stronger one for this." });
  const node = el("div", { class: "agent-form mp" },
    el("div", { class: "form-field" }, el("label", { text: "Provider" }), provider.node),
    el("div", { class: "form-field" }, el("label", { for: `${idPrefix}-model`, text: "Model (optional)" }), model), help);
  return {
    node, provider,
    get: () => (provider.value || model.value.trim() ? { provider: provider.value, model: model.value.trim() } : null),
  };
}
