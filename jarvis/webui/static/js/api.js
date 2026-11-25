// Fetch wrapper and the live event stream.

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function api(path, { method = "GET", body } = {}) {
  const options = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(`/api${path}`, options); }
  catch { throw new ApiError("Cannot reach the Jarvis server", 0); }
  let data = null;
  try { data = await response.json(); } catch { /* non-JSON body */ }
  if (!response.ok) {
    throw new ApiError((data && data.error) || `Request failed (${response.status})`, response.status);
  }
  return data;
}

// EventSource reconnects by itself; every (re)connect starts with a fresh snapshot.
export class EventStream {
  constructor(onMessage, onConnection) {
    this.onMessage = onMessage;
    this.onConnection = onConnection;
  }

  start() {
    this.source = new EventSource("/api/events");
    this.source.onopen = () => this.onConnection(true);
    this.source.onerror = () => this.onConnection(false);
    this.source.onmessage = (event) => {
      try { this.onMessage(JSON.parse(event.data)); }
      catch (error) { console.error("bad event", error); }
    };
  }
}
