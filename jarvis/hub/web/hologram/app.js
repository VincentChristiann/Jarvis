import {GestureEngine} from "/hologram/gestures.js";

const $ = (id) => document.getElementById(id);
const CONNECTIONS = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]];
const ACTIONS = [
  {label: "Play/Pause", glyph: "⏯", action: {cmd: "media.play_pause"}},
  {label: "Previous", glyph: "⏮", action: {cmd: "media.previous"}},
  {label: "Next", glyph: "⏭", action: {cmd: "media.next"}},
  {label: "Mute", glyph: "🔇", action: {cmd: "volume.mute"}},
  {label: "Volume −", glyph: "🔉", action: {cmd: "volume.step", args: {delta: -10}}},
  {label: "Volume +", glyph: "🔊", action: {cmd: "volume.step", args: {delta: 10}}},
  {label: "Cursor mode", glyph: "🖱", action: {special: "cursor"}},
  {label: "Back", glyph: "⬅", action: {special: "back"}},
];
const AREA = {x0: 0.15, x1: 0.85, y0: 0.12, y1: 0.88};   // the part of the camera view that maps to the whole screen
const CURSOR_SENS = 2200, SCROLL_SENS = 60;
const NO_HAND_STOP_MS = 60_000, RENEW_MS = 20_000, POLL_MS = 3_000;

