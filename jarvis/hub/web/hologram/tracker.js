// Camera + MediaPipe hand (and optional face) tracking. Everything is served from this hub: no internet needed.
import {FilesetResolver, HandLandmarker, FaceLandmarker} from "/vendor/mediapipe/vision_bundle.mjs";

export class Tracker {
  constructor(video) { this.video = video; this.stream = null; this.hand = null; this.face = null; this.lastTime = -1; this.n = 0; }

  async load({face = false} = {}) {
    if (this.hand) return;
    const fileset = await FilesetResolver.forVisionTasks("/vendor/mediapipe/wasm");
    const make = (Cls, model, extra, delegate) => Cls.createFromOptions(fileset,
      {baseOptions: {modelAssetPath: model, delegate}, runningMode: "VIDEO", ...extra});
    for (const delegate of ["GPU", "CPU"]) {          // GPU when the machine has it, CPU otherwise
      try { this.hand = await make(HandLandmarker, "/vendor/models/hand_landmarker.task", {numHands: 1}, delegate); this.delegate = delegate; break; }
      catch (e) { if (delegate === "CPU") throw e; }
    }
    if (face) {
      try { this.face = await make(FaceLandmarker, "/vendor/models/face_landmarker.task", {numFaces: 1}, this.delegate); }
      catch (e) { this.face = null; }                  // head tracking is a bonus; hands still work
    }
  }

  async startCamera() {
    this.stream = await navigator.mediaDevices.getUserMedia({video: {width: {ideal: 640}, height: {ideal: 480}, facingMode: "user"}, audio: false});
    this.video.srcObject = this.stream;
    await this.video.play();
  }

  get running() { return !!this.stream; }

  stopCamera() {
    if (this.stream) for (const t of this.stream.getTracks()) t.stop();   // releases the camera light
    this.stream = null; this.video.srcObject = null; this.lastTime = -1;
  }

  // Returns {hand: landmarks|null, head: {x,y}|null, aspect} for a new video frame, or null if there is no new frame.
  detect(t) {
    const v = this.video;
    if (!this.stream || v.readyState < 2 || v.currentTime === this.lastTime) return null;
    this.lastTime = v.currentTime;
    const r = this.hand.detectForVideo(v, t);
    let head = this.head || null;
    if (this.face && (this.n++ % 3 === 0)) {
      const f = this.face.detectForVideo(v, t + 0.01);
      const nose = f.faceLandmarks?.[0]?.[1];
      head = this.head = nose ? {x: 1 - nose.x, y: nose.y} : null;
    }
    return {hand: r.landmarks?.[0] || null, head, aspect: v.videoWidth / v.videoHeight || 4 / 3};
  }
}
