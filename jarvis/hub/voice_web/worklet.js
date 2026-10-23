// Runs on the audio thread: turns the microphone into 16 kHz, 16-bit mono chunks of 20 ms.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;      // input samples per output sample
    this.phase = 0; this.acc = 0; this.cnt = 0;
    this.out = new Int16Array(320); this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {       // box-filter decimation: cheap, and good enough for speech
      this.acc += ch[i]; this.cnt++; this.phase += 1;
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.acc / this.cnt));
        this.out[this.n++] = v < 0 ? v * 32768 : v * 32767;
        this.acc = 0; this.cnt = 0;
        if (this.n === 320) { this.port.postMessage(this.out.slice().buffer, []); this.n = 0; }
      }
    }
    return true;
  }
}
registerProcessor("jarvis-capture", Capture);
