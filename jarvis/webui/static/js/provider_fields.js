// Inputs for the non-secret settings some providers need (Cloudflare's Account ID).
import { el } from "./dom.js";

export function providerFields(onchange) {
  const node = el("div", { class: "provider-fields", hidden: true });
  let inputs = [];  // [field, input]
  return {
    node,
    show(provider) {
      inputs = (provider?.fields || []).map((f) => [f, el("input", {
        type: "text", value: f.value || "", autocomplete: "off", spellcheck: false,
        placeholder: f.env_set && !f.value ? `Using ${f.env} from your environment` : f.placeholder,
      })]);
      for (const [, input] of inputs) input.addEventListener("input", onchange);
      node.hidden = !inputs.length;
      node.replaceChildren(...inputs.map(([f, input]) => el("label", { class: "field" },
        el("span", { text: f.required ? `${f.label} (required)` : f.label }), input)));
    },
    // Every shown field, so that clearing a box also clears the saved value.
    values: () => Object.fromEntries(inputs.map(([f, input]) => [f.key, input.value.trim()])),
    // The first required field with nothing typed and no environment variable, or null.
    missing: () => inputs.find(([f, input]) => f.required && !input.value.trim() && !f.env_set)?.[0] || null,
    focus(key) { inputs.find(([f]) => f.key === key)?.[1].focus(); },
  };
}
