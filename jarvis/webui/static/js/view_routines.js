// Routines view: recurring and timed tasks. List, create/edit form, run history drawer.
import { registerView } from "./views.js";
import { confirmBox, el, iconBtn } from "./dom.js";
import { icon } from "./icons.js";
import { emptyState, pageHeader } from "./view_parts.js";
import { openRoutineForm, switchControl } from "./routine_form.js";
import { enableNotifications, markRoutinesSeen, notificationsSupported } from "./routine_notify.js";
import { renderMarkdown } from "./markdown.js";
import { ago, duration, plainText, relative, when } from "./routine_time.js";

const STATUS = {
  never: ["Never run", ""],
  running: ["Running", "running"],
  ok: ["OK", "ok"],
  error: ["Failed", "bad"],
  needs_approval: ["Needs approval", "warn"],
  cancelled: ["Cancelled", ""],
};
const TRIGGERS = { schedule: "Scheduled", manual: "Run now", catchup: "Catch-up after downtime", migrated: "Imported from old job" };

function statusChip(status, text) {
  const [label, tone] = STATUS[status] || [status, ""];
  return el("span", { class: `status-chip ${tone}` },
    status === "running" ? el("span", { class: "dot-spin", "aria-hidden": "true" }) : null,
    text || label);
}

// Starter routines shown in the empty state; each is a ready-made form input.
function examples(agentIds) {
  const list = [
    { name: "Morning briefing", blurb: "Every day at 08:00: a short summary of what matters today.",
      schedule: { kind: "cron", cron: "0 8 * * *" },
      target: { kind: "prompt", prompt: "Look through my notes and memory for anything due or planned today and give me a short morning briefing." } },
    { name: "Weekly disk-space check", blurb: "Mondays at 09:00: warn if a disk is getting full.",
      schedule: { kind: "cron", cron: "0 9 * * 1" },
      target: { kind: "prompt", prompt: "Check free disk space with df -h and tell me if any filesystem is more than 85% full. Keep the report short." } },
  ];
  if (agentIds.includes("lead-finder")) {
    list.push({ name: "Weekly lead hunt", blurb: "Mondays at 09:00: Lead Finder looks for new leads.",
      schedule: { kind: "cron", cron: "0 9 * * 1" },
      target: { kind: "agent_prompt", agent: "lead-finder", prompt: "Find 10 new high-quality leads that match my ideal lead description in the agent settings, and draft first messages for them." } });
  }
  return list;
}

