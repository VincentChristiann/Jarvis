// Synthetic hand: 21 landmarks, palm facing the camera, fingers pointing up (y decreases upward).
const FINGER_X = {index: 0.52, middle: 0.56, ring: 0.60, pinky: 0.64};
const BASE = {index: 5, middle: 9, ring: 13, pinky: 17};

export function hand({fingers = {}, thumb = false, pinch = false, at = {x: 0.5, y: 0.8}} = {}) {
  const lm = Array.from({length: 21}, () => ({x: at.x, y: at.y, z: 0}));
  const P = (i, x, y) => { lm[i] = {x: at.x + (x - 0.5), y: at.y + (y - 0.8), z: 0}; };
  P(0, 0.5, 0.8);
  for (const [name, x] of Object.entries(FINGER_X)) {
    const b = BASE[name], up = fingers[name];
    P(b, x, 0.62);
    P(b + 1, x, 0.52);
    P(b + 2, x, up ? 0.44 : 0.58);
    P(b + 3, x, up ? 0.36 : 0.66);
  }
  P(1, 0.46, 0.76); P(2, 0.42, 0.70);
  if (thumb) { P(3, 0.37, 0.64); P(4, 0.32, 0.58); } else { P(3, 0.48, 0.68); P(4, 0.55, 0.66); }
  if (pinch) {                       // thumb tip meets index tip
    const t = lm[8];
    lm[4] = {x: t.x - 0.01, y: t.y + 0.01, z: 0};
    lm[3] = {x: t.x - 0.06, y: t.y + 0.08, z: 0};
  }
  return lm;
}
export const OPEN = {index: true, middle: true, ring: true, pinky: true};
export const shift = (lm, dx, dy) => lm.map((p) => ({...p, x: p.x + dx, y: p.y + dy}));
