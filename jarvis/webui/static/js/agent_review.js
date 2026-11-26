// The review card shown before an agent is installed (import and "Built with AI" share it).
// renderReview(body, card, hooks) fills `body`. hooks:
//   close(stagingToCancel?)  dismiss the dialog; the staged files are deleted by the caller
//   installed(detail)        called after a successful install
//   setStaging(token|null)   lets the caller remember what to clean up
//   toast(message, kind)
// A card from the builder also carries `mode`, `conflict` and `changes` ("What changed").
import { el } from "./dom.js";
import { aicon, agentTile } from "./agent_icons.js";
import { request } from "./agent_forms.js";

export function permChips(perms) {
  const chip = (ok, label) => el("span", { class: `perm ${ok ? "" : "off"}` }, aicon(ok ? "check" : "x", 12), label);
  return el("div", { class: "review-perms", "aria-label": "Permissions" },
    chip(perms.network, perms.network ? "Uses the internet" : "No internet"),
    chip(perms.commands, perms.commands ? "Can run commands" : "No commands"),
    chip(perms.files !== "none", { data: "Reads files; writes only its data", workspace: "Can edit your files", none: "No file access" }[perms.files] || perms.files));
}

// Unified diff text -> a <pre> with added/removed lines coloured.
export function diffBlock(text) {
  const pre = el("pre", { class: "diff", tabindex: "0", "aria-label": "File changes" });
  for (const line of String(text || "").split("\n")) {
    const kind = line.startsWith("+++") || line.startsWith("---") ? "meta" : line.startsWith("@@") ? "hunk"
      : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
    pre.append(el("span", { class: `diff-line ${kind}`, text: line || " " }));
  }
  return pre;
}

function whatChanged(changes) {
  const lines = changes.manifest || [];
  const files = changes.files || [];
  if (!lines.length && !files.length) return el("p", { class: "note", text: "No changes compared with the installed version." });
  const label = { added: "Added", changed: "Changed", removed: "Removed" };
  return el("section", { class: "review-changes", "aria-label": "What changed" },
    el("h4", { class: "aws-sub", text: "What changed" }),
    lines.length ? el("ul", { class: "change-list" }, lines.map((l) => el("li", { text: l }))) : null,
    files.map((f) => el("details", { class: "change-file" },
      el("summary", {}, el("span", { class: `badge ${f.status === "removed" ? "warn" : ""}`, text: label[f.status] || f.status }), el("code", { text: f.path })),
      f.diff ? diffBlock(f.diff) : el("p", { class: "note", text: "No text diff." }))));
}

export function renderReview(body, card, hooks) {
  const { toast } = hooks;
  hooks.setStaging?.(card.staging);
  const editing = !!card.changes && card.mode === "upgrade"; // a brand-new agent has nothing to compare with
  const verb = editing ? "apply" : "install";
  const list = (title, items) => items.length ? el("div", { class: "review-row" }, el("span", { class: "review-key", text: title }), el("span", { text: items.join(", ") })) : null;
  let mode = card.mode === "upgrade" && card.existing && !card.existing.builtin ? "upgrade"
    : card.existing ? (card.existing.builtin ? "copy" : "upgrade") : "new";
  function radio(value, label) {
    const input = el("input", { type: "radio", name: "import-mode", value });
    input.checked = mode === value;
    input.addEventListener("change", () => { mode = value; });
    return el("label", { class: "check" }, input, el("span", { text: label }));
  }
  const modes = card.existing && !editing ? el("fieldset", { class: "review-modes" },
    el("legend", { text: `You already have ${card.existing.name} v${card.existing.version}` }),
    ...(card.existing.builtin ? [] : [radio("upgrade", `Upgrade to v${card.version} (keeps its records and settings)`)]),
    radio("copy", "Install as a separate copy")) : null;
  const code = card.py_files.length ? el("div", { class: "review-code", role: "note" },
    el("div", { class: "agent-broken-head" }, aicon("code", 16), el("strong", { text: `Includes Python code (${card.py_files.length} file${card.py_files.length > 1 ? "s" : ""})` })),
    el("ul", {}, card.py_files.map((f) => el("li", {}, el("code", { text: f })))),
    el("p", { class: "note", text: editing
      ? "Trusted code runs on your computer with your permissions. If the code changed, you are asked to trust it again. You can also apply without the code."
      : "Trusted code runs on your computer with your permissions. Only trust agents from people you trust. You can install without the code and trust it later." })) : null;
  const install = async (trust) => {
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const detail = await request(`/agents/${encodeURIComponent(card.id)}/install`, { method: "POST", body: { staging: card.staging, mode, trust } });
      hooks.setStaging?.(null);
      hooks.installed(detail);
    } catch (error) {
      buttons.forEach((b) => { b.disabled = false; });
      toast(error.message, "error");
    }
  };
  const Verb = verb[0].toUpperCase() + verb.slice(1);
  const buttons = [el("button", { type: "button", text: "Cancel", onclick: () => hooks.close(card.staging) })];
  if (card.py_files.length) {
    buttons.push(el("button", { type: "button", text: `${Verb} without code`, onclick: () => install(false) }));
    buttons.push(el("button", { type: "button", class: "primary", onclick: () => install(true) }, aicon("shield", 15), `Trust & ${verb}`));
  } else {
    buttons.push(el("button", { type: "button", class: "primary", text: Verb, onclick: () => install(false) }));
  }
  const dataCounts = Object.entries(card.data || {}).map(([t, n]) => `${n} ${t}`);
  body.replaceChildren(el("div", { class: "review" },
    el("div", { class: "review-head" }, agentTile({ icon: card.icon }, 48),
      el("div", {}, el("strong", { class: "review-name", text: card.name }),
        el("div", { class: "note", text: `v${card.version}${card.author ? ` · by ${card.author}` : ""} · ${card.id}` }))),
    card.description ? el("p", { class: "review-desc", text: card.description }) : null,
    card.conflict ? el("div", { class: "aws-banner warn", role: "alert" }, aicon("alert", 16),
      el("span", { text: "The installed agent changed since you started this draft. Applying will overwrite those changes." })) : null,
    permChips(card.permissions),
    editing ? whatChanged(card.changes) : null,
    el("div", { class: "review-list" },
      list("Actions", card.actions), list("Records", card.records), list("Exports", card.exports),
      list("Settings", card.settings), list("Skills", card.skills), list("Data included", dataCounts)),
    code, modes,
    card.warnings.length ? el("details", { class: "review-warn" }, el("summary", { text: `${card.warnings.length} warning${card.warnings.length > 1 ? "s" : ""}` }),
      el("ul", {}, card.warnings.map((w) => el("li", { text: w })))) : null,
    card.skipped.length ? el("details", { class: "review-warn" }, el("summary", { text: `${card.skipped.length} file${card.skipped.length > 1 ? "s" : ""} skipped` }),
      el("ul", {}, card.skipped.map((w) => el("li", { text: w })))) : null,
    el("div", { class: "modal-actions" }, buttons)));
  buttons[buttons.length - 1].focus();
}
