// Hand gesture engine. Pure functions + one stateful class; no DOM, no camera, so it is unit-testable in Node.
// Input: 21 MediaPipe hand landmarks per frame ({x,y,z}, normalised to the image). Output: gesture events.

export const LM = {wrist: 0, thumbIp: 3, thumbTip: 4, indexMcp: 5, indexPip: 6, indexTip: 8,
  middleMcp: 9, middlePip: 10, middleTip: 12, ringMcp: 13, ringPip: 14, ringTip: 16, pinkyMcp: 17, pinkyPip: 18, pinkyTip: 20};

export const DEFAULTS = {
  aspect: 16 / 9,        // video width / height, so distances are not stretched
  mirror: true,          // the preview is mirrored, so moving your hand right moves the pointer right
  pinchOn: 0.30,         // pinch closes below this (in palm sizes) ...
  pinchOff: 0.50,        // ... and opens above this (hysteresis stops flicker)
  stableMs: 90,          // a pose must persist this long before it is reported
  holdMs: 700,           // hold a pose this long for a "hold" event
  swipeDist: 0.22,       // palm travel (fraction of the image) that counts as a swipe
  swipeWindowMs: 450,
  swipeCooldownMs: 900,
  lostMs: 250,           // hand missing this long = gone
  clickMaxMs: 400,       // pinch released within this = a click
  clickMaxMove: 0.04,
};

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const smooth = (e0, e1, x) => { const t = clamp((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t); };

export function dist(a, b, aspect = 1) {
  return Math.hypot((a.x - b.x) * aspect, a.y - b.y);
}

export function palmSize(lm, aspect = 1) { return dist(lm[LM.wrist], lm[LM.middleMcp], aspect) || 1e-6; }

export function pinchRatio(lm, aspect = 1) {
  return dist(lm[LM.thumbTip], lm[LM.indexTip], aspect) / palmSize(lm, aspect);
}

// A finger is "extended" when its tip is clearly farther from the wrist than its middle joint.
function extended(lm, tip, pip, aspect) {
  return dist(lm[LM.wrist], lm[tip], aspect) > dist(lm[LM.wrist], lm[pip], aspect) * 1.1;
}

export function fingerStates(lm, aspect = 1) {
  return {
    index: extended(lm, LM.indexTip, LM.indexPip, aspect),
    middle: extended(lm, LM.middleTip, LM.middlePip, aspect),
    ring: extended(lm, LM.ringTip, LM.ringPip, aspect),
    pinky: extended(lm, LM.pinkyTip, LM.pinkyPip, aspect),
    // thumb: tip is farther from the pinky base than the thumb's own middle joint
    thumb: dist(lm[LM.thumbTip], lm[LM.pinkyMcp], aspect) > dist(lm[LM.thumbIp], lm[LM.pinkyMcp], aspect) * 1.1,
  };
}

// Pose from which fingers are up (thumb ignored; it is used for pinching).
export function classifyPose(lm, aspect = 1) {
  const f = fingerStates(lm, aspect);
  const n = [f.index, f.middle, f.ring, f.pinky].filter(Boolean).length;
  if (n === 4) return "open_palm";
  if (n === 0) return "fist";
  if (n === 1 && f.index) return "point";
  if (n === 2 && f.index && f.middle) return "peace";
  return "other";
}

export function palmCenter(lm) {
  const ids = [LM.wrist, LM.indexMcp, LM.middleMcp, LM.ringMcp, LM.pinkyMcp];
  return {x: ids.reduce((s, i) => s + lm[i].x, 0) / ids.length, y: ids.reduce((s, i) => s + lm[i].y, 0) / ids.length};
}

// One Euro filter: heavy smoothing when slow (no jitter), light when fast (no lag).
class OneEuro {
  constructor(minCutoff = 1.2, beta = 8) { this.minCutoff = minCutoff; this.beta = beta; this.x = null; this.dx = 0; this.t = null; }
  static alpha(cutoff, dt) { const tau = 1 / (2 * Math.PI * cutoff); return 1 / (1 + tau / dt); }
  reset() { this.x = null; this.t = null; this.dx = 0; }
  step(v, tMs) {
    if (this.x === null) { this.x = v; this.t = tMs; return v; }
    const dt = Math.max((tMs - this.t) / 1000, 1e-3); this.t = tMs;
    const d = (v - this.x) / dt;
    this.dx += OneEuro.alpha(1, dt) * (d - this.dx);
    this.x += OneEuro.alpha(this.minCutoff + this.beta * Math.abs(this.dx), dt) * (v - this.x);
    return this.x;
  }
}

export class GestureEngine {
  constructor(opts = {}) {
    this.o = {...DEFAULTS, ...opts};
    this.fx = new OneEuro(); this.fy = new OneEuro();
    this.reset();
  }

  reset() {
    this.state = {visible: false, pose: "none", pinching: false, pointer: null};
    this.fx.reset(); this.fy.reset();
    this._cand = null; this._candSince = 0; this._poseSince = 0; this._held = false;
    this._trail = []; this._lastSwipe = -1e9; this._lastSeen = -1e9;
    this._pinchStart = null;
  }

  // Feed one frame (landmarks or null) with a millisecond timestamp. Returns an array of events.
  update(lm, t) {
    const ev = [], o = this.o, s = this.state;
    if (!lm) {
      if (s.visible && t - this._lastSeen > o.lostMs) {
        if (s.pinching) ev.push({type: "pinch_up", ...s.pointer, t});
        if (s.pose !== "none") ev.push({type: "pose", name: "none", t});
        this.reset(); ev.push({type: "lost", t});
      }
      return ev;
    }
    this._lastSeen = t;
    const flip = (p) => ({x: o.mirror ? 1 - p.x : p.x, y: p.y});
    if (!s.visible) { s.visible = true; ev.push({type: "found", t}); }

    // --- pinch with hysteresis ---
    const ratio = pinchRatio(lm, o.aspect);
    const wasPinching = s.pinching;
    // A fist also brings thumb and index tips together, so a pinch only starts when the index is not curled into the palm.
    const indexCurled = dist(lm[LM.wrist], lm[LM.indexTip], o.aspect) < dist(lm[LM.wrist], lm[LM.indexPip], o.aspect) * 0.75;
    if (!s.pinching && ratio < o.pinchOn && !indexCurled) s.pinching = true;
    else if (s.pinching && ratio > o.pinchOff) s.pinching = false;

    // --- pointer: index tip, blending toward the thumb/index midpoint as the fingers close (stable while clicking) ---
    const tip = flip(lm[LM.indexTip]), th = flip(lm[LM.thumbTip]);
    const w = smooth(0.9, 0.3, ratio);
    const raw = {x: tip.x + (((tip.x + th.x) / 2) - tip.x) * w, y: tip.y + (((tip.y + th.y) / 2) - tip.y) * w};
    s.pointer = {x: clamp(this.fx.step(raw.x, t), 0, 1), y: clamp(this.fy.step(raw.y, t), 0, 1)};

    if (s.pinching && !wasPinching) { this._pinchStart = {t, ...s.pointer}; ev.push({type: "pinch_down", ...s.pointer, t}); }
    if (!s.pinching && wasPinching) {
      ev.push({type: "pinch_up", ...s.pointer, t});
      const p0 = this._pinchStart;
      if (p0 && t - p0.t <= o.clickMaxMs && Math.hypot(s.pointer.x - p0.x, s.pointer.y - p0.y) <= o.clickMaxMove) {
        ev.push({type: "click", ...s.pointer, t});
      }
      this._pinchStart = null;
    }

    // --- pose, debounced ---
    const pose = s.pinching ? "pinch" : classifyPose(lm, o.aspect);
    if (pose !== this._cand) { this._cand = pose; this._candSince = t; }
    if (pose !== s.pose && t - this._candSince >= o.stableMs) {
      s.pose = pose; this._poseSince = t; this._held = false;
      ev.push({type: "pose", name: pose, t});
    }
    if (!this._held && s.pose !== "none" && s.pose !== "other" && s.pose !== "pinch" && t - this._poseSince >= o.holdMs) {
      this._held = true; ev.push({type: "hold", pose: s.pose, t});
    }

    // --- swipe: a fast, mostly straight palm movement while the hand is open ---
    const c = flip(palmCenter(lm));
    this._trail.push({...c, t, open: s.pose === "open_palm"});
    while (this._trail.length && t - this._trail[0].t > o.swipeWindowMs) this._trail.shift();
    if (t - this._lastSwipe > o.swipeCooldownMs && this._trail.length >= 4) {
      const a = this._trail[0], b = this._trail[this._trail.length - 1];
      const openShare = this._trail.filter((p) => p.open).length / this._trail.length;
      const dx = (b.x - a.x), dy = (b.y - a.y);
      if (openShare >= 0.7 && Math.max(Math.abs(dx), Math.abs(dy)) >= o.swipeDist) {
        const horizontal = Math.abs(dx) > Math.abs(dy) * 2, vertical = Math.abs(dy) > Math.abs(dx) * 2;
        if (horizontal || vertical) {
          const dir = horizontal ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up");
          this._lastSwipe = t; this._trail = [];
          ev.push({type: "swipe", dir, t});
        }
      }
    }
    return ev;
  }
}
