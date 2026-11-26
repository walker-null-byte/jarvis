// Hash router. Routes: #/chat[/<sid>], #/agents[/<id>], #/agents/build/<draft_id>, #/discussions[/<id>], #/routines, #/tools.
// Anything else is the chat. Back and forward work because every route is a hash.

const VIEWS = ["agents", "discussions", "routines", "tools"];

export function parseHash(hash = window.location.hash) {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const [first, second, third] = parts;
  if (first === "agents" && second === "build" && third) return { name: "agents", params: { build: third }, view: true };
  if (VIEWS.includes(first)) return { name: first, params: second ? { id: second } : {}, view: true };
  return { name: "chat", params: { sid: first === "chat" && second ? second : "" }, view: false };
}

export function navigate(path) {
  const hash = `#/${path.replace(/^#?\/?/, "")}`;
  if (window.location.hash === hash) window.dispatchEvent(new HashChangeEvent("hashchange"));
  else window.location.hash = hash;
}

// Change the URL without adding a history entry or firing a route change.
export function replaceRoute(path) {
  const hash = `#/${path}`;
  if (window.location.hash !== hash) history.replaceState(null, "", hash);
}

export function startRouter(onRoute) {
  window.addEventListener("hashchange", () => onRoute(parseHash()));
  onRoute(parseHash());
}
