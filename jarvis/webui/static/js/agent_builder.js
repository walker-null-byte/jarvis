// Agent Builder (#/agents/build/<draft_id>): a builder chat on the left, a live preview of the
// draft on the right (stacked on phones), Review & install in the header.
import { confirmBox, el, iconBtn } from "./dom.js";
import { icon } from "./icons.js";
import { aicon } from "./agent_icons.js";
import { request } from "./agent_forms.js";
import { BuilderChat } from "./agent_builder_chat.js";
import { BuilderPreview } from "./agent_builder_preview.js";
import { modelPicker } from "./agent_model_pick.js";
import { renderReview } from "./agent_review.js";

export function mountBuilder(container, draftId, ctx) {
  const { bus, toast, navigate } = ctx;
  const enc = encodeURIComponent(draftId);
  const root = el("div", { class: "aws bld", "aria-busy": "true" });
  container.append(root);
  container.classList.add("agents-wide");
  let alive = true;
  let draft = null;
  let chat = null;
  let busy = false;
  let refreshTimer = 0;
  let reviewing = false;
  const preview = new BuilderPreview({ draftId });
  const ui = {};

  const offBus = bus.on((msg) => {
    if (msg.kind === "agent_draft_changed" && msg.data?.draft_id === draftId) scheduleRefresh(250);
  });

  function scheduleRefresh(delay = 250) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, delay);
  }

  async function refresh() {
    if (!alive) return;
    try {
      draft = await request(`/agent-drafts/${enc}`);
    } catch (error) {
      if (error.status === 404) showMissing(); // discarded or installed elsewhere
      return;
    }
    if (!alive) return;
    if (!ui.header) { build(); return; }
    paintHeader();
    preview.update(draft, busy);
  }

  async function load() {
    try {
      draft = await request(`/agent-drafts/${enc}`);
    } catch (error) {
      if (error.status === 404) showMissing(); else showError(error);
      return;
    }
    if (!alive) return;
    busy = !!draft.busy;
    ctx.setTitle?.(draft.mode === "edit" ? `Editing ${draft.target_name || draft.name}` : `Building ${draft.name}`);
    build();
  }

  function missingView(title, text) {
    root.removeAttribute("aria-busy");
    ui.header = null;
    chat?.destroy();
    chat = null;
    root.replaceChildren(el("div", { class: "aws-missing" },
      aicon("sparkle", 28), el("h2", { text: title }), el("p", { class: "note", text }),
      el("a", { class: "button primary", href: "#/agents" }, icon("left", 15), "All agents")));
  }
  const showMissing = () => missingView("This draft is gone", "It was discarded, installed, or has expired. Start a new one from the Agents page.");
  const showError = (error) => missingView("Could not open this draft", error.message);

  // ---------- layout ----------

  function build() {
    chat?.destroy();
    root.removeAttribute("aria-busy");
    const edit = draft.mode === "edit";
    ui.title = el("h2");
    ui.sub = el("p", { class: "note" });
    ui.modelBtn = el("button", { type: "button", class: "bld-model", onclick: () => openModel() }, icon("sliders", 15), el("span", { class: "label" }));
    ui.discard = el("button", { type: "button", class: "ghost danger-text", onclick: () => discard() }, icon("trash", 15), el("span", { class: "label", text: "Discard" }));
    ui.review = el("button", { type: "button", class: "primary", onclick: () => stage() }, aicon("shield", 15), el("span", { text: edit ? "Review & apply" : "Review & install" }));
    ui.hint = el("p", { class: "sr-only", id: "bld-review-hint", "aria-live": "polite" });
    ui.review.setAttribute("aria-describedby", "bld-review-hint");
    ui.back = el("a", { class: "icon-btn aws-back", href: "#/agents" }, icon("left", 18));
    ui.header = el("header", { class: "aws-head bld-head" }, ui.back,
      el("div", { class: "aws-title" }, ui.title, ui.sub),
      el("div", { class: "aws-head-actions" }, ui.modelBtn, ui.discard, ui.review));

    chat = new BuilderChat({ draft, bus, toast, onSwitchModel: () => openModel() });
    chat.onBusy = (value) => {
      const was = busy;
      busy = value;
      paintHeader();
      preview.update(draft, busy);
      if (was && !value) refresh();
    };
    const left = el("div", { class: "bld-chat", "aria-label": "Builder chat" }, chat.node);
    const right = el("div", { class: "bld-preview", role: "region", "aria-label": "Live preview of the agent" }, preview.node);
    root.replaceChildren(ui.header, ui.hint, el("div", { class: "bld-cols" }, left, right));
    paintHeader();
    preview.update(draft, busy);
    chat.start();
  }

  function paintHeader() {
    const edit = draft.mode === "edit";
    const name = draft.name || "Untitled agent";
    ui.title.replaceChildren(el("span", { text: edit ? `Editing ${draft.target_name || name}` : `Building ${name}` }),
      el("span", { class: "badge", text: "Draft" }));
    ui.sub.textContent = edit ? "Changes stay in this draft until you apply them." : "Nothing is installed until you review it.";
    ui.back.href = edit && draft.target ? `#/agents/${encodeURIComponent(draft.target)}` : "#/agents";
    ui.back.setAttribute("aria-label", edit ? `Back to ${draft.target_name || "the agent"}` : "All agents");
    ui.back.title = ui.back.getAttribute("aria-label");
    const llm = draft.llm || {};
    const modelText = llm.model || llm.provider ? [llm.provider, llm.model].filter(Boolean).join(" · ") : "Main model";
    ui.modelBtn.lastChild.textContent = modelText;
    ui.modelBtn.title = `Builder model: ${modelText}`;
    ui.modelBtn.setAttribute("aria-label", `Builder model: ${modelText}. Change`);
    const ok = !!draft.check?.ok;
    ui.review.disabled = !ok || busy || reviewing;
    const todo = draft.check?.todo?.length || 0;
    const problems = draft.check?.problems?.length || 0;
    const left = [todo ? `${todo} to do` : "", problems ? `${problems} problem${problems === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ");
    ui.hint.textContent = busy ? "The builder is working." : ok ? "" : `${left} first.`;
    ui.review.title = busy ? "Wait for the builder to finish" : ok ? "" : "The builder must finish the todo list and fix the problems first";
  }

  // ---------- model ----------

  function openModel() {
    const opener = document.activeElement;
    const dialog = el("dialog", { class: "modal agent-dialog", "aria-label": "Builder model" });
    const close = () => { dialog.close(); dialog.remove(); opener?.focus?.(); };
    const picker = modelPicker({ initial: draft.llm || {}, idPrefix: "bld-llm" });
    const save = el("button", { type: "button", class: "primary", text: "Use this model" });
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const next = picker.get() || { provider: "", model: "" };
        draft = await request(`/agent-drafts/${enc}/llm`, { method: "POST", body: next });
        paintHeader();
        close();
        toast("Builder model changed. It applies from the next message.");
      } catch (error) { save.disabled = false; toast(error.message, "error"); }
    });
    dialog.append(el("div", { class: "agent-dialog-head" }, el("h3", { text: "Builder model" }), iconBtn("x", "Close", close, "", 18)),
      el("p", { class: "note", text: "Which model builds this agent. A stronger model makes fewer mistakes." }), picker.node,
      el("div", { class: "modal-actions" }, el("button", { type: "button", text: "Cancel", onclick: close }), save));
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
    document.body.append(dialog);
    dialog.showModal();
    picker.provider.button.focus();
  }

  // ---------- discard ----------

  async function discard() {
    const ok = await confirmBox("Discard this draft?", draft.mode === "edit"
      ? "Your changes are thrown away. The installed agent stays as it is."
      : "The agent being built is thrown away. Nothing has been installed.");
    if (!ok) return;
    try {
      await request(`/agent-drafts/${enc}`, { method: "DELETE" });
      toast("Draft discarded");
      navigate(draft.mode === "edit" && draft.target ? `agents/${encodeURIComponent(draft.target)}` : "agents");
    } catch (error) { toast(error.message, "error"); }
  }

  // ---------- review & install ----------

  async function stage() {
    if (ui.review.disabled) return;
    reviewing = true;
    paintHeader();
    const edit = draft.mode === "edit";
    const dialog = el("dialog", { class: "modal agent-dialog bld-review", "aria-label": edit ? "Review and apply changes" : "Review and install" });
    const body = el("div", { class: "agent-dialog-body" }, el("div", { class: "agent-loading", role: "status" }, el("span", { class: "spin on" }), el("span", { text: "Checking everything…" })));
    let staging = null;
    const finish = (token) => {
      if (token) request(`/agents/import/${encodeURIComponent(token)}`, { method: "DELETE" }).catch(() => {});
      dialog.close();
      dialog.remove();
      reviewing = false;
      if (alive) { paintHeader(); ui.review.focus(); }
    };
    dialog.append(el("div", { class: "agent-dialog-head" }, el("h3", { text: edit ? "Review & apply" : "Review & install" }),
      el("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: () => finish(staging) }, icon("x", 18))), body);
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); finish(staging); });
    document.body.append(dialog);
    dialog.showModal();
    try {
      const card = await request(`/agent-drafts/${enc}/stage`, { method: "POST" });
      renderReview(body, card, {
        toast,
        setStaging: (token) => { staging = token; },
        close: (token) => finish(token),
        installed: (detail) => {
          staging = null;
          dialog.close();
          dialog.remove();
          reviewing = false;
          toast(edit ? `${detail.name} updated` : `${detail.name} installed`);
          navigate(`agents/${encodeURIComponent(detail.id)}`);
        },
      });
    } catch (error) {
      const issues = error.data?.issues || [];
      body.replaceChildren(el("div", { class: "agent-import-error", role: "alert" },
        el("div", { class: "agent-broken-head" }, icon("alert", 16), el("strong", { text: issues.length ? "Not ready yet" : "Could not prepare the review" })),
        issues.length ? el("ul", {}, issues.map((l) => el("li", { text: l }))) : el("p", { text: error.message })),
        el("div", { class: "modal-actions" }, el("button", { type: "button", class: "primary", text: "Close", onclick: () => finish(null) })));
      if (error.status === 404) { finish(null); showMissing(); return; }
      refresh();
    }
  }

  load();
  return {
    unmount() {
      alive = false;
      offBus();
      chat?.destroy();
      clearTimeout(refreshTimer);
      container.classList.remove("agents-wide");
    },
  };
}
