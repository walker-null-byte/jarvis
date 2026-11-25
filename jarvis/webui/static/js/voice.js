// Browser-side voice: the server synthesizes WAV, the browser plays it through
// WebAudio (so the orb can follow the real signal). Also owns the model-download
// helpers shared by the inline prompt, the Voice tab and onboarding.
import { api } from "./api.js";
import { el, toast } from "./dom.js";

// ---------- downloads (progress arrives as "voice_download" events) ----------

const listeners = new Set();
export const downloads = new Map(); // item_id -> last progress event data

export function onDownload(fn) { listeners.add(fn); return () => listeners.delete(fn); }

export function handleDownloadEvent(data) {
  if (!data || !data.item_id) return;
  downloads.set(data.item_id, data);
  for (const fn of [...listeners]) fn(data);
}

// Start a download and resolve when the server reports done (reject on error).
export function downloadItem(itemId, onProgress) {
  return new Promise((resolve, reject) => {
    let off = () => {};
    off = onDownload((d) => {
      if (d.item_id !== itemId) return;
      onProgress?.(d);
      if (d.status === "done") { off(); resolve(d); }
      else if (d.status === "error") { off(); reject(new Error(d.message || "Download failed")); }
    });
    api("/voice/download", { method: "POST", body: { item_id: itemId } }).catch((error) => {
      off();
      // 409 means this item is already downloading: keep waiting for its events.
      if (error.status === 409) { off = onDownload((d) => d.item_id === itemId && d.status === "done" && (off(), resolve(d))); }
      else reject(error);
    });
  });
}

export function progressText(d) {
  if (!d) return "";
  if (d.status === "installing") return d.message || "Installing…";
  if (d.status === "error") return d.message || "Failed";
  if (d.progress === null || d.progress === undefined) return d.message || "Downloading…";
  return `${d.message || "Downloading"} ${Math.round(d.progress * 100)}%`;
}

// A <progress> bar + status line that follows one item's events. Returns {node, stop}.
export function progressView(itemId) {
  const bar = el("progress", { max: "1", "aria-label": "Download progress" });
  const text = el("span", { class: "note", role: "status" });
  const node = el("div", { class: "dl-progress" }, bar, text);
  const draw = (d) => {
    if (d.progress === null || d.progress === undefined) bar.removeAttribute("value");
    else bar.value = d.progress;
    text.textContent = progressText(d);
  };
  const off = onDownload((d) => d.item_id === itemId && draw(d));
  return { node, stop: off, draw };
}

// ---------- the player ----------

async function fetchSpeech(text) {
  let response;
  try {
    response = await fetch("/api/voice/tts", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
    });
  } catch { throw new Error("Cannot reach the Jarvis server"); }
  if (response.status === 424) return { needs: await response.json().catch(() => ({})) };
  if (!response.ok) {
    let message = `Speech failed (${response.status})`;
    try { message = (await response.json()).error || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  return { wav: await response.arrayBuffer() };
}

export class Voice {
  // hooks: {onSpeaking(bool), onLevel(0..1)}. `owner` objects get setState("idle"|"loading"|"playing").
  constructor(hooks) {
    this.hooks = hooks;
    this.audio = null;
    this.source = null;
    this.owner = null;
    this.token = 0;
    this.frame = 0;
  }

  // Browsers only start audio after a user gesture: call this from click handlers.
  unlock() {
    try {
      if (!this.audio) {
        const Context = window.AudioContext || window.webkitAudioContext;
        if (!Context) return;
        this.audio = new Context();
        this.analyser = this.audio.createAnalyser();
        this.analyser.fftSize = 512;
        this.analyser.connect(this.audio.destination);
      }
      if (this.audio.state === "suspended") this.audio.resume();
    } catch { /* audio unavailable */ }
  }

  get playing() { return !!this.owner; }

  stop() {
    this.token++;
    cancelAnimationFrame(this.frame);
    if (this.source) {
      this.source.onended = null;
      try { this.source.stop(); } catch { /* already stopped */ }
      this.source.disconnect();
      this.source = null;
    }
    if (this.owner) this.owner.setState("idle");
    this.owner = null;
    this.hooks.onSpeaking(false);
    this.hooks.onLevel(0);
  }

  // Returns "played" | "needs_download" (with info) | "error".
  async speak(text, owner, host) {
    this.unlock();
    const mine = this.owner === owner && owner;
    this.stop();
    if (mine) return { status: "stopped" }; // second click on the same button = stop
    const token = ++this.token;
    this.owner = owner;
    owner.setState("loading");
    this.hooks.onSpeaking(true);
    try {
      const result = await fetchSpeech(text.slice(0, 6000));
      if (token !== this.token) return { status: "stopped" };
      if (result.needs) {
        this.stop();
        return { status: "needs_download", info: result.needs };
      }
      if (!this.audio) throw new Error("This browser cannot play audio");
      const buffer = await this.audio.decodeAudioData(result.wav);
      if (token !== this.token) return { status: "stopped" };
      const source = this.audio.createBufferSource();
      source.buffer = buffer;
      source.connect(this.analyser);
      source.onended = () => { if (token === this.token) this.stop(); };
      this.source = source;
      owner.setState("playing");
      source.start();
      this.meter(token);
      return { status: "played" };
    } catch (error) {
      if (token === this.token) this.stop();
      toast(error.message || "Could not play speech", "error");
      return { status: "error", error };
    }
  }

  meter(token) {
    const data = new Uint8Array(this.analyser.fftSize);
    const loop = () => {
      if (token !== this.token) return;
      this.analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) { const x = (v - 128) / 128; sum += x * x; }
      this.hooks.onLevel(Math.min(1, Math.sqrt(sum / data.length) * 4));
      this.frame = requestAnimationFrame(loop);
    };
    loop();
  }
}

// ---------- inline "voice model not downloaded" prompt ----------

// Shows a prompt inside `host`; on finish calls retry() (which plays the audio).
export function showDownloadPrompt(host, info, retry) {
  host.querySelector(".voice-prompt")?.remove();
  const itemId = info.needs_download;
  const label = info.label || "voice model";
  const size = info.size_mb ? ` (${info.size_mb} MB)` : "";
  const button = el("button", { class: "primary small", type: "button", text: `Download ${label}${size}` });
  const close = el("button", { class: "ghost small", type: "button", text: "Dismiss" });
  const message = el("span", { text: "Voice model not downloaded \u2014" });
  const progress = progressView(itemId);
  progress.node.hidden = true;
  const node = el("div", { class: "voice-prompt", role: "group", "aria-label": "Voice model download" },
    el("div", { class: "voice-prompt-row" }, message, button, close), progress.node);
  const remove = () => { progress.stop(); node.remove(); };
  close.addEventListener("click", remove);
  button.addEventListener("click", async () => {
    button.disabled = true;
    progress.node.hidden = false;
    message.textContent = "Downloading…";
    try {
      await downloadItem(itemId, progress.draw);
      remove();
      retry();
    } catch (error) {
      button.disabled = false;
      message.textContent = `Download failed: ${error.message}`;
    }
  });
  host.append(node);
  node.scrollIntoView({ block: "nearest" });
  button.focus({ preventScroll: true });
  return node;
}
