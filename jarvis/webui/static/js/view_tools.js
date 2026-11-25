// Tools view: MCP servers (connect external tool servers) above the tool and skill browser.
import { registerView } from "./views.js";
import { el, confirmBox } from "./dom.js";
import { icon } from "./icons.js";
import { renderTools } from "./panel_tools.js";
import { emptyState, pageHeader } from "./view_parts.js";

const EXAMPLE = `{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/you/Documents"]
    }
  }
}`;

const STATUS_TEXT = { connected: "Connected", connecting: "Connecting…", failed: "Failed", disabled: "Off", stopped: "Stopped" };

function statusLine(server) {
  const base = STATUS_TEXT[server.status] || server.status;
  return server.status === "connected" ? `${base} · ${server.tool_count} tool${server.tool_count === 1 ? "" : "s"}` : base;
}

function mcpSection(ctx) {
  const { api, bus, toast } = ctx;
  let servers = [];
  let loaded = false;
  let formOpen = false;
  const expanded = new Set();
  const seen = new Map(); // name -> { at, data } of the latest mcp_status event
  // A response snapshot can be older than a status event that arrived while it was in flight.
  const adopt = (list, since) => list.map((s) => (seen.get(s.name)?.at >= since ? seen.get(s.name).data : s));
  const list = el("div", { class: "mcp-list" });
  const intro = el("p", { class: "note", text: "MCP servers are small programs that give Jarvis extra tools, such as files, GitHub or a browser. Their tools stay out of the way until Jarvis searches for them." });
  const section = el("section", { class: "mcp", "aria-label": "MCP servers" });

  const field = el("textarea", { class: "mcp-json", rows: "8", spellcheck: "false", "aria-label": "Server JSON", placeholder: EXAMPLE });
  const formError = el("p", { class: "mcp-error", role: "alert", hidden: true });
  const addButton = el("button", { type: "button", class: "primary", text: "Add server" });
  const form = el("div", { class: "mcp-form" },
    el("label", { class: "form-field" }, el("span", { text: "Paste the server's JSON config" }), field),
    el("p", { class: "note" }, "Accepts the snippet from a server's README (", el("code", { text: '{"mcpServers": {…}}' }),
      "), or one server with a ", el("code", { text: '"name"' }), ". Use ", el("code", { text: "${NAME}" }),
      " in env or headers to read a saved key."),
    formError, el("div", { class: "card-actions" }, addButton,
      el("button", { type: "button", text: "Use example", onclick: () => { field.value = EXAMPLE; field.focus(); } })));

  async function submit() {
    formError.hidden = true;
    const text = field.value.trim();
    if (!text) { formError.textContent = "Paste a server config first."; formError.hidden = false; return; }
    addButton.disabled = true;
    const since = Date.now();
    try {
      const out = await api("/mcp/servers", { method: "POST", body: { json: text } });
      servers = adopt(out.servers, since);
      field.value = "";
      formOpen = false;
      toast(`Added ${out.added.join(", ")}`);
      render();
    } catch (error) {
      formError.textContent = error.message;
      formError.hidden = false;
    }
    addButton.disabled = false;
  }
  addButton.addEventListener("click", submit);

  async function act(promise, fallback) {
    const since = Date.now();
    try { servers = adopt((await promise).servers, since); render(); }
    catch (error) { toast(error.message || fallback, "error"); }
  }

  function card(server) {
    const busy = server.status === "connecting";
    const toggle = el("input", { type: "checkbox", checked: server.enabled, "aria-label": `Enable ${server.name}`,
      onchange: (e) => act(api(`/mcp/servers/${encodeURIComponent(server.name)}`, { method: "PATCH", body: { enabled: e.target.checked } })) });
    const trust = el("input", { type: "checkbox", checked: server.trust, "aria-label": `Trust ${server.name}`,
      onchange: (e) => act(api(`/mcp/servers/${encodeURIComponent(server.name)}`, { method: "PATCH", body: { trust: e.target.checked } })) });
    const tools = server.tools.length ? el("details", { class: "mcp-tools", open: expanded.has(server.name),
      ontoggle: (e) => { if (e.target.open) expanded.add(server.name); else expanded.delete(server.name); } },
      el("summary", { text: `${server.tool_count} tools` }),
      el("div", { class: "mcp-tool-names" }, server.tools.map((name) => el("code", { text: name })))) : null;
    return el("article", { class: `mcp-card ${server.status}`, "data-name": server.name },
      el("div", { class: "mcp-head" },
        el("span", { class: `mcp-dot ${server.status}`, "aria-hidden": "true" }),
        el("strong", { class: "mcp-name", text: server.name }),
        el("span", { class: "badge", text: server.transport === "stdio" ? "local" : "remote" }),
        el("span", { class: `mcp-status ${server.status}`, role: "status", text: statusLine(server) }),
        el("span", { class: "mcp-spacer" }),
        el("label", { class: "check mcp-switch", title: "Connect this server" }, toggle, el("span", { text: "On" })),
        el("label", { class: "check mcp-switch", title: "Never ask before this server's tools run (destructive tools still ask)" }, trust, el("span", { text: "Trust" })),
        el("button", { type: "button", class: "icon-btn", "aria-label": `Restart ${server.name}`, title: "Restart", disabled: busy || !server.enabled,
          onclick: () => act(api(`/mcp/servers/${encodeURIComponent(server.name)}/restart`, { method: "POST" })) }, icon("redo", 15)),
        el("button", { type: "button", class: "icon-btn danger", "aria-label": `Remove ${server.name}`, title: "Remove",
          onclick: async () => {
            if (await confirmBox("Remove server", `Remove “${server.name}” and forget its settings?`))
              act(api(`/mcp/servers/${encodeURIComponent(server.name)}`, { method: "DELETE" }));
          } }, icon("trash", 15))),
      el("div", { class: "mcp-target note", text: server.target }),
      server.error ? el("p", { class: "mcp-error", text: server.error }) : null,
      tools);
  }

  function render() {
    const empty = loaded && servers.length === 0;
    list.replaceChildren(...(empty
      ? [emptyState("server", "No MCP servers yet", "An MCP server is a small program that gives Jarvis new tools, like reading a folder, searching GitHub or driving a browser. Paste a server's JSON config below to connect your first one.")]
      : servers.map(card)));
    form.hidden = !(formOpen || empty);
    toggleForm.hidden = empty;
    toggleForm.textContent = formOpen ? "Close" : "Add server";
  }

  const toggleForm = el("button", { type: "button", text: "Add server", onclick: () => { formOpen = !formOpen; render(); if (formOpen) field.focus(); } });
  section.append(
    el("div", { class: "mcp-title" }, el("h4", { text: "MCP servers" }), toggleForm),
    intro, list, form);

  const off = bus.on((msg) => {
    if (msg.kind !== "mcp_status") return;
    seen.set(msg.data.name, { at: Date.now(), data: msg.data });
    if (!loaded) return;
    const at = servers.findIndex((s) => s.name === msg.data.name);
    if (at >= 0) servers[at] = msg.data; else servers.push(msg.data);
    render();
  });

  api("/mcp/servers").then((out) => { servers = out.servers; loaded = true; render(); })
    .catch((error) => { loaded = true; list.replaceChildren(el("p", { class: "mcp-error", text: error.message })); });
  render();
  return { node: section, off };
}

let cleanup = null;

registerView("tools", {
  mount(container, params, ctx) {
    const body = el("div", { class: "view-body" });
    const mcp = mcpSection(ctx);
    cleanup = mcp.off;
    const browser = el("div", { class: "panel" });
    body.append(mcp.node, el("h4", { text: "Tools and skills" }), browser);
    container.append(pageHeader("Tools", "Everything Jarvis can call, MCP servers you connect, and the skills it has learned."), body);
    renderTools(browser);
  },
  unmount() { cleanup?.(); cleanup = null; },
});
