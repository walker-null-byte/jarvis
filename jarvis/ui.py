"""Terminal UI: a Textual app with a chat pane and input bar.

The agent runs in a worker thread; tokens stream into the chat as they arrive.
The waveform strip joins in phase 5 (voice).
"""

import json

from rich.text import Text
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Input, Static


class JarvisUI(App):
    TITLE = "JARVIS"
    CSS = """
    #banner { height: 1; content-align: center middle; color: $accent; text-style: bold; }
    #chat { padding: 0 2; }
    #chat Static { margin-bottom: 1; }
    Input { dock: bottom; }
    """
    BINDINGS = [("ctrl+q", "quit", "Quit")]

    def __init__(self, jarvis):
        super().__init__()
        self.jarvis = jarvis

    def compose(self):
        yield Static("⏻  J A R V I S", id="banner")
        yield VerticalScroll(id="chat")
        yield Input(placeholder="Talk to Jarvis…  (Ctrl+Q to quit)")

    def on_mount(self):
        self._add(Text("Online. All systems nominal, sir.", style="dim italic"))
        self.query_one(Input).focus()

    # ---------- chat helpers (UI thread only) ----------

    def _add(self, renderable):
        chat = self.query_one("#chat")
        widget = Static(renderable)
        chat.mount(widget)
        chat.scroll_end(animate=False)
        return widget

    def _update(self, widget, renderable):
        widget.update(renderable)
        self.query_one("#chat").scroll_end(animate=False)

    # ---------- turn handling ----------

    def on_input_submitted(self, event):
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        event.input.disabled = True
        event.input.placeholder = "thinking…"
        self._add(Text(f"❯ {text}", style="bold cyan"))
        self.run_worker(lambda: self._turn(text), thread=True, exclusive=True)

    def _turn(self, text):
        """Runs in a worker thread — all UI access goes via call_from_thread."""
        state = {"widget": None, "buffer": ""}

        def on_text(token):
            state["buffer"] += token
            if state["widget"] is None:
                state["widget"] = self.call_from_thread(self._add, Text(state["buffer"]))
            else:
                self.call_from_thread(self._update, state["widget"], Text(state["buffer"]))

        def on_tool(name, args):
            note = f"⚙ {name} {json.dumps(args, default=str)[:120]}"
            self.call_from_thread(self._add, Text(note, style="dim yellow"))
            state["widget"] = None  # text after a tool call starts a fresh bubble
            state["buffer"] = ""

        try:
            reply = self.jarvis.ask(text, on_text=on_text, on_tool=on_tool)
            if not state["buffer"] and reply:  # non-streaming fallback
                self.call_from_thread(self._add, Text(reply))
        except Exception as e:
            self.call_from_thread(self._add, Text(f"✗ {e}", style="bold red"))
        finally:
            self.call_from_thread(self._reenable_input)

    def _reenable_input(self):
        box = self.query_one(Input)
        box.disabled = False
        box.placeholder = "Talk to Jarvis…  (Ctrl+Q to quit)"
        box.focus()


def run_ui(jarvis):
    JarvisUI(jarvis).run()