registerView("routines", {
  mount(container, params, ctx) {
    const { api, bus, toast } = ctx;
    let routines = [];
    let notices = {};
    let schedulerRunning = true;
    let agents = null;       // loaded once, lazily
    let drawer = null;       // { routine, node, body }
    let reloadTimer = 0;
    let alive = true;

    const body = el("div", { class: "view-body routines" });
    const actions = el("div", { class: "view-actions" });
    const head = pageHeader("Routines", "Tasks Jarvis runs by itself, on a schedule or once. They only run while Jarvis is open (jarvis --web) or running as a service (jarvis --daemon).");
    head.querySelector(".view-actions")?.remove();
    head.classList.add("routines-head");
    head.append(actions);
    container.append(head, body);

    async function load() {
      try {
        const data = await api("/routines");
        routines = data.routines;
        notices = data.notices || {};
        schedulerRunning = data.scheduler_running;
      } catch (error) {
        body.replaceChildren(el("p", { class: "form-error", role: "alert", text: error.message }));
        return;
      }
      if (!alive) return;
      render();
      if (drawer) refreshDrawer();
    }

    function queueReload() {
      clearTimeout(reloadTimer);
      reloadTimer = setTimeout(load, 150);
    }

    async function loadAgents() {
      if (agents) return agents;
      try {
        const data = await api("/agents");
        agents = (Array.isArray(data) ? data : data.agents || []).filter((a) => a.id);
      } catch { agents = []; }
      return agents;
    }

    // ---- header actions ----
    function renderActions() {
      actions.replaceChildren();
      if (notificationsSupported() && Notification.permission === "default") {
        actions.append(el("button", { text: "Enable notifications", title: "Show a desktop notification when a routine finishes while this tab is in the background",
          onclick: async () => { await enableNotifications(); renderActions(); } }, icon("bell", 15)));
      }
      actions.append(el("button", { class: "primary", onclick: () => edit(null) }, icon("plus", 15), "New routine"));
    }

    // ---- list ----
    function render() {
      renderActions();
      const parts = [];
      if (notices.legacy_cron) parts.push(legacyNotice(notices.legacy_cron));
      if (!schedulerRunning) parts.push(el("div", { class: "notice warn", role: "status" }, icon("alert", 16),
        el("span", { text: "The scheduler is not running, so routines will not start by themselves." })));
      if (!routines.length) parts.push(emptyBlock());
      else parts.push(el("div", { class: "routine-list" }, ...routines.map(row)));
      body.replaceChildren(...parts);
      tick();
    }

    function legacyNotice(info) {
      return el("div", { class: "notice", role: "status" },
        icon("info", 16),
        el("div", { class: "notice-body" },
          el("strong", { text: "Old cron entries are still installed" }),
          el("p", { text: "Your crontab still runs these old jobs. Their routines were imported paused so nothing runs twice. Remove the lines, then switch the routines on." }),
          el("pre", { class: "raw-output", text: info.lines.join("\n") }),
          el("p", { class: "note", text: info.how_to_remove })),
        el("button", { text: "Dismiss", onclick: async () => {
          try { await api("/routines/notices/dismiss", { method: "POST" }); } catch (error) { toast(error.message, "error"); }
          load();
        } }));
    }

    function emptyBlock() {
      const ids = (agents || []).map((a) => a.id);
      const cards = examples(ids).map((example) => el("article", { class: "example-card" },
        el("h4", { text: example.name }),
        el("p", { class: "note", text: example.blurb }),
        el("button", { text: "Add", onclick: () => edit(null, example) }, icon("plus", 14))));
      return el("div", { class: "routines-empty" },
        emptyState("routines", "No routines yet",
          "A routine is something Jarvis does on its own: every morning, every hour, or once at a set time. You can also ask Jarvis in chat, for example “remind me every weekday at 9 to check my email”."),
        el("h4", { class: "examples-title", text: "Start from an example" }),
        el("div", { class: "example-grid" }, ...cards));
    }

    function row(routine) {
      const sw = switchControl(routine.enabled, `Enable ${routine.name}`, async (on) => {
        try { await api(`/routines/${encodeURIComponent(routine.id)}`, { method: "PATCH", body: { enabled: on } }); }
        catch (error) { toast(error.message, "error"); }
        load();
      });
      const next = el("span", { class: "next", "data-next": routine.enabled ? routine.next_run || "" : "" });
      const menuWrap = el("div", { class: "menu-wrap" });
      const more = iconBtn("more", `More actions for ${routine.name}`, () => openMenu(menuWrap, routine), "row-more");
      menuWrap.append(more);
      const running = routine.running;
      return el("article", { class: `routine-row${routine.enabled ? "" : " paused"}`, dataset: { id: routine.id } },
        el("div", { class: "row-switch" }, sw.node),
        el("div", { class: "row-main" },
          el("div", { class: "row-title" }, el("h3", { text: routine.name }),
            routine.target.kind !== "prompt" ? el("span", { class: "badge", text: routine.target.agent }) : null),
          el("div", { class: "row-when" }, icon("clock", 14), el("span", { text: routine.schedule_text }),
            el("span", { class: "dot", "aria-hidden": "true", text: "·" }),
            routine.enabled ? next : el("span", { class: "next", text: "paused" })),
          routine.last_summary ? el("p", { class: "row-summary", text: plainText(routine.last_summary) }) : null),
        el("div", { class: "row-status" },
          statusChip(routine.last_status),
          routine.last_run ? el("span", { class: "note", title: when(routine.last_run), text: ago(routine.last_run) }) : null),
        el("div", { class: "row-actions" },
          el("button", { class: "small", disabled: running, text: running ? "Running…" : "Run now", onclick: () => runNow(routine) },
            running ? null : icon("play", 13)),
          iconBtn("clock", `Run history for ${routine.name}`, () => openDrawer(routine), "row-history"),
          menuWrap));
    }

    function openMenu(wrap, routine) {
      document.querySelectorAll(".routine-menu").forEach((m) => m.remove());
      const close = () => { menu.remove(); document.removeEventListener("pointerdown", outside, true); document.removeEventListener("keydown", onKey, true); };
      const outside = (e) => { if (!menu.contains(e.target) && !wrap.contains(e.target)) close(); };
      const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); wrap.querySelector("button")?.focus(); } };
      const item = (label, name, fn, cls = "") => el("button", { type: "button", role: "menuitem", class: cls, onclick: () => { close(); fn(); } }, icon(name, 15), label);
      const menu = el("div", { class: "menu routine-menu", role: "menu" },
        item("Edit", "pencil", () => edit(routine)),
        item("Duplicate", "copy", () => duplicate(routine)),
        item("Run history", "clock", () => openDrawer(routine)),
        item("Delete", "trash", () => remove(routine), "danger"));
      wrap.append(menu);
      menu.querySelector("button").focus();
      document.addEventListener("pointerdown", outside, true);
      document.addEventListener("keydown", onKey, true);
    }

    // ---- actions ----
    async function edit(routine, example = null) {
      await loadAgents();
      const saved = await openRoutineForm({ api, routine, agents, example });
      if (saved) { toast(routine ? "Routine saved" : "Routine created", "info"); load(); }
    }

    async function duplicate(routine) {
      const { name, schedule, target, notify, on_missed } = routine;
      try {
        await api("/routines", { method: "POST", body: { name: `${name} (copy)`, schedule, target, notify, on_missed } });
        toast("Duplicated", "info");
      } catch (error) { toast(error.message, "error"); }
      load();
    }

    async function remove(routine) {
      if (!(await confirmBox("Delete routine", `Delete “${routine.name}” and its run history?`))) return;
      try { await api(`/routines/${encodeURIComponent(routine.id)}`, { method: "DELETE" }); } catch (error) { toast(error.message, "error"); }
      if (drawer?.routine.id === routine.id) closeDrawer();
      load();
    }

    async function runNow(routine) {
      try {
        await api(`/routines/${encodeURIComponent(routine.id)}/run`, { method: "POST" });
        toast(`Started “${routine.name}”`, "info");
      } catch (error) { toast(error.message, "error"); }
      load();
    }

    // ---- history drawer ----
    function openDrawer(routine) {
      closeDrawer();
      const bodyNode = el("div", { class: "drawer-body" });
      const node = el("aside", { class: "routine-drawer", role: "dialog", "aria-label": `Run history for ${routine.name}` },
        el("header", {}, el("div", {}, el("h3", { text: routine.name }), el("p", { class: "note", text: "Run history" })),
          iconBtn("x", "Close history", closeDrawer)),
        bodyNode);
      const scrim = el("div", { class: "drawer-scrim", onclick: closeDrawer });
      container.append(scrim, node);
      drawer = { routine, node, scrim, body: bodyNode };
      node.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
      refreshDrawer();
      node.querySelector("button")?.focus();
    }

    function closeDrawer() {
      if (!drawer) return;
      drawer.node.remove();
      drawer.scrim.remove();
      drawer = null;
    }

    async function refreshDrawer() {
      const current = drawer;
      if (!current) return;
      let data;
      try { data = await api(`/routines/${encodeURIComponent(current.routine.id)}/runs`); }
      catch (error) { current.body.replaceChildren(el("p", { class: "form-error", text: error.message })); return; }
      if (drawer !== current) return;
      current.body.replaceChildren(...(data.runs.length
        ? data.runs.map(runItem)
        : [el("p", { class: "note", text: "No runs yet. Press Run now to try it." })]));
    }

    function runItem(run) {
      return el("article", { class: "run-item" },
        el("div", { class: "run-head" }, statusChip(run.status),
          el("span", { class: "note", title: run.started, text: `${when(run.started)} · ${TRIGGERS[run.trigger] || run.trigger}` }),
          run.status === "running" ? null : el("span", { class: "note", text: duration(run.duration) })),
        run.summary ? el("div", { class: "run-summary" }, renderMarkdown(run.summary)) : null,
        run.error ? el("p", { class: "run-error", text: run.error }) : null,
        run.session_id ? el("button", { class: "small", onclick: () => ctx.navigate(`chat/${encodeURIComponent(run.session_id)}`) },
          "Open", icon("external", 13)) : null);
  }

    // ---- live updates ----
    function tick() {
      for (const node of body.querySelectorAll(".next[data-next]")) {
        const iso = node.dataset.next;
        node.textContent = iso ? relative(iso) : "no upcoming run";
        node.title = iso ? when(iso) : "";
      }
    }
    const timer = setInterval(tick, 30000);
    const off = bus.on((msg) => {
      if (msg.kind === "routines_changed" || msg.kind === "routine_run_started" || msg.kind === "routine_run_finished") queueReload();
    });

    markRoutinesSeen();
    loadAgents().then(() => { if (alive && !routines.length) render(); });
    load();

    this.cleanup = () => { alive = false; clearInterval(timer); clearTimeout(reloadTimer); off(); closeDrawer(); };
  },
  unmount() { this.cleanup?.(); this.cleanup = null; },
});
