// View registry and event bus. A view renders into a container inside #center in place
// of the chat: registerView(name, { mount(container, params, ctx), unmount() }).
// ctx = { api, bus, toast, navigate, setTitle }.

const views = new Map();
let current = null;

export function registerView(name, view) { views.set(name, view); }

// Tiny pub/sub: app.js feeds it every server event; views filter by session_id themselves.
export function createBus() {
  const listeners = new Set();
  return {
    on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    emit(msg) {
      for (const fn of [...listeners]) {
        try { fn(msg); } catch (error) { console.error("bus listener failed", error); }
      }
    },
  };
}

export function unmountView() {
  if (!current) return;
  try { current.view.unmount?.(); } catch (error) { console.error("unmount failed", error); }
  current.container.replaceChildren();
  current = null;
}

export function mountView(name, container, params, ctx) {
  unmountView();
  const view = views.get(name);
  if (!view) return false;
  current = { name, view, container };
  view.mount(container, params, ctx);
  return true;
}

export const currentViewName = () => current?.name || "";