async function api(method, path, body) {
  const r = await fetch(path, {method, headers: {"X-Jarvis": "1", "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { document.body.textContent = "Session expired. Run `jarvis hub dashboard` again."; throw new Error("auth"); }
  return r.json();
}
const clamp01 = (v) => Math.min(1, Math.max(0, v));

export class App {
  constructor() {
    this.engine = new GestureEngine();
    this.devices = []; this.selected = null; this.map = {};
    this.scene = null; this.tracker = null; this.cursor = null;
    this.lastHand = 0; this.timers = []; this.sig = "";
  }

  status(t) { $("status").textContent = t; }
  flash(t) { $("action").textContent = t; clearTimeout(this._f); this._f = setTimeout(() => ($("action").textContent = ""), 2500); }

  async init() {
    $("start").onclick = () => this.start();
    $("stop").onclick = () => this.stop("stopped");
    addEventListener("keydown", (e) => { if (e.key === "Escape") this.stop("stopped"); });
    document.addEventListener("visibilitychange", () => { if (document.hidden) this.stop("page hidden"); });
    const v = await api("GET", "/admin/vision");
    if (!v.installed) { $("start").disabled = true; return this.status("Hand-tracking files are not installed. Run:  jarvis vision install"); }
    this.map = (await api("GET", "/admin/gesture-map")).map;
    await this.loadScene();
    await this.refreshDevices();
    this.timers.push(setInterval(() => this.refreshDevices(), POLL_MS));
    this.status("Ready. Pick a device with the mouse, or press Start camera to use your hand.");
    this.ready = true;
  }

  async loadScene() {
    const {HoloScene} = await import("/hologram/scene.js");
    this.scene = new HoloScene($("gl"));
    const c = $("gl");
    c.addEventListener("pointermove", (e) => { if (!this.tracker?.running) this.scene.setPointer({x: e.clientX / innerWidth, y: e.clientY / innerHeight}); });
    c.addEventListener("click", () => { if (!this.tracker?.running) this.activate(this.scene.hover?.userData.action); });
    const ov = $("overlay"); const fit = () => { ov.width = innerWidth; ov.height = innerHeight; }; fit(); addEventListener("resize", fit);
    const loop = (t) => {
      if (this.tracker?.running) { const f = this.tracker.detect(t); if (f) this.onFrame(f.hand, f.head, t, f.aspect); this.checkIdle(t); }
      this.scene.render(t); requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  async refreshDevices() {
    const list = (await api("GET", "/admin/devices")).filter((d) => !d.revoked && d.kind !== "web");
    this.devices = list;
    if (!this.selected && list.filter((d) => d.online).length === 1) this.selected = list.find((d) => d.online).id;
    const sig = JSON.stringify(list.map((d) => [d.id, d.name, d.online])) + this.selected;
    if (sig !== this.sig && this.scene) { this.sig = sig; this.rebuild(); }
  }

  rebuild() { this.scene.build(this.devices, this.selected, ACTIONS); }

  // ---- camera session ---------------------------------------------------------------
  async start() {
    if (this.tracker?.running) return;
    $("start").disabled = true;
    const m = await api("POST", "/admin/mode", {mode: "hologram"});
    if (m.error) { $("start").disabled = false; return this.status("Camera refused by the hub: " + m.error); }
    try {
      if (!this.tracker) {
        this.status("Loading hand-tracking model…");
        const {Tracker} = await import("/hologram/tracker.js");
        this.tracker = new Tracker($("cam"));
        await this.tracker.load({face: true});
      }
      await this.tracker.startCamera();
    } catch (e) {
      await api("POST", "/admin/mode", {mode: "idle"}).catch(() => {});
      $("start").disabled = false;
      return this.status("Could not start the camera: " + (e.message || e));
    }
    this.engine.reset(); this.lastHand = performance.now();
    $("lamp").className = "lamp on"; $("lamp-text").textContent = "CAMERA ON"; $("stop").hidden = false; $("start").hidden = true;
    this.status(`Tracking (${this.tracker.delegate}${this.tracker.face ? ", head" : ""}). Hand in view to begin.`);
    this.timers.push(this._renew = setInterval(() => api("POST", "/admin/mode", {mode: "hologram"}).catch(() => {}), RENEW_MS));
    this.timers.push(this._poll = setInterval(async () => {
      const m = await api("GET", "/admin/mode").catch(() => null);
      if (m && m.mode === "idle" && this.tracker?.running) this.stop("the hub turned the camera off");
    }, POLL_MS));
  }

  async stop(why = "stopped") {
    clearInterval(this._renew); clearInterval(this._poll);
    if (!this.tracker?.running) return;
    this.tracker.stopCamera();
    this.exitCursor(true);
    this.scene.setHead({x: 0.5, y: 0.5}); this.scene.setPointer(null);
    const g = $("overlay").getContext("2d"); g.clearRect(0, 0, innerWidth, innerHeight);
    $("lamp").className = "lamp off"; $("lamp-text").textContent = "CAMERA OFF"; $("stop").hidden = true; $("start").hidden = false; $("start").disabled = false;
    this.status(`Camera off (${why}).`);
    await api("POST", "/admin/mode", {mode: "idle"}).catch(() => {});
  }

  checkIdle(t) { if (t - this.lastHand > NO_HAND_STOP_MS) this.stop("no hand seen for a minute"); }

  // ---- one camera frame -> gestures -> actions --------------------------------------------
  onFrame(lm, head, t, aspect = 4 / 3) {
    this.engine.o.aspect = aspect;
    if (head && this.scene) this.scene.setHead(head);
    const ev = this.engine.update(lm, t);
    if (lm) this.lastHand = t;
    const st = this.engine.state;
    const screen = st.pointer ? {x: clamp01((st.pointer.x - AREA.x0) / (AREA.x1 - AREA.x0)), y: clamp01((st.pointer.y - AREA.y0) / (AREA.y1 - AREA.y0))} : null;
    if (!this.cursor && this.scene) this.scene.setPointer(lm ? screen : null);
    for (const e of ev) this.handle(e);
    if (this.cursor) this.moveCursor(st, t);
    this.draw(lm, screen, st.pinching, aspect);
    return ev;
  }

  handle(e) {
    if (e.type === "pinch_down" && !this.cursor) this.scene?.press(true);
    else if (e.type === "pinch_up") this.scene?.press(false);
    else if (e.type === "click") { if (this.cursor) this.run("input.click", {button: "left"}, true); else this.activate(this.scene?.hover?.userData.action); }
    else if (e.type === "swipe") this.gesture(`swipe_${e.dir}`);
    else if (e.type === "hold") this.gesture(`hold_${e.pose}`);
  }

  gesture(name) {
    if (this.cursor) { if (name === "hold_fist") this.exitCursor(); return; }
    const b = this.map[name]; if (!b) return;
    this.flash(`${name.replace("_", " ")} → ${b.cmd}`);
    this.run(b.cmd, b.args);
  }

  activate(a) {
    if (!a) return;
    if (a.kind === "device") { this.selected = a.id; this.rebuild(); this.status(`Selected ${this.devices.find((d) => d.id === a.id)?.name}.`); }
    else if (a.special === "back") { this.exitCursor(); this.selected = null; this.rebuild(); }
    else if (a.special === "cursor") this.enterCursor();
    else if (a.cmd) { this.flash(a.label || a.cmd); this.run(a.cmd, a.args); }
  }

  async run(cmd, args, quiet) {
    if (!this.selected) return this.flash("Pick a device first");
    const r = await api("POST", "/admin/send", {target: this.selected, cmd, args: args || {}});
    if (!r.ok && !quiet) this.flash(`${cmd}: ${r.error}`);
    return r;
  }

  // ---- cursor mode: your finger drives the other computer's mouse ---------------------------
  enterCursor() {
    if (!this.selected) return this.flash("Pick a device first");
    this.cursor = {last: null, ax: 0, ay: 0, sy: 0, busy: false, since: 0};
    this.scene.setPointer(null);
    $("banner").hidden = false; $("banner").textContent = `CURSOR MODE → ${this.devices.find((d) => d.id === this.selected)?.name} · hold a fist to leave`;
  }

  exitCursor(silent) {
    if (!this.cursor) return;
    this.cursor = null; $("banner").hidden = true;
    if (!silent) this.flash("Cursor mode off");
  }

  moveCursor(st, t) {
    const c = this.cursor;
    if (!st.pointer || st.pinching) { c.last = st.pointer; return; }
    if (c.last) {
      const dx = st.pointer.x - c.last.x, dy = st.pointer.y - c.last.y;
      if (st.pose === "peace") c.sy += dy * SCROLL_SENS;
      else { c.ax += dx * CURSOR_SENS; c.ay += dy * CURSOR_SENS; }
    }
    c.last = st.pointer;
    if (t - c.since < 40 || c.busy) return;
    c.since = t;
    const mx = Math.trunc(c.ax), my = Math.trunc(c.ay), sc = Math.trunc(c.sy);
    if (!mx && !my && !sc) return;
    c.ax -= mx; c.ay -= my; c.sy -= sc; c.busy = true;
    const jobs = [];
    if (mx || my) jobs.push(this.run("input.move", {dx: Math.max(-2000, Math.min(2000, mx)), dy: Math.max(-2000, Math.min(2000, my))}, true));
    if (sc) jobs.push(this.run("input.scroll", {amount: Math.max(-20, Math.min(20, -sc))}, true));
    Promise.all(jobs).finally(() => { c.busy = false; });
  }

  // ---- overlay: skeleton and reticle ------------------------------------------------------------
  draw(lm, screen, pinching, aspect) {
    const ov = $("overlay"), g = ov.getContext("2d"), W = ov.width, H = ov.height;
    g.clearRect(0, 0, W, H);
    if (!lm) return;
    // the preview video uses object-fit: cover and is mirrored; draw the skeleton the same way
    const scale = Math.max(W / (aspect * 100), H / 100), vw = aspect * 100 * scale, vh = 100 * scale;
    const P = (p) => [W / 2 + ((1 - p.x) - 0.5) * vw, H / 2 + (p.y - 0.5) * vh];
    g.strokeStyle = "rgba(76,201,240,.8)"; g.lineWidth = 2; g.shadowColor = "#4cc9f0"; g.shadowBlur = 8;
    g.beginPath(); for (const [a, b] of CONNECTIONS) { const [x1, y1] = P(lm[a]), [x2, y2] = P(lm[b]); g.moveTo(x1, y1); g.lineTo(x2, y2); } g.stroke();
    g.fillStyle = "#e6ecff"; for (const p of lm) { const [x, y] = P(p); g.beginPath(); g.arc(x, y, 3, 0, 7); g.fill(); }
    if (screen && !this.cursor) {
      g.shadowBlur = 16; g.strokeStyle = pinching ? "#3ddc97" : "#4cc9f0"; g.lineWidth = 3;
      g.beginPath(); g.arc(screen.x * W, screen.y * H, pinching ? 12 : 20, 0, 7); g.stroke();
      if (pinching) { g.fillStyle = "rgba(61,220,151,.5)"; g.fill(); }
    }
  }
}

export const app = new App();
app.init();
