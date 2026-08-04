"""Tk-based renderer for a single Jarvis artifact.

This module is launched as a detached process by ``SurfaceManager``.  It is
intentionally small and only renders data already persisted by ArtifactStore.
"""

import json
import sys
from pathlib import Path


def _pretty_data(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _chart_series(artifact):
    """Extract (x_labels, y_values) from stored chart data.

    Handles both the Chart.js shape the artifact tools actually emit
    (labels + datasets[].data) and the older x/y shape.
    """
    data = artifact.get("data") or {}
    if not isinstance(data, dict):
        return [], []
    x_values = data.get("labels") or data.get("x") or []
    datasets = data.get("datasets") or []
    if datasets and isinstance(datasets[0], dict) and datasets[0].get("data"):
        y_values = datasets[0]["data"]
    else:
        y_values = data.get("y") or []
    return x_values, y_values


def _render(root, artifact):
    import tkinter as tk

    kind = artifact.get("type", "text")
    title = artifact.get("title", "Jarvis artifact")
    root.title(f"Jarvis · {title}")
    root.geometry("760x520")

    header = tk.Label(root, text=title, anchor="w", padx=12, pady=8,
                      font=("TkDefaultFont", 13, "bold"))
    header.pack(fill="x")

    if kind == "image" and artifact.get("path"):
        try:
            image = tk.PhotoImage(file=artifact["path"])
            label = tk.Label(root, image=image)
            label.image = image
            label.pack(expand=True)
            return
        except Exception:
            pass

    if kind == "chart":
        x_values, y_values = _chart_series(artifact)
        if y_values and all(isinstance(value, (int, float)) for value in y_values):
            canvas = tk.Canvas(root, background="#101823", highlightthickness=0)
            canvas.pack(fill="both", expand=True, padx=12, pady=12)

            def draw(_event=None):
                canvas.delete("all")
                width = max(canvas.winfo_width(), 300)
                height = max(canvas.winfo_height(), 240)
                margin = 42
                maximum = max(max(y_values), 1)
                points = []
                for index, value in enumerate(y_values):
                    px = margin + index * (width - 2 * margin) / max(len(y_values) - 1, 1)
                    py = height - margin - (value / maximum) * (height - 2 * margin)
                    points.extend((px, py))
                    canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill="#53d1b0", outline="")
                    label = str(x_values[index]) if index < len(x_values) else str(index + 1)
                    canvas.create_text(px, height - margin + 16, text=label, fill="#b9c9d8")
                if len(points) >= 4:
                    canvas.create_line(*points, fill="#53d1b0", width=3, smooth=True)

            canvas.bind("<Configure>", draw)
            draw()
            return

    text = artifact.get("content") or _pretty_data(artifact.get("data"))
    widget = tk.Text(root, wrap="word", padx=12, pady=12)
    widget.insert("1.0", text or artifact.get("path", "(empty artifact)"))
    widget.configure(state="disabled")
    widget.pack(fill="both", expand=True)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m jarvis.surface_window <artifact.json>")
    artifact = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    import tkinter as tk
    root = tk.Tk()
    _render(root, artifact)
    root.mainloop()


if __name__ == "__main__":
    main()
