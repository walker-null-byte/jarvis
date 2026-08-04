#!/usr/bin/env python3
"""Jarvis demo: an entirely custom Tk window, drawn in code."""

import math
import random
import time
import tkinter as tk

BG = "#0e1116"
FG = "#53d1b0"
GRID = "#1c232e"
ACCENT = "#7aa2f7"
TEXT = "#c8d3f5"
MUTED = "#566273"


class Demo(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Jarvis — arbitrary Tk demo")
        self.configure(bg=BG)
        self.geometry("640x440")
        self.amp = tk.DoubleVar(value=60.0)
        self.speed = tk.DoubleVar(value=1.0)
        self.particles = [
            [random.uniform(0, 640), random.uniform(0, 440),
             random.uniform(-0.8, 0.8), random.uniform(-0.8, 0.8)]
            for _ in range(60)
        ]
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._build_controls()
        self.last = time.monotonic()
        self.after(16, self.tick)

    def _build_controls(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(bar, text="amplitude", fg=TEXT, bg=BG,
                 font=("monospace", 9)).pack(side="left")
        tk.Scale(bar, from_=10, to=140, variable=self.amp, orient="horizontal",
                 bg=BG, fg=FG, highlightthickness=0, troughcolor=GRID,
                 sliderrelief="flat", length=150).pack(side="left", padx=6)
        tk.Label(bar, text="speed", fg=TEXT, bg=BG,
                 font=("monospace", 9)).pack(side="left", padx=(18, 0))
        tk.Scale(bar, from_=0.2, to=3.0, resolution=0.1, variable=self.speed,
                 orient="horizontal", bg=BG, fg=FG, highlightthickness=0,
                 troughcolor=GRID, sliderrelief="flat", length=150).pack(side="left", padx=6)

    def tick(self):
        now = time.monotonic()
        dt = min(now - self.last, 0.05)
        self.last = now
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10:
            w, h = 640, 420
        c.delete("all")
        for x in range(0, w, 40):
            c.create_line(x, 0, x, h, fill=GRID)
        for y in range(0, h, 40):
            c.create_line(0, y, w, y, fill=GRID)
        pts = []
        for i in range(0, w + 1, 2):
            x = i
            y = h / 2 + self.amp.get() * math.sin(
                (x / w) * math.tau * 3 + now * 2.2 * self.speed.get())
            pts.extend((x, y))
        c.create_line(pts, fill=FG, width=2, smooth=True)
        for p in self.particles:
            p[0] += p[2] * dt * 60
            p[1] += p[3] * dt * 60
            if p[0] < 0 or p[0] > w:
                p[2] *= -1
            if p[1] < 0 or p[1] > h:
                p[3] *= -1
            c.create_oval(p[0] - 2, p[1] - 2, p[0] + 2, p[1] + 2,
                          fill=ACCENT, outline="")
        c.create_text(w - 12, 12, anchor="ne", text=time.strftime("%H:%M:%S"),
                      fill=TEXT, font=("monospace", 12, "bold"))
        c.create_text(12, 12, anchor="nw",
                      text="JARVIS demo window — drawn entirely in code",
                      fill=MUTED, font=("monospace", 9))
        self.after(16, self.tick)


if __name__ == "__main__":
    Demo().mainloop()
