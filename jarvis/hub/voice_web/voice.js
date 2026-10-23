// Microphone -> utterances -> hub -> spoken reply. Shared by the phone page, the dashboard and the hologram.
// The microphone is only open while a session is on, and sessions end by themselves.
const CHUNK_MS = 20;
const PREROLL = 10;                    // 200 ms kept from before speech starts
const START_CHUNKS = 3, END_SILENCE_MS = 700, MIN_SPEECH_MS = 400, MAX_UTTERANCE_MS = 12000;
const SESSION_MAX_MS = 5 * 60 * 1000, RENEW_MS = 20000;
const rms = (c) => { let s = 0; for (let i = 0; i < c.length; i++) s += c[i] * c[i]; return Math.sqrt(s / c.length) / 32768; };

const instances = new Set();

export class Voice {
  // endpoint: where audio is POSTed. setSession(on, renew): tell the hub the microphone is (or is no longer) in use.
  constructor({endpoint, setSession, headers = {}, getTarget = () => null, onState = () => {}, onResult = () => {}, onLevel = () => {}}) {
    Object.assign(this, {endpoint, setSession, headers, getTarget, onState, onResult, onLevel});
    this.speak = true; this.stream = null; this.ctx = null; this.mode = null; this.busy = false; this.mutedUntil = 0;
    instances.add(this);
  }

  get active() { return !!this.stream; }

  async _open() {
    if (this.stream) return;
    if (!(await this.setSession(true))) throw new Error("the hub did not allow the microphone");
    this.pre = []; this.speech = null; this.floor = 0.01; this.loud = 0; this.silence = 0;
    try {
      // Load the audio pipeline while the browser opens the microphone, so no speech is lost at the start.
      const loadWorklet = (async () => { this.ctx = new AudioContext(); await this.ctx.audioWorklet.addModule("/voice/worklet.js"); })();
      const [stream] = await Promise.all([
        navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}, video: false}),
        loadWorklet]);
      this.stream = stream;
      const src = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, "jarvis-capture");
      this.node.port.onmessage = (e) => this._chunk(new Int16Array(e.data));
      src.connect(this.node);
    } catch (e) { await this._close(); throw e; }
    this.renew = setInterval(() => this.setSession(true, true).then((ok) => { if (!ok) this.stop("the hub turned the microphone off"); }), RENEW_MS);
    this.limit = setTimeout(() => this.stop("listening time ended"), SESSION_MAX_MS);
  }

  async _close() {
    clearInterval(this.renew); clearTimeout(this.limit);
    if (this.stream) for (const t of this.stream.getTracks()) t.stop();     // releases the microphone
    this.stream = null;
    try { this.node?.disconnect(); await this.ctx?.close(); } catch (e) { /* already closed */ }
    this.ctx = this.node = null; this.speech = null; this.pre = [];
  }

  // ---- hands-free: listen continuously; the hub only acts on utterances that start with "Jarvis" ----
  async startHandsFree() {
    await this._open(); this.mode = "hands-free"; this.onState({on: true, mode: this.mode});
  }

  // ---- push to talk: the microphone is open only while the button is held ----
  async pttDown() {
    if (this.mode === "hands-free") return;
    this._held = true;
    await this._open();
    if (!this._held) { await this.stop("released"); return; }              // let go before the microphone finished opening
    this.mode = "ptt"; this.speech = this.speech || {chunks: [...this.pre], ptt: true}; this.onState({on: true, mode: this.mode});
  }
  async pttUp() {
    this._held = false;
    if (this.mode !== "ptt") return;
    await new Promise((r) => setTimeout(r, 200));                          // keep the last syllable
    const s = this.speech; this.speech = null; this.mode = null;
    await this._close();                                                   // the microphone hardware is released right away...
    try {
      if (s && s.chunks.length * CHUNK_MS >= MIN_SPEECH_MS) await this._send(s.chunks, true);   // ...then the audio is sent while the hub session is still on
    } finally {
      await this.setSession(false).catch(() => {});                       // and only then is the session ended
      this.onState({on: false, why: "done"});
    }
  }

  async stop(why = "stopped") {
    if (!this.stream && !this.mode) return;
    await this._close();
    this.mode = null;
    await this.setSession(false).catch(() => {});
    this.onState({on: false, why});
  }

  // ---- utterance detection (a small energy gate with an adaptive noise floor) ----
  _chunk(c) {
    const level = rms(c); this.onLevel(level);
    if (performance.now() < this.mutedUntil) return;                       // do not listen to our own spoken reply
    if (this._held && !this.speech && this.mode !== "hands-free") this.speech = {chunks: [...this.pre], ptt: true};   // button already down: start keeping audio now
    if (this.speech?.ptt) { this.speech.chunks.push(c); return; }
    if (this.mode !== "hands-free") { this._preroll(c); return; }
    const threshold = Math.max(0.02, this.floor * 3);
    if (!this.speech) {
      this._preroll(c);
      this.floor = 0.97 * this.floor + 0.03 * Math.max(level, 0.002);
      this.loud = level > threshold ? this.loud + 1 : 0;
      if (this.loud >= START_CHUNKS) { this.speech = {chunks: [...this.pre], ptt: false}; this.silence = 0; }
      return;
    }
    this.speech.chunks.push(c);
    this.silence = level > threshold * 0.6 ? 0 : this.silence + CHUNK_MS;
    const ms = this.speech.chunks.length * CHUNK_MS;
    if (this.silence >= END_SILENCE_MS || ms >= MAX_UTTERANCE_MS) {
      const s = this.speech; this.speech = null; this.loud = 0;
      if (ms - this.silence >= MIN_SPEECH_MS) this._send(s.chunks, false);
    }
  }
  _preroll(c) { this.pre.push(c); if (this.pre.length > PREROLL) this.pre.shift(); }

  async _send(chunks, ptt) {
    if (this.busy && !ptt) return;                                        // one at a time; do not queue background speech
    this.busy = true;
    try {
      const pcm = new Int16Array(chunks.reduce((n, c) => n + c.length, 0)); let o = 0;
      for (const c of chunks) { pcm.set(c, o); o += c.length; }
      const h = {...this.headers, "X-Jarvis": "1", "Content-Type": "application/octet-stream", "X-Jarvis-PTT": ptt ? "1" : "0"};
      const t = this.getTarget(); if (t) h["X-Jarvis-Target"] = t;
      const r = await fetch(this.endpoint, {method: "POST", headers: h, body: pcm.buffer});
      const out = await r.json().catch(() => ({}));
      if (r.status === 403 && !ptt) { this.stop("the hub turned the microphone off"); return; }
      if (out.error) { this.onResult({ok: false, reply: out.error}); return; }
      if (out.ignored && !out.reply) return;                              // overheard speech: show nothing
      this.onResult(out);
      if (out.reply && this.speak) this.say(out.reply);
    } finally { this.busy = false; }
  }

  say(text) {
    if (!("speechSynthesis" in window)) return;
    const u = new SpeechSynthesisUtterance(text);
    this.mutedUntil = performance.now() + 60000;                          // until the reply finishes
    u.onend = u.onerror = () => { this.mutedUntil = performance.now() + 400; };
    speechSynthesis.cancel(); speechSynthesis.speak(u);
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) for (const v of instances) if (v.active) v.stop("page hidden");
});
