// The Jarvis orb: a canvas port of the TUI Waveform. A golden-angle point sphere
// with an inner core and drifting ambient particles, perspective-projected and
// depth-sorted. The state changes how it moves.

function sphere(count, radius, seed) {
  let s = seed;
  const rand = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
  const golden = Math.PI * (3 - Math.sqrt(5));
  const points = [];
  for (let i = 0; i < count; i++) {
    const y = 1 - ((i + 0.5) * 2) / count;
    const ring = Math.sqrt(Math.max(0, 1 - y * y));
    const angle = i * golden + (rand() - 0.5) * 0.08;
    points.push([radius * ring * Math.cos(angle), radius * y, radius * ring * Math.sin(angle)]);
  }
  return points;
}

function ambient(count, seed) {
  let s = seed;
  const rand = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
  const points = [];
  for (let i = 0; i < count; i++) {
    const theta = rand() * Math.PI * 2, phi = Math.acos(2 * rand() - 1), r = 1.75 + rand() * 0.9;
    points.push([r * Math.sin(phi) * Math.cos(theta), r * Math.sin(phi) * Math.sin(theta), r * Math.cos(phi)]);
  }
  return points;
}

const STATES = {
  idle:      { spin: 0.42, pulse: 0.0,  breathe: 0.03 },
  thinking:  { spin: 1.1,  pulse: 0.07, breathe: 0.03 },
  working:   { spin: 0.8,  pulse: 0.04, breathe: 0.03 },
  listening: { spin: 0.3,  pulse: 0.05, breathe: 0.06 },
  speaking:  { spin: 0.55, pulse: 0.0,  breathe: 0.03 },
};

export class Orb {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.outer = sphere(260, 1.15, 11);
    this.inner = sphere(96, 0.72, 17);
    this.ambient = ambient(42, 23);
    this.state = "idle";
    this.phase = 0;
    this.level = 0.04;
    this.target = 0.04;
    this.samples = new Array(64).fill(0.04);
    this.reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.running = false;
    document.addEventListener("visibilitychange", () => (document.hidden ? this.stop() : this.start()));
    this.resize();
    this.start();
  }

  setState(state) {
    this.state = STATES[state] ? state : "idle";
    if (this.reduced) this.draw();
  }

  setAmplitude(level) {
    this.target = Math.max(0, Math.min(1, level));
    this.samples.push(this.target);
    this.samples.shift();
  }

  resize() {
    const ratio = window.devicePixelRatio || 1;
    const size = this.canvas.clientWidth || 96;
    this.canvas.width = size * ratio;
    this.canvas.height = size * ratio;
    this.size = size * ratio;
    this.draw();
  }

  start() {
    if (this.running || this.reduced) return;
    this.running = true;
    const frame = () => {
      if (!this.running) return;
      this.tick();
      this.draw();
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  stop() { this.running = false; }

  tick() {
    const mode = STATES[this.state];
    this.phase = (this.phase + 0.03 + mode.spin * 0.02) % (Math.PI * 200);
    if (this.state !== "speaking") this.target *= 0.9; // amplitude decays when nothing is speaking
    this.level += (this.target - this.level) * 0.34;
    this.samples.push(Math.max(0.02, Math.min(1, this.level + mode.breathe * (Math.sin(this.phase) + 1))));
    this.samples.shift();
  }

  draw() {
    const { ctx, size } = this;
    const mode = STATES[this.state];
    ctx.clearRect(0, 0, size, size);
    const half = size / 2;
    const scale = size * 0.27;
    const level = Math.max(this.level, this.target);
    const rotY = this.phase * mode.spin + level * 0.35;
    const rotX = Math.sin(this.phase * 0.31) * 0.22;
    const pulse = 1 + mode.pulse * Math.sin(this.phase * 3);
    const cosY = Math.cos(rotY), sinY = Math.sin(rotY), cosX = Math.cos(rotX), sinX = Math.sin(rotX);

    const project = ([x, y, z], k = 1) => {
      const x1 = x * cosY + z * sinY, z1 = -x * sinY + z * cosY;
      const y1 = y * cosX - z1 * sinX, z2 = y * sinX + z1 * cosX;
      const perspective = 1 / Math.max(1.35, 3.25 - z2);
      return { x: half + x1 * scale * 2 * k * perspective * pulse, y: half + y1 * scale * 2 * k * perspective * pulse, z: z2 };
    };

    const dots = [];
    const drift = this.phase * 0.08;
    for (const [x, y, z] of this.ambient) {
      const p = project([x * Math.cos(drift) - z * Math.sin(drift), y, x * Math.sin(drift) + z * Math.cos(drift)], 0.8);
      dots.push({ ...p, z: p.z - 1, r: 0.7, a: 0.28 });
    }
    this.outer.forEach((point, i) => {
      const energy = this.samples[(i * 7 + Math.floor(this.phase * 5)) % this.samples.length];
      const ripple = Math.sin(this.phase * 2.3 + i * 0.37) * 0.045;
      const p = project(point, 1 + Math.max(level, energy) * 0.22 + ripple);
      const visual = Math.max(energy, level * 0.45);
      dots.push({ ...p, r: visual > 0.68 ? 2.4 : visual > 0.25 ? 1.7 : 1.1, a: p.z > 0.15 ? 0.95 : 0.45 });
    });
    const innerPulse = 1 + level * 0.13 * Math.sin(this.phase * 5.5);
    for (const point of this.inner) {
      const p = project(point, innerPulse);
      dots.push({ ...p, z: p.z + 0.2, r: p.z > 0 ? 1.5 : 1, a: p.z > 0.1 ? 0.95 : 0.5, core: true });
    }

    dots.sort((a, b) => a.z - b.z); // far to near
    const ratio = size / 96;
    for (const d of dots) {
      ctx.fillStyle = d.core ? `rgba(220,250,255,${d.a})` : `rgba(45,212,232,${d.a})`;
      ctx.beginPath();
      ctx.arc(d.x, d.y, Math.max(0.6, d.r * ratio * 0.8), 0, Math.PI * 2);
      ctx.fill();
    }
  }
}
