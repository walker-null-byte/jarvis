"""Desktop-first Textual interface for Jarvis.

The UI is intentionally driven by the agent event bus.  That keeps the chat
surface, activity inspector, and future web UI on the same runtime contract.
"""

import json
import math
import os
import random
from collections import deque

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .commands import (
    PROVIDER_PRESETS,
    _split_credentials,
    _test_connection,
    dialog_for_command,
    handle_slash_command,
    slash_command_suggestions,
)
from .config import remembered_model
from .events import Event


class Waveform(Static):
    """A damped point-cloud sphere inspired by the standalone visualizer."""

    amplitude = reactive(0.0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._state = "Ready"
        self._phase = 0.0
        self._level = 0.04
        self._target = 0.04
        self._samples = deque([0.04] * 64, maxlen=64)
        self._outer_points = self._sphere_points(260, 1.15, seed=11)
        self._inner_points = self._sphere_points(96, 0.72, seed=17)
        self._ambient_points = self._ambient_points_for_sphere(42, seed=23)

    @staticmethod
    def _sphere_points(count, radius, seed):
        rng = random.Random(seed)
        points = []
        golden = math.pi * (3.0 - math.sqrt(5.0))
        for index in range(count):
            y = 1.0 - (index + 0.5) * 2.0 / count
            ring = math.sqrt(max(0.0, 1.0 - y * y))
            angle = index * golden + rng.uniform(-0.04, 0.04)
            points.append((
                radius * ring * math.cos(angle),
                radius * y,
                radius * ring * math.sin(angle),
            ))
        return points

    @staticmethod
    def _ambient_points_for_sphere(count, seed):
        rng = random.Random(seed)
        points = []
        for _ in range(count):
            theta = rng.random() * math.pi * 2
            phi = math.acos(2.0 * rng.random() - 1.0)
            radius = 1.75 + rng.random() * 0.9
            points.append((
                radius * math.sin(phi) * math.cos(theta),
                radius * math.sin(phi) * math.sin(theta),
                radius * math.cos(phi),
            ))
        return points

    def set_amplitude(self, amp: float):
        value = max(0.0, min(1.0, float(amp)))
        self.amplitude = value
        self._target = value
        self._samples.append(value)
        self.refresh()

    def set_state(self, state: str):
        self._state = state
        self.refresh()

    def tick(self):
        self._phase = (self._phase + 0.14) % (math.pi * 2)
        self._level += (self._target - self._level) * 0.34
        breathing = 0.03 * (math.sin(self._phase) + 1.0)
        self._samples.append(max(0.02, min(1.0, self._level + breathing)))
        self.refresh()

    def render(self):
        """Render outer shell, inner core, and floating particles with depth."""
        width, height = 35, 17
        center_x, center_y = (width - 1) / 2, (height - 1) / 2
        grid = [[(" ", "default") for _ in range(width)] for _ in range(height)]
        depth = [[-100.0 for _ in range(width)] for _ in range(height)]
        samples = list(self._samples)
        level = max(self._level, self._target)
        rotation_y = self._phase * 0.42 + level * 0.35
        rotation_x = math.sin(self._phase * 0.31) * 0.22

        def project(point, scale=1.0):
            x, y, z = point
            cos_y, sin_y = math.cos(rotation_y), math.sin(rotation_y)
            x1 = x * cos_y + z * sin_y
            z1 = -x * sin_y + z * cos_y
            cos_x, sin_x = math.cos(rotation_x), math.sin(rotation_x)
            y1 = y * cos_x - z1 * sin_x
            z2 = y * sin_x + z1 * cos_x
            perspective = 1.0 / max(1.35, 3.25 - z2)
            screen_x = round(center_x + x1 * 12.5 * scale * perspective)
            screen_y = round(center_y + y1 * 8.6 * scale * perspective)
            return screen_x, screen_y, z2

        def plot(screen_x, screen_y, point_depth, char, style):
            if not (0 <= screen_x < width and 0 <= screen_y < height):
                return
            if point_depth >= depth[screen_y][screen_x]:
                depth[screen_y][screen_x] = point_depth
                grid[screen_y][screen_x] = (char, style)

        # The ambient field rotates slowly, as in the reference visualizer.
        for index, point in enumerate(self._ambient_points):
            x, y, z = point
            drift = self._phase * 0.08
            ambient = (x * math.cos(drift) - z * math.sin(drift), y, x * math.sin(drift) + z * math.cos(drift))
            px, py, pz = project(ambient, scale=0.8)
            plot(px, py, pz - 1.0, "·", "dim cyan")

        # Deform the outer shell according to the recent TTS amplitude history.
        for index, point in enumerate(self._outer_points):
            audio_energy = samples[(index * 7 + int(self._phase * 5)) % len(samples)]
            ripple = math.sin(self._phase * 2.3 + index * 0.37) * 0.045
            displacement = 1.0 + (max(level, audio_energy) * 0.22) + ripple
            px, py, pz = project(point, scale=displacement)
            style = "bright_cyan" if pz > 0.15 else "cyan"
            visual_energy = max(audio_energy, level * 0.45)
            char = "✦" if visual_energy > 0.68 else ("•" if visual_energy > 0.25 else "·")
            plot(px, py, pz, char, style)

        # The inner white point sphere provides the glowing core/depth cue.
        inner_pulse = 1.0 + level * 0.13 * math.sin(self._phase * 5.5)
        for index, point in enumerate(self._inner_points):
            px, py, pz = project(point, scale=inner_pulse)
            char = "•" if pz > 0 else "·"
            plot(px, py, pz + 0.2, char, "bright_white" if pz > 0.1 else "white")

        plot(round(center_x), round(center_y), 10.0, "◉", "bold bright_white")

        output = Text(justify="center")
        for row_index, row in enumerate(grid):
            for char, style in row:
                output.append(char, style=style)
            if row_index < len(grid) - 1:
                output.append("\n")
        color = {
            "Listening": "green",
            "Thinking": "yellow",
            "Speaking": "cyan",
            "Working": "magenta",
            "Error": "red",
        }.get(self._state, "bright_cyan")
        output.append(f"\n{self._state}", style=f"bold {color}")
        return output


class MessageCard(Static):
    def __init__(self, role, content="", *args, **kwargs):
        self.role = role
        self._message_content = content
        self._is_streaming = False
        super().__init__(self._renderable(), *args, **kwargs)

    def set_content(self, content, streaming=False):
        self._message_content = content
        self._is_streaming = streaming
        self.update(self._renderable())

    def finalize(self, content):
        self._message_content = content
        self._is_streaming = False
        self.update(self._renderable())

    def _renderable(self):
        if self.role == "assistant":
            # During streaming render as plain Text to avoid Markdown re-parse flicker
            if self._is_streaming:
                return Text(self._message_content or "▌")
            return Markdown(self._message_content or "▌")
        if self.role == "error":
            return Text(self._message_content, style="bold red")
        if self.role == "system":
            return Text(self._message_content, style="bright_cyan")
        return Text(self._message_content)


class WelcomeCard(Static):
    """A deliberate empty-state anchor for a new conversation."""

    def __init__(self, *args, **kwargs):
        content = Text(justify="center")
        content.append("J A R V I S\n", style="bold bright_cyan")
        content.append("Ready when you are\n\n", style="bold white")
        content.append("Ask a question, start an analysis, or say hello.\n", style="cyan")
        content.append("Enter ", style="dim white")
        content.append("send", style="bold white")
        content.append("  ·  Ctrl+Space voice  ·  Esc stop", style="dim white")
        super().__init__(content, *args, **kwargs)


class CommandInput(Input):
    """Composer input with keyboard controls for the slash-command palette."""

    BINDINGS = [
        *Input.BINDINGS,
        Binding("up", "command_up", "", show=False),
        Binding("down", "command_down", "", show=False),
        Binding("tab", "command_accept", "", show=False),
        Binding("escape", "command_dismiss", "", show=False),
    ]

    def action_command_up(self):
        self.app._move_command_selection(-1)

    def action_command_down(self):
        self.app._move_command_selection(1)

    def action_command_accept(self):
        self.app._accept_command_suggestion()

    def action_command_dismiss(self):
        if not self.app._dismiss_command_palette():
            self.app.action_cancel_turn()

    async def action_submit(self):
        # A complete command (for example, /status) should submit directly.
        # A partial command uses Enter as an intuitive "choose" action; a
        # second Enter then executes the inserted command.
        if (
            self.app._command_palette_visible
            and not self.app._command_palette_should_submit()
            and self.app._accept_command_suggestion(open_dialog=True)
        ):
            return
        await super().action_submit()


class ChatView(VerticalScroll):
    """Scrollable message stack with one mutable assistant card per turn."""

    def add_message(self, role, content=""):
        for welcome in self.query("#welcome"):
            welcome.remove()
        card = MessageCard(role, content, classes=f"message {role}")
        self.mount(card)
        return card

    def show_welcome(self):
        if not self.query("#welcome"):
            self.mount(WelcomeCard(id="welcome"))


class ArtifactCard(Static):
    def __init__(self, artifact, *args, **kwargs):
        self.artifact = artifact
        super().__init__(self._renderable(), *args, **kwargs)

    def _renderable(self):
        artifact = self.artifact
        kind = artifact.get("type", "artifact").upper()
        title = artifact.get("title", "Jarvis artifact")
        lines = [f"◆ {kind}  {title}"]
        if artifact.get("path"):
            lines.append(f"Path: {artifact['path']}")
        if artifact.get("content"):
            lines.append(artifact["content"][:800])
        if artifact.get("data") is not None:
            data = json.dumps(artifact["data"], ensure_ascii=False, indent=2, default=str)
            lines.append(data[:800])
        return Text("\n".join(lines), style="cyan")


class ProviderScreen(ModalScreen):
    """The single place to manage the active endpoint.

    Provider preset (or custom URL), API key (env var name or literal),
    connection test, and a model dropdown populated from the endpoint's own
    model list —     everything about provider + model lives here.
    """

    BINDINGS = [Binding("escape", "form_cancel", "Cancel", show=False)]
    AUTO_FOCUS = ""  # no auto-focus on mount: it would scroll the box

    def __init__(self, jarvis, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._jarvis = jarvis
        self._working_url = None
        self._last_test_worker = None
        self._test_seq = 0
        self._test_timer = None
        self._test_debounce = 0.7

    def compose(self) -> ComposeResult:
        llm = self._jarvis.config["llm"]
        provider_options = [
            (f"{name} — {preset['description']}", name)
            for name, preset in PROVIDER_PRESETS.items()
        ]
        provider_options.append(("custom — any OpenAI-compatible endpoint", "custom"))
        yield Vertical(
            Static("Provider manager", id="form-title"),
            Static(
                "Pick the endpoint, paste the API key, test the connection, then pick a model.",
                id="form-desc",
            ),
            Horizontal(
                Vertical(
                    Static("Provider", classes="field-label"),
                    OptionList(
                        *(Option(label, id=value) for label, value in provider_options),
                        id="field-provider",
                        classes="field-widget",
                    ),
                ),
                Vertical(
                    Static("Model", classes="field-label"),
                    OptionList(
                        Option(llm.get("model") or "(none)", id=llm.get("model") or ""),
                        id="field-model",
                        classes="field-widget",
                    ),
                ),
                id="fields-top",
            ),
            Horizontal(
                Vertical(
                    Static("Base URL", classes="field-label"),
                    Input(
                        value=llm.get("base_url") or "",
                        placeholder="http://host:port/v1",
                        id="field-url",
                        classes="field-widget",
                    ),
                ),
                Vertical(
                    Static("API key or env var name", classes="field-label"),
                    Input(
                        value=llm.get("api_key") or llm.get("api_key_env") or "",
                        placeholder="e.g. JARVIS_API_KEY or oc-…",
                        id="field-key",
                        classes="field-widget",
                    ),
                ),
                id="fields-creds",
            ),
            Static("", id="field-status"),
            Static("", id="form-error"),
            Horizontal(
                Button("Test", id="form-test"),
                Button("Cancel", id="form-cancel"),
                Button("Apply", id="form-submit", variant="success"),
                id="form-actions",
            ),
            id="form-box",
        )

    def on_mount(self):
        llm = self._jarvis.config["llm"]
        provider = llm.get("provider", "custom")
        panel = self.query_one("#field-provider", OptionList)
        for index, option in enumerate(panel.options):
            if option.id == provider:
                panel.highlighted = index
                break
        self.query_one("#field-model", OptionList).highlighted = 0
        self.screen.set_focus(
            self.query_one(".field-widget"), scroll_visible=False
        )
        self._run_test()

    def on_option_list_option_selected(self, event):
        if event.option_list.id != "field-provider":
            return
        preset = PROVIDER_PRESETS.get(event.option.id)
        if preset:
            self.query_one("#field-url", Input).value = preset["base_url"]
        self._run_test()

    def on_input_changed(self, event):
        # Typing an endpoint or API key re-tests automatically (debounced) so
        # the model dropdown fills in on its own once both are correct.
        if event.input.id in ("field-url", "field-key"):
            self._schedule_test()

    def on_button_pressed(self, event):
        if event.button.id == "form-test":
            self._run_test()
        elif event.button.id == "form-cancel":
            self.action_form_cancel()
        elif event.button.id == "form-submit":
            self._apply()

    def action_form_cancel(self):
        self._invalidate_test()
        self.dismiss(None)

    def _invalidate_test(self):
        self._test_seq += 1
        if self._test_timer is not None:
            self._test_timer.stop()
            self._test_timer = None

    def _schedule_test(self):
        if self._test_timer is not None:
            self._test_timer.stop()
        self._test_timer = self.set_timer(self._test_debounce, self._run_test)

    def _run_test(self):
        if self._test_timer is not None:
            self._test_timer.stop()
            self._test_timer = None
        url = self.query_one("#field-url", Input).value.strip()
        if not url:
            url = self._jarvis.config["llm"].get("base_url", "")
        key = self.query_one("#field-key", Input).value.strip()
        env_name, literal = _split_credentials(key) if key else (None, None)
        provider_panel = self.query_one("#field-provider", OptionList)
        if provider_panel.highlighted is not None:
            provider = provider_panel.get_option_at_index(provider_panel.highlighted).id
        else:
            provider = self._jarvis.config["llm"].get("provider", "custom")
        self._test_seq += 1
        self._test_draft = (url or None, env_name, literal, provider, self._test_seq)
        self.query_one("#field-status", Static).update("Testing…")
        self.query_one("#form-test", Button).disabled = True
        self._last_test_worker = self.run_worker(
            self._test_worker, thread=True, exclusive=False
        )

    def _test_worker(self):
        url, env_name, literal, provider, seq = self._test_draft
        ok, message, ids, working_url = _test_connection(
            self._jarvis,
            base_url=url,
            api_key_env=env_name,
            api_key=literal,
            check_model=False,
            provider=provider,
        )
        self.app.call_from_thread(
            self._test_done, seq, ok, message, ids, working_url, provider
        )

    def _test_done(self, seq, ok, message, ids, working_url, provider):
        if seq != self._test_seq:
            return  # a newer test superseded this one
        self._working_url = working_url
        url_field = self.query_one("#field-url", Input)
        if (
            isinstance(working_url, str)
            and working_url
            and url_field.value.strip()
            and working_url != url_field.value.strip()
        ):
            # The endpoint answered on the /v1-appended URL; make the fix visible.
            url_field.value = working_url
        status = self.query_one("#field-status", Static)
        status.update(f"{'OK' if ok else 'Failed'} — {message}")
        status.styles.color = "#61dbb9" if ok else "#f06d91"
        self.query_one("#form-test", Button).disabled = False
        if ids:
            self._set_model_options(ids, provider)

    def _set_model_options(self, ids, provider):
        llm = self._jarvis.config["llm"]
        current = llm.get("model") or ""
        preferred = remembered_model(self._jarvis.config, provider) or current
        options = list(ids)
        if preferred and preferred not in options:
            options.insert(0, preferred)
        panel = self.query_one("#field-model", OptionList)
        panel.clear_options()
        for model in options:
            panel.add_option(Option(model, id=model))
        panel.highlighted = 0
        for index, option in enumerate(panel.options):
            if option.id == preferred:
                panel.highlighted = index
                break

    def _apply(self):
        self._invalidate_test()
        jarvis = self._jarvis
        provider_panel = self.query_one("#field-provider", OptionList)
        provider_index = provider_panel.highlighted if provider_panel.highlighted is not None else 0
        provider = provider_panel.get_option_at_index(provider_index).id
        url = self.query_one("#field-url", Input).value.strip()
        key = self.query_one("#field-key", Input).value.strip()
        model_panel = self.query_one("#field-model", OptionList)
        model_index = model_panel.highlighted if model_panel.highlighted is not None else 0
        model = model_panel.get_option_at_index(model_index).id

        if provider != "custom":
            preset = PROVIDER_PRESETS.get(provider)
            if preset:
                # A highlighted preset wins; the URL field is only for custom.
                url = preset["base_url"]
        if provider == "custom" and not url:
            self.query_one("#form-error", Static).update("Base URL is required.")
            return

        if provider == "custom":
            command = f"/provider set {url}"
            echo = command
            if key:
                command += f" {key}"
                if key not in os.environ:
                    echo = command.replace(key, "••••••••")
        else:
            command = f"/provider use {provider}"
            echo = command

        commands = [command]
        current_model = jarvis.config["llm"].get("model")
        if model and model != current_model:
            commands.append(f"/model set {model}")

        results = [handle_slash_command(jarvis, cmd) for cmd in commands]
        self.dismiss({"command": echo, "result": "\n".join(results)})


class SlashFormScreen(ModalScreen):
    """Modal form that collects a slash command's fields before running it.

    Driven by the plain-data specs from :func:`commands.dialog_for_command`,
    so a future web composer can render the same forms.
    """

    BINDINGS = [Binding("escape", "form_cancel", "Cancel", show=False)]

    def __init__(self, spec, jarvis, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._spec = spec
        self._jarvis = jarvis

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._spec["title"], id="form-title"),
            Static(self._spec.get("description") or "", id="form-desc"),
            *self._field_widgets(),
            Static("", id="form-error"),
            Horizontal(
                Button("Cancel", id="form-cancel"),
                Button("Apply", id="form-submit", variant="success"),
                id="form-actions",
            ),
            id="form-box",
        )

    def _field_widgets(self):
        widgets = []
        for field in self._spec["fields"]:
            widgets.append(Static(field["label"], classes="field-label"))
            if field["type"] == "select":
                options = field.get("options", [])
                select = OptionList(
                    *(Option(label, id=value) for label, value in options),
                    classes="field-widget select",
                )
                select.id = f"field-{field['key']}"
                default = field.get("default")
                if default is not None:
                    for index, (_, value) in enumerate(options):
                        if value == default:
                            select.highlighted = index
                            break
                widgets.append(select)
            else:
                widgets.append(
                    Input(
                        placeholder=field.get("placeholder", ""),
                        id=f"field-{field['key']}",
                        classes="field-widget",
                    )
                )
        return widgets

    def on_mount(self):
        first = self.query_one(".field-widget")
        if first:
            first.focus()

    def on_input_submitted(self, event):
        if event.input.id.startswith("field-"):
            self._submit_form()

    def on_option_list_option_selected(self, event):
        fields = self._spec["fields"]
        for index, field in enumerate(fields):
            if f"field-{field['key']}" != event.option_list.id:
                continue
            if index == len(fields) - 1:
                self._submit_form()
            else:
                next_widget = self.query_one(f"#field-{fields[index + 1]['key']}")
                next_widget.focus()
            return

    def on_button_pressed(self, event):
        if event.button.id == "form-cancel":
            self.action_form_cancel()
        elif event.button.id == "form-submit":
            self._submit_form()

    def action_form_cancel(self):
        self.dismiss(None)

    def _submit_form(self):
        values = {}
        missing = []
        for field in self._spec["fields"]:
            key = field["key"]
            if field["type"] == "select":
                panel = self.query_one(f"#field-{key}", OptionList)
                index = panel.highlighted if panel.highlighted is not None else 0
                values[key] = panel.get_option_at_index(index).id
            else:
                box = self.query_one(f"#field-{key}", Input)
                value = box.value.strip()
                if field.get("required") and not value:
                    missing.append(field["label"])
                    continue
                values[key] = value
        if missing:
            self.query_one("#form-error", Static).update(
                f"Please fill in: {', '.join(missing)}"
            )
            return
        command = self._spec["command"](values)
        result = handle_slash_command(self._jarvis, command)
        self.dismiss({"command": command, "result": result})


class JarvisUI(App):
    TITLE = "JARVIS"
    CSS = """
    Screen { background: #070b12; color: #d8e7f2; }
    #root { height: 1fr; min-height: 0; }
    #rail {
        width: 40;
        min-width: 34;
        height: 1fr;
        min-height: 0;
        overflow-y: auto;
        background: #0b1420;
        border: double #294b68;
        padding: 1;
    }
    #brand { height: 3; content-align: center middle; color: #78d9ff; text-style: bold; border-bottom: solid #1b3b55; }
    #orb { height: 18; min-height: 18; content-align: center middle; background: #08111d; border: round #1c4665; }
    #rail-status { height: 3; content-align: center middle; color: #8faec0; background: #0e1c2b; border: round #1b3a54; }
    #main { width: 1fr; height: 1fr; min-height: 0; padding: 0 1; background: #09111b; }
    #chat-header { height: 4; padding: 0 1; border-bottom: solid #23425c; }
    #chat-title { height: 2; color: #aeeaff; text-style: bold; content-align: left bottom; }
    #chat-subtitle { height: 1; color: #6f8da2; content-align: left top; }
    #chat-presence { width: 18; color: #61dbb9; content-align: center middle; text-style: bold; }
    #chat { height: 1fr; min-height: 0; overflow-y: auto; padding: 1 1; }
    #welcome { width: 72%; min-height: 8; margin: 1 0 0 2; padding: 2 3; border: double #266080; background: #0b1d2b; content-align: center middle; }
    .message { width: 92%; margin: 0 0 1 0; padding: 1 2; border: round #1b3449; background: #0c1723; }
    .message.user { width: 76%; border-left: thick #42b8ea; background: #0d2030; color: #d7efff; }
    .message.assistant { border-left: thick #50d5bd; background: #0d211f; }
    .message.error { border-left: thick #f06d91; background: #2a1722; }
    .message.system { width: 92%; border-left: thick #6e85a0; background: #0b1621; color: #b7ccda; }
    .artifact { margin: 0 0 1 2; padding: 1; border: round #3b7597; background: #0c1e2e; }
    #input-row { height: auto; min-height: 5; padding: 1 0 0 0; border-top: solid #1d3a50; }
    #command-palette { height: auto; max-height: 5; margin: 0 0 1 3; display: none; background: #0a1826; border: round #34799a; color: #cfeeff; }
    #command-palette.visible { display: block; }
    #command-palette:focus { border: double #62dfff; }
    /* Compact mode for small terminals */
    .compact #rail { width: 30; min-width: 28; }
    .compact #orb { height: 10; min-height: 10; }
    .compact #welcome { width: 90%; margin: 1 0 0 1; }
    .compact #chat-presence { display: none; }
    .compact #hint { display: none; }
    #composer-line { height: 4; }
    #composer-prefix { width: 3; height: 3; content-align: center middle; color: #52d8be; background: #102a2a; border: round #2d786e; text-style: bold; }
    #input { width: 1fr; height: 3; min-height: 3; background: #102333; border: round #3b7798; padding: 0 1; color: #e4f5ff; }
    #input:focus { border: double #62dfff; background: #122a3d; }
    #send { width: 11; height: 3; margin-left: 1; background: #164b60; border: round #55d8ed; color: #dffaff; text-style: bold; }
    #send:hover { background: #246e85; }
    #hint { height: 1; padding-left: 4; color: #6f8da2; }
    #status { height: 1; content-align: center middle; color: #8faec0; background: #0b1723; }
    #memory-review-list { height: 9; border: round #765e2b; background: #0a141e; }
    #memory-review-actions { height: 3; }
    #memory-review-actions Button { width: 1fr; min-width: 8; }
    #approval-row { height: 3; padding: 0 1; display: none; background: #332912; border: round #81672a; }
    #approval-row.visible { display: block; }
    #approval-text { width: 1fr; content-align: left middle; color: yellow; }
    Collapsible { margin: 1 0 0 0; background: #0d1b29; border: round #1c3a53; }
    Collapsible > Contents { padding: 1; background: #09131e; }
    OptionList { background: #0a141f; border: round #20445d; }
    RichLog { height: 8; border: round #20445d; background: #0a141f; }
    SlashFormScreen { align: center middle; background: #070b12 85%; }
    ProviderScreen { align: center middle; background: #070b12 85%; }
    #form-box { width: 96; max-width: 96%; height: auto; max-height: 90%; overflow-y: auto; padding: 1 2 1 2; background: #0d1b29; border: round #34799a; }
    ProviderScreen #form-box { height: 86%; }
    #form-title { height: 2; color: #aeeaff; text-style: bold; content-align: left bottom; }
    #form-desc { height: auto; margin-bottom: 1; color: #6f8da2; }
    .field-label { height: 1; margin-top: 1; color: #78d9ff; }
    #form-box Input { width: 1fr; height: 3; margin-bottom: 1; background: #102333; border: round #3b7798; color: #e4f5ff; }
    #form-box Input:focus { border: double #62dfff; background: #122a3d; }
    #form-box OptionList { width: 1fr; height: auto; max-height: 12; margin-bottom: 1; background: #0a141f; border: round #20445d; }
    #form-box #field-provider { height: 8; }
    #form-box #field-model { height: 9; }
    #fields-top > Vertical, #fields-creds > Vertical { width: 1fr; height: auto; margin-right: 1; }
    #form-error { height: 1; margin-top: 1; color: #f06d91; }
    #field-status { height: 1; margin-top: 1; }
    #form-actions { height: 3; margin-top: 1; }
    #form-actions Button { width: 13; margin-right: 1; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+space", "push_to_talk", "Push-to-Talk"),
        Binding("escape", "cancel_turn", "Stop"),
        Binding("ctrl+n", "new_session", "New session"),
        Binding("ctrl+r", "retry_last_turn", "Retry"),
        Binding("ctrl+e", "export_session", "Export"),
        Binding("ctrl+v", "paste", "Paste"),
    ]

    def __init__(self, jarvis):
        super().__init__()
        self.jarvis = jarvis
        self._waveform = Waveform(id="orb")
        self._active_message = None
        self._stream_buffer = ""
        self._ptt_active = False
        self._ptt_loading = False
        self._listener = None
        self._pending_approval = None
        self._selected_memory = ""
        self._command_palette_suggestions = []
        self._command_palette_visible = False
        self._search_timer = None
        self._pending_search = ""
        self._event_callback = self._on_event
        self.jarvis.events.subscribe(self._event_callback)
        self.jarvis.voice.set_amplitude_callback(self._on_amplitude)

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Vertical(
                Static("⏻  J A R V I S", id="brand"),
                self._waveform,
                Static("Ready\nvoice: off", id="rail-status"),
                Collapsible(
                    Vertical(
                        Input(placeholder="Search conversations…", id="session-search"),
                        OptionList(id="sessions-list"),
                    ),
                    title="Conversations",
                    collapsed=True,
                    id="sessions-panel",
                ),
                Collapsible(
                    Vertical(
                        OptionList(id="memory-review-list"),
                        Horizontal(
                            Button("Approve", id="memory-approve", variant="success"),
                            Button("Reject", id="memory-reject", variant="error"),
                            Button("Expire", id="memory-expire"),
                            id="memory-review-actions",
                        ),
                    ),
                    title="Memory review",
                    collapsed=True,
                    id="memory-review-panel",
                ),
                Collapsible(
                    RichLog(id="activity-log", wrap=True, highlight=False),
                    title="Activity / tool calls",
                    collapsed=True,
                    id="activity-panel",
                ),
                Collapsible(
                    RichLog(id="diagnostics-log", wrap=True, highlight=False),
                    title="Diagnostics",
                    collapsed=True,
                    id="diagnostics-panel",
                ),
                id="rail",
            ),
            Vertical(
                Horizontal(
                    Vertical(
                        Static("Conversation", id="chat-title"),
                        Static("Active session · local workspace", id="chat-subtitle"),
                    ),
                    Static("● ONLINE", id="chat-presence"),
                    id="chat-header",
                ),
                ChatView(id="chat"),
                Vertical(
                    OptionList(id="command-palette", markup=False),
                    Horizontal(
                        Static("›", id="composer-prefix"),
                        CommandInput(placeholder="Type a message to Jarvis…", id="input"),
                        Button("Send  ↵", id="send", variant="primary"),
                        id="composer-line",
                    ),
                    Static("Enter send  ·  Type / for commands  ·  Ctrl+Space voice  ·  Esc stop", id="hint"),
                    id="input-row",
                ),
                Horizontal(
                    Static("", id="approval-text"),
                    Button("Allow", id="approve", variant="success"),
                    Button("Deny", id="deny", variant="error"),
                    id="approval-row",
                ),
                Static("Ready", id="status"),
                id="main",
            ),
            id="root",
        )

    def on_mount(self):
        self.query_one("#chat", ChatView).show_welcome()
        self._refresh_sessions()
        self._refresh_memory_review()
        self.query_one("#input", Input).focus()
        self.set_interval(0.12, self._animate_orb)
        self._check_compact()

    def on_resize(self, event):
        self._check_compact()

    def _check_compact(self):
        try:
            w, h = self.size.width, self.size.height
            compact = w < 90 or h < 22
            if compact:
                self.add_class("compact")
            else:
                self.remove_class("compact")
        except Exception:
            pass

    def on_unmount(self):
        self.jarvis.events.unsubscribe(self._event_callback)
        self.jarvis.close()

    def _animate_orb(self):
        self._waveform.tick()

    def _on_amplitude(self, amp: float):
        self.call_from_thread(self._waveform.set_amplitude, amp)

    def _on_event(self, event):
        """Translate runtime events into UI updates on the Textual thread."""
        if event.kind == "turn_started":
            self.call_from_thread(self._begin_assistant, "Thinking…")
            self.call_from_thread(self._set_state, "Thinking")
        elif event.kind == "assistant_delta":
            self.call_from_thread(self._append_assistant, event.data.get("content", ""))
        elif event.kind == "tool_started":
            name = event.data.get("name", "tool")
            args = json.dumps(event.data.get("args", {}), default=str)[:180]
            self.call_from_thread(self._activity, f"⚙ {name}  {args}")
            self.call_from_thread(self._expand_activity)
            self.call_from_thread(self._set_state, "Working")
        elif event.kind == "tool_finished":
            name = event.data.get("name", "tool")
            self.call_from_thread(self._activity, f"✓ {name} finished")
            self.call_from_thread(self._set_state, "Thinking")
            if name in {
                "remember", "forget", "memory_review_decide", "memory_expire",
                "memory_never_remember", "memory_allow_remember",
            }:
                self.call_from_thread(self._refresh_memory_review)
        elif event.kind == "artifact_created":
            self.call_from_thread(self._show_artifact, event.data.get("artifact", {}))
        elif event.kind == "approval_requested":
            self.call_from_thread(self._show_approval, event.data)
        elif event.kind == "approval_finished":
            self.call_from_thread(self._hide_approval)
        elif event.kind == "assistant_message":
            self.call_from_thread(self._finish_assistant, event.data.get("content", ""))
        elif event.kind == "turn_finished":
            self.call_from_thread(self._set_state, "Ready")
            self.call_from_thread(self._refresh_sessions)
            self.call_from_thread(self._refresh_memory_review)
        elif event.kind == "session_started":
            self.call_from_thread(self._refresh_sessions)
        elif event.kind == "config_changed":
            # A voice setting change creates a new engine; restore the live
            # waveform callback after that replacement.
            self.jarvis.voice.set_amplitude_callback(self._on_amplitude)
            self.call_from_thread(self._refresh_runtime_labels)
        elif event.kind == "error":
            msg = event.data.get("message", "Unknown error")
            self.call_from_thread(self._add_message, "error", msg)
            self.call_from_thread(self._diagnostic, msg)
            self.call_from_thread(self._expand_diagnostics)

    def _set_state(self, state):
        self._waveform.set_state(state)
        voice = "on" if self.jarvis.config.get("voice", {}).get("enabled", False) else "off"
        self.query_one("#rail-status", Static).update(f"{state}\nvoice: {voice}")
        self.query_one("#status", Static).update(state)
        self.query_one("#chat-presence", Static).update(f"● {state.upper()}")
        self._refresh_runtime_labels()

    def _refresh_runtime_labels(self):
        llm = self.jarvis.config.get("llm", {})
        provider = llm.get("provider", "custom")
        model = llm.get("model", "unknown")
        self.query_one("#chat-subtitle", Static).update(
            f"{provider} · {model} · local workspace"
        )

    def _add_message(self, role, content=""):
        chat = self.query_one("#chat", ChatView)
        card = chat.add_message(role, content)
        self.call_after_refresh(chat.scroll_end)
        return card

    def _begin_assistant(self, initial=""):
        self._stream_buffer = ""
        self._active_message = self._add_message("assistant", initial)
        # Mark as streaming so Markdown is deferred
        if self._active_message:
            self._active_message._is_streaming = True

    def _append_assistant(self, token):
        if self._active_message is None:
            self._begin_assistant()
        self._stream_buffer += token
        self._active_message.set_content(self._stream_buffer, streaming=True)
        self.query_one("#chat", ChatView).scroll_end(animate=False)

    def _finish_assistant(self, content):
        if self._active_message is None:
            self._begin_assistant()
        self._stream_buffer = content
        self._active_message.finalize(content or "(no response)")
        self.query_one("#chat", ChatView).scroll_end(animate=False)

    def _show_artifact(self, artifact):
        if not artifact:
            return
        chat = self.query_one("#chat", ChatView)
        chat.mount(ArtifactCard(artifact, classes="artifact"))
        chat.scroll_end()

    def _activity(self, text):
        self.query_one("#activity-log", RichLog).write(Text(text, style="yellow"))

    def _expand_activity(self):
        try:
            panel = self.query_one("#activity-panel", Collapsible)
            panel.collapsed = False
        except Exception:
            pass

    def _diagnostic(self, text):
        self.query_one("#diagnostics-log", RichLog).write(Text(text, style="red"))
        try:
            self.notify(text, severity="error", timeout=5)
        except Exception:
            pass

    def _expand_diagnostics(self):
        try:
            self.query_one("#diagnostics-panel", Collapsible).collapsed = False
        except Exception:
            pass

    def _show_approval(self, data):
        self._pending_approval = data.get("request_id")
        payload = json.dumps(data.get("payload", {}), default=str)
        self.query_one("#approval-text", Static).update(f"Approval needed: {data.get('action')}  {payload[:180]}")
        self.query_one("#approval-row").add_class("visible")
        self._set_state("Approval needed")
        try:
            self.query_one("#approve", Button).focus()
            self.notify("Approval required — press Allow or Deny", timeout=4)
        except Exception:
            pass

    def _hide_approval(self):
        self._pending_approval = None
        self.query_one("#approval-row").remove_class("visible")

    def on_button_pressed(self, event):
        button_id = event.button.id
        if button_id == "send":
            # When a turn is in progress, Send is repurposed as Stop
            try:
                if self.query_one("#input", Input).disabled:
                    self.action_cancel_turn()
                    return
            except Exception:
                pass
            self._submit_input()
            return
        if button_id in {"approve", "deny"}:
            if not self._pending_approval:
                return
            approved = button_id == "approve"
            self.jarvis.approvals.resolve(self._pending_approval, approved)
            self._hide_approval()
            return

        decisions = {
            "memory-approve": "approve",
            "memory-reject": "reject",
            "memory-expire": "expire",
        }
        decision = decisions.get(button_id)
        if not decision:
            return
        if not self._selected_memory:
            self._diagnostic("Select a memory to review first")
            return
        result = self.jarvis.registry.call(
            "memory_review_decide",
            {"name": self._selected_memory, "decision": decision},
        )
        if result.get("error"):
            self._diagnostic(result["error"])
            return
        self._activity(f"◆ Memory {self._selected_memory}: {result.get('status', decision)}")
        self._selected_memory = ""
        self._refresh_memory_review()

    def _set_status(self, text):
        self._set_state(text)

    def _refresh_sessions(self, query=None):
        if query is None:
            try:
                query = self.query_one("#session-search", Input).value
            except Exception:
                query = ""
        sessions = self.jarvis.sessions.search(query, limit=8)
        panel = self.query_one("#sessions-list", OptionList)
        panel.clear_options()
        for item in sessions:
            title = item.get("title", "Conversation").replace("\n", " ")[:28]
            panel.add_option(Option(f"{title}\n  {item['id']}", id=item["id"]))
        if not sessions:
            panel.add_option(Option("No sessions yet", id="empty"))

    def _refresh_memory_review(self):
        panel = self.query_one("#memory-review-list", OptionList)
        result = self.jarvis.registry.call("memory_review", {"limit": 8})
        panel.clear_options()
        self._selected_memory = ""
        memories = result.get("memories", []) if isinstance(result, dict) else []
        for item in memories:
            name = item.get("name", "memory")
            preview = " ".join((item.get("memory") or "").split())[:82]
            label = (
                f"{name} · {item.get('category', 'general')} · "
                f"{item.get('confidence', '?')}\n{preview}"
            )
            panel.add_option(Option(label, id=name))
        if not memories:
            panel.add_option(Option("No pending memories", id="empty"))

    def on_input_changed(self, event):
        if event.input.id == "session-search":
            # Debounce: avoid scanning all sessions on every keystroke
            self._pending_search = event.value
            if self._search_timer is not None:
                self._search_timer.stop()
            self._search_timer = self.set_timer(0.25, self._do_search)
        elif event.input.id == "input":
            self._update_command_palette(event.value)

    def _do_search(self):
        self._search_timer = None
        self._refresh_sessions(self._pending_search)

    def _update_command_palette(self, value):
        suggestions = slash_command_suggestions(value)
        self._command_palette_suggestions = suggestions
        panel = self.query_one("#command-palette", OptionList)
        if not suggestions:
            self._dismiss_command_palette()
            return
        panel.set_options(
            Option(
                f"{suggestion['label']}  ·  {suggestion['description']}",
                id=str(index),
            )
            for index, suggestion in enumerate(suggestions)
        )
        panel.highlighted = 0
        panel.add_class("visible")
        self._command_palette_visible = True
        self.query_one("#hint", Static).update(
            "↑↓ choose  ·  Tab insert  ·  Enter run/open form  ·  Esc close"
        )

    def _move_command_selection(self, offset):
        if not self._command_palette_visible or not self._command_palette_suggestions:
            return
        panel = self.query_one("#command-palette", OptionList)
        current = panel.highlighted if panel.highlighted is not None else 0
        panel.highlighted = (current + offset) % len(self._command_palette_suggestions)
        panel.scroll_to_highlight()

    def _command_palette_should_submit(self):
        if not self._command_palette_suggestions:
            return False
        current = self.query_one("#command-palette", OptionList).highlighted
        if current is None:
            return False
        value = self.query_one("#input", Input).value.strip().lower()
        return value == self._command_palette_suggestions[current]["command"]

    def _accept_command_suggestion(self, open_dialog=False):
        if not self._command_palette_visible or not self._command_palette_suggestions:
            return False
        panel = self.query_one("#command-palette", OptionList)
        current = panel.highlighted
        if current is None:
            current = 0
        suggestion = self._command_palette_suggestions[current]
        box = self.query_one("#input", Input)

        # Enter runs the command (opening a form for the commands that need
        # input); Tab only inserts the completed text into the composer.
        if open_dialog:
            spec = dialog_for_command(self.jarvis, suggestion["command"])
            if spec:
                self._dismiss_command_palette()
                self._open_slash_form(spec)
                return True

        box.value = suggestion["insert"]
        box.cursor_position = len(box.value)
        self._dismiss_command_palette()
        box.focus()
        return True

    def _dismiss_command_palette(self):
        if not self._command_palette_visible:
            return False
        self._command_palette_visible = False
        self._command_palette_suggestions = []
        panel = self.query_one("#command-palette", OptionList)
        panel.remove_class("visible")
        panel.clear_options()
        self.query_one("#hint", Static).update(
            "Enter send  ·  Type / for commands  ·  Ctrl+Space voice  ·  Esc stop"
        )
        return True

    def on_option_list_option_selected(self, event):
        if event.option_list.id == "command-palette":
            self._accept_command_suggestion(open_dialog=True)
            return
        if event.option_list.id == "memory-review-list":
            self._selected_memory = "" if event.option.id == "empty" else event.option.id
            return
        if event.option_list.id != "sessions-list" or event.option.id == "empty":
            return
        result = self.jarvis.resume_session(event.option.id)
        if result.get("error"):
            self._diagnostic(result["error"])
            return
        chat = self.query_one("#chat", ChatView)
        chat.remove_children()
        self._active_message = None
        for message in self.jarvis.history:
            if message.get("role") in ("user", "assistant") and message.get("content"):
                self._add_message(message["role"], message["content"])
        if not self.jarvis.history:
            chat.show_welcome()
        self._set_state("Ready")

    # ---------- keyboard actions ----------

    def action_new_session(self):
        self.jarvis.new_session()
        chat = self.query_one("#chat", ChatView)
        chat.remove_children()
        self._active_message = None
        chat.show_welcome()
        self._refresh_sessions()

    def action_cancel_turn(self):
        self.jarvis.cancel()
        self._set_state("Ready")
        self._activity("■ Stop requested")

    def action_retry_last_turn(self):
        if self.jarvis._active_turn:
            self._diagnostic("A turn is already in progress")
            return
        text = self.jarvis.last_user_message()
        if not text:
            self._diagnostic("There is no previous user turn to retry")
            return
        self._add_message("user", f"↻ {text}")
        self._submit_turn(text)

    def action_export_session(self):
        try:
            destination = self.jarvis.sessions.root / "exports" / f"{self.jarvis.session_id}.md"
            path = self.jarvis.sessions.export(self.jarvis.session_id, destination=destination)
        except (OSError, ValueError) as error:
            self._diagnostic(f"Session export failed: {error}")
            return
        self._activity(f"↥ Session exported to {path}")

    def action_paste(self):
        try:
            import pyperclip
            text = pyperclip.paste()
        except ImportError:
            self._diagnostic("Clipboard support is not installed")
            return
        if text:
            box = self.query_one("#input", Input)
            box.value += text
            box.focus()

    # ---------- push-to-talk ----------

    def action_push_to_talk(self):
        if self._ptt_loading:
            return
        if self._ptt_active:
            self._ptt_active = False
            self._set_state("Transcribing")
            self.run_worker(self._finish_ptt, thread=True, exclusive=False)
        else:
            self._start_ptt()

    def _start_ptt(self):
        from jarvis.listen import get_listener
        self._listener = get_listener(self.jarvis.config)
        self._ptt_loading = True
        self._set_state("Loading mic")
        self.run_worker(self._start_ptt_worker, thread=True, exclusive=False)

    def _start_ptt_worker(self):
        ok = self._listener.start()
        self.call_from_thread(self._ptt_started, ok)

    def _ptt_started(self, ok):
        self._ptt_loading = False
        if ok:
            self._ptt_active = True
            self._set_state("Listening")
            self._activity("🎙 Listening… press Ctrl+Space to stop")
        else:
            self._set_state("Ready")
            self._diagnostic("Microphone or speech model could not start")

    def _finish_ptt(self):
        text = self._listener.stop() if self._listener else ""
        self.call_from_thread(self._transcript_ready, text)

    def _transcript_ready(self, text):
        if not text:
            self._set_state("Ready")
            self._activity("🎙 No speech detected")
            return
        self._add_message("user", f"🎙 {text}")
        self._submit_turn(text)

    # ---------- turn handling ----------

    def on_input_submitted(self, event):
        if event.input.id != "input":
            return
        self._submit_input()

    def _submit_input(self):
        box = self.query_one("#input", Input)
        text = box.value.strip()
        if not text:
            return

        spec = dialog_for_command(self.jarvis, text)
        if spec:
            self._open_slash_form(spec)
            return

        box.value = ""

        command_result = handle_slash_command(self.jarvis, text)
        if command_result is not None:
            self._add_message("user", text)
            self._add_message("system", command_result)
            self.jarvis.voice.set_amplitude_callback(self._on_amplitude)
            self._refresh_runtime_labels()
            self._activity(f"⌁ {text}")
            self._set_state("Ready")
            return

        self._add_message("user", text)
        self._submit_turn(text)

    def _open_slash_form(self, spec):
        if spec.get("screen") == "provider":
            self.push_screen(ProviderScreen(self.jarvis), self._on_slash_form_result)
            return
        self.push_screen(SlashFormScreen(spec, self.jarvis), self._on_slash_form_result)

    def _on_slash_form_result(self, result):
        if not result:
            return
        self.query_one("#input", Input).value = ""
        self._add_message("user", result["command"])
        self._add_message("system", result["result"])
        self.jarvis.voice.set_amplitude_callback(self._on_amplitude)
        self._refresh_runtime_labels()
        self._activity(f"⌁ {result['command']}")
        self._set_state("Ready")
        self.query_one("#input", Input).focus()

    def _submit_turn(self, text):
        box = self.query_one("#input", Input)
        send = self.query_one("#send", Button)
        box.disabled = True
        box.placeholder = "Jarvis is working… — Esc to stop"
        send.label = "Stop ■"
        send.variant = "error"
        self.run_worker(lambda: self._turn(text), thread=True, exclusive=True)

    def _turn(self, text):
        try:
            reply = self.jarvis.ask(text)
            if self.jarvis.config.get("voice", {}).get("enabled", False) and reply:
                self.call_from_thread(self._set_state, "Speaking")
                self.jarvis.voice.speak(reply)
        except Exception as e:
            self.jarvis.events.emit(Event("error", self.jarvis.session_id, data={"message": str(e)}))
            self.call_from_thread(self._diagnostic, str(e))
        finally:
            self.call_from_thread(self._reenable_input)

    def _reenable_input(self):
        box = self.query_one("#input", Input)
        send = self.query_one("#send", Button)
        box.disabled = False
        send.label = "Send  ↵"
        send.variant = "primary"
        send.disabled = False
        box.placeholder = "Type a message to Jarvis…"
        self._set_state("Ready")
        box.focus()


def run_ui(jarvis):
    JarvisUI(jarvis).run()
