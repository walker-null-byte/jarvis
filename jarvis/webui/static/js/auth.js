// "Sign in with ChatGPT": start the login, open the sign-in page in a new tab,
// poll until the server reports signed_in. Shared by onboarding and the switcher.
import { api } from "./api.js";
import { el } from "./dom.js";

// Returns {auth_url, wait()}; wait() resolves with the signed-in status or rejects on timeout/cancel.
export async function startChatgptSignIn() {
  // Open the tab synchronously (inside the click) so popup blockers allow it.
  const tab = window.open("", "_blank");
  if (tab) tab.opener = null;
  let login;
  try { login = await api("/auth/chatgpt/login", { method: "POST" }); }
  catch (error) { tab?.close(); throw error; }
  if (tab) tab.location.href = login.auth_url;
  let cancelled = false;
  return {
    auth_url: login.auth_url,
    cancel() { cancelled = true; },
    async wait(timeoutMs = 5 * 60 * 1000) {
      const end = Date.now() + timeoutMs;
      while (!cancelled && Date.now() < end) {
        await new Promise((r) => setTimeout(r, 2000));
        let status;
        try { status = await api("/auth/chatgpt"); } catch { continue; }
        if (status.signed_in && status.plan_ok !== false) return status;
        // Finished without usable access: surface the server's reason now.
        if (!status.pending && (status.error || status.signed_in)) {
          throw new Error(status.error || "Signed in, but this ChatGPT plan can't be used by Jarvis.");
        }
      }
      throw new Error(cancelled ? "Sign-in cancelled" : "Sign-in timed out. Try again.");
    },
  };
}

export function signInLink(url) {
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: "open the sign-in page" });
}

// Full flow with UI callbacks; returns the signed-in status.
export async function signInFlow({ onLink, onWaiting }) {
  const flow = await startChatgptSignIn();
  onLink?.(flow);
  onWaiting?.(flow);
  return flow.wait();
}
