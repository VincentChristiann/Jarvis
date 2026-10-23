import test from "node:test";
import assert from "node:assert/strict";
import {GestureEngine, classifyPose, pinchRatio, fingerStates} from "../../jarvis/hub/web/hologram/gestures.js";
import {hand, OPEN, shift} from "./hand.mjs";

const A = 1; // square test image
const run = (engine, frames, start = 0, step = 33) => frames.flatMap((f, i) => engine.update(f, start + i * step));
const names = (ev, type) => ev.filter((e) => e.type === type);

test("classifies the basic poses", () => {
  assert.equal(classifyPose(hand({fingers: OPEN}), A), "open_palm");
  assert.equal(classifyPose(hand({fingers: OPEN, thumb: true}), A), "open_palm");
  assert.equal(classifyPose(hand({}), A), "fist");
  assert.equal(classifyPose(hand({fingers: {index: true}}), A), "point");
  assert.equal(classifyPose(hand({fingers: {index: true, middle: true}}), A), "peace");
  assert.equal(classifyPose(hand({fingers: {index: true, ring: true}}), A), "other");
});

test("finger state detection", () => {
  const f = fingerStates(hand({fingers: {index: true, pinky: true}, thumb: true}), A);
  assert.deepEqual(f, {index: true, middle: false, ring: false, pinky: true, thumb: true});
});

test("pose is debounced: a single odd frame does not change it", () => {
  const e = new GestureEngine({aspect: A});
  const open = hand({fingers: OPEN}), fist = hand({});
  run(e, Array(10).fill(open));
  assert.equal(e.state.pose, "open_palm");
  run(e, [fist], 400);
  assert.equal(e.state.pose, "open_palm");           // one frame of fist is noise
  run(e, Array(6).fill(fist), 433);
  assert.equal(e.state.pose, "fist");
});

test("pinch has hysteresis and emits a click for a quick tap", () => {
  const e = new GestureEngine({aspect: A});
  const open = hand({fingers: {index: true}}), pinch = hand({fingers: {index: true}, pinch: true});
  assert.ok(pinchRatio(pinch, A) < 0.3 && pinchRatio(open, A) > 0.5);
  const ev = run(e, [...Array(5).fill(open), ...Array(4).fill(pinch), ...Array(5).fill(open)]);
  assert.equal(names(ev, "pinch_down").length, 1);
  assert.equal(names(ev, "pinch_up").length, 1);
  assert.equal(names(ev, "click").length, 1);
});

test("a long pinch is not a click (it is a drag/hold)", () => {
  const e = new GestureEngine({aspect: A});
  const open = hand({fingers: {index: true}}), pinch = hand({fingers: {index: true}, pinch: true});
  const ev = run(e, [...Array(3).fill(open), ...Array(30).fill(pinch), ...Array(5).fill(open)]);
  assert.equal(names(ev, "click").length, 0);
  assert.equal(names(ev, "pinch_up").length, 1);
});

test("pinching while moving a lot is not a click", () => {
  const e = new GestureEngine({aspect: A});
  const base = hand({fingers: {index: true}, pinch: true});
  const frames = [hand({fingers: {index: true}}), hand({fingers: {index: true}}), base,
    shift(base, 0.1, 0), shift(base, 0.2, 0), hand({fingers: {index: true}}), hand({fingers: {index: true}})];
  assert.equal(names(run(e, frames, 0, 40), "click").length, 0);
});

test("swipes in four directions, in the user's frame (mirrored)", () => {
  for (const [dx, dy, dir] of [[0.3, 0, "right"], [-0.3, 0, "left"], [0, -0.3, "up"], [0, 0.3, "down"]]) {
    const e = new GestureEngine({aspect: A});
    const base = hand({fingers: OPEN, at: {x: 0.5, y: 0.6}});
    // Raw camera x is mirrored: a hand moving to the user's right appears to move toward smaller raw x.
    const frames = Array.from({length: 10}, (_, i) => shift(base, -dx * i / 9 * (dx ? 1 : 0), dy * i / 9));
    const sw = names(run(e, frames, 0, 30), "swipe");
    assert.equal(sw.length, 1, dir);
    assert.equal(sw[0].dir, dir);
  }
});

test("moving a pointing hand never swipes", () => {
  const e = new GestureEngine({aspect: A});
  const base = hand({fingers: {index: true}, at: {x: 0.3, y: 0.6}});
  const frames = Array.from({length: 12}, (_, i) => shift(base, 0.04 * i, 0));
  assert.equal(names(run(e, frames, 0, 30), "swipe").length, 0);
});

test("slow drift is not a swipe, and a swipe has a cooldown", () => {
  const e = new GestureEngine({aspect: A});
  const base = hand({fingers: OPEN});
  const slow = Array.from({length: 40}, (_, i) => shift(base, -0.006 * i, 0));
  assert.equal(names(run(e, slow, 0, 100), "swipe").length, 0);
  const e2 = new GestureEngine({aspect: A});
  const fast = Array.from({length: 24}, (_, i) => shift(base, -0.03 * i, 0));   // keeps going: only one swipe per cooldown
  assert.equal(names(run(e2, fast, 0, 30), "swipe").length, 1);
});

test("holding a pose emits one hold event", () => {
  const e = new GestureEngine({aspect: A});
  const ev = run(e, Array(40).fill(hand({fingers: OPEN})), 0, 33);
  const holds = names(ev, "hold");
  assert.equal(holds.length, 1);
  assert.equal(holds[0].pose, "open_palm");
  assert.ok(holds[0].t >= 700);
});

test("losing the hand releases a pinch and resets", () => {
  const e = new GestureEngine({aspect: A});
  const pinch = hand({fingers: {index: true}, pinch: true});
  run(e, Array(6).fill(pinch));
  assert.equal(e.state.pinching, true);
  const ev = run(e, Array(15).fill(null), 300, 33);
  assert.equal(names(ev, "pinch_up").length, 1);
  assert.equal(names(ev, "lost").length, 1);
  assert.equal(e.state.visible, false);
  assert.equal(e.state.pointer, null);
});

test("pointer follows the index fingertip, mirrored, within 0..1", () => {
  const e = new GestureEngine({aspect: A});
  const left = hand({fingers: {index: true}, at: {x: 0.2, y: 0.8}});
  run(e, Array(30).fill(left));
  assert.ok(e.state.pointer.x > 0.7, "hand on the camera's left appears on the user's right");
  const edge = hand({fingers: {index: true}, at: {x: -0.4, y: 0.8}});
  run(e, Array(30).fill(edge), 2000);
  assert.ok(e.state.pointer.x <= 1 && e.state.pointer.x >= 0);
});

test("pointer stays steady while pinching to click", () => {
  const e = new GestureEngine({aspect: A});
  const open = hand({fingers: {index: true}});
  run(e, Array(20).fill(open));
  const before = {...e.state.pointer};
  run(e, Array(12).fill(hand({fingers: {index: true}, pinch: true})), 700);
  const after = e.state.pointer;
  assert.ok(Math.hypot(after.x - before.x, after.y - before.y) < 0.06, "pinching must not throw the cursor");
});

test("aspect ratio is applied to distances", () => {
  const lm = hand({fingers: {index: true}, pinch: true});
  assert.ok(pinchRatio(lm, 16 / 9) > 0 && Number.isFinite(pinchRatio(lm, 16 / 9)));
});
