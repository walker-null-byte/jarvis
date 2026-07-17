"""Terminal UI: a Textual app with a chat pane, waveform strip, and push-to-talk."""

import json
import threading
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll, Horizontal
from textual.widgets import Input, Static
from textual.binding import Binding


class Waveform(Static):
    """Live audio amplitude rendered as unicode blocks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._amplitude = 0.0
        self._bars = "▁▂▃▄▅▆▇█"
        self._width = 60

    def set_amplitude(self, amp: float):
        self._amplitude = max(0.0, min(1.0, amp))
        self.refresh()

    def render(self):
        # Render a horizontal bar
        filled = int(self._amplitude * self._width)
        bar = "█" * filled + "░" * (self._width - filled)
        return Text(f"[{bar}] {self._amplitude:.0%}", style="cyan")


class JarvisUI(App):
    TITLE = "JARVIS"
    CSS = """
    #banner { height: 1; content-align: center middle; color: $accent; text-style: bold; }
    #waveform { height: 1; content-align: center middle; padding: 0 2; }
    #chat { padding: 0 2; height: 1fr; }
    #chat Static { margin-bottom: 1; }
    #input-row { height: 3; dock: bottom; padding: 0 2; }
    Input { width: 100%; }
    #status { height: 1; content-align: center middle; color: $text-muted; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+space", "push_to_talk", "Push-to-Talk", show=False),
    ]

    def __init__(self, jarvis):
        super().__init__()
        self.jarvis = jarvis
        self._waveform = Waveform(id="waveform")
        self._ptt_active = False
        self._listener = None
        self._ptt_thread = None

        # Wire TTS amplitude to waveform
        from jarvis.voice import get_voice
        voice = get_voice(self.jarvis.config)
        voice.set_amplitude_callback(self._on_amplitude)

    def compose(self) -> ComposeResult:
        yield Static("⏻  J A R V I S", id="banner")
        yield self._waveform
        yield VerticalScroll(id="chat")
        yield Horizontal(
            Input(placeholder="Talk to Jarvis…  (Ctrl+Space for voice, Ctrl+Q to quit)", id="input"),
            id="input-row",
        )
        yield Static("Ready", id="status")

    def on_mount(self):
        self._add(Text("Online. All systems nominal, sir.", style="dim italic"))
        self.query_one("#input").focus()

    def _on_amplitude(self, amp: float):
        self.call_from_thread(self._waveform.set_amplitude, amp)

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

    def _set_status(self, text: str):
        self.call_from_thread(self.query_one("#status").update, text)

    # ---------- push-to-talk ----------

    def action_push_to_talk(self):
        """Toggle push-to-talk recording."""
        if self._ptt_active:
            self._stop_ptt()
        else:
            self._start_ptt()

    def _start_ptt(self):
        from jarvis.listen import get_listener
        self._listener = get_listener(self.jarvis.config)
        self._listener.start()
        self._ptt_active = True
        self._set_status("🎙  Listening… (Ctrl+Space to stop)")
        self._add(Text("🎙  Listening…", style="bold green"))

    def _stop_ptt(self):
        if self._listener:
            text = self._listener.stop()
            self._ptt_active = False
            self._set_status("Ready")
            if text:
                self._add(Text(f"🎙  You: {text}", style="bold cyan"))
                # Process as normal message
                self.run_worker(lambda: self._turn(text), thread=True, exclusive=True)
            else:
                self._add(Text("🎙  (no speech detected)", style="dim yellow"))

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
            state["widget"] = None
            state["buffer"] = ""

        try:
            reply = self.jarvis.ask(text, on_text=on_text, on_tool=on_tool)
            if not state["buffer"] and reply:
                self.call_from_thread(self._add, Text(reply))
        except Exception as e:
            self.call_from_thread(self._add, Text(f"✗ {e}", style="bold red"))
        finally:
            self.call_from_thread(self._reenable_input)

    def _reenable_input(self):
        box = self.query_one("#input")
        box.disabled = False
        box.placeholder = "Talk to Jarvis…  (Ctrl+Space for voice, Ctrl+Q to quit)"
        box.focus()


def run_ui(jarvis):
    JarvisUI(jarvis).run()