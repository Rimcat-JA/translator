import { Resampler } from './dsp';
declare const sampleRate: number;
declare class AudioWorkletProcessor { readonly port: MessagePort }
declare function registerProcessor(name: string, processor: typeof AudioWorkletProcessor): void;

class CaptureProcessor extends AudioWorkletProcessor {
  private resampler = new Resampler(sampleRate);
  private frame = new Int16Array(320);
  private used = 0;
  private credits = 5;
  private muted = false;
  private discontinuity = true;
  private offset = 0;
  private energy = 0;
  constructor() {
    super();
    this.port.onmessage = ({ data }) => {
      if (data.type === 'credit') this.credits = Math.min(5, this.credits + 1);
      if (data.type === 'mute') this.muted = data.muted;
    };
  }
  process(inputs: Float32Array[][]): boolean {
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let mono = 0;
      for (let c = 0; c < channels.length; c++) mono += channels[c][i];
      const value = this.resampler.push(mono / channels.length);
      if (value === null) continue;
      const level = this.muted ? 0 : Math.max(-1, Math.min(1, value));
      this.energy += level * level;
      this.frame[this.used++] = Math.round(level * (level < 0 ? 32768 : 32767));
      if (this.used === 320) {
        if (this.credits > 0) {
          this.credits--;
          this.port.postMessage({ type: 'frame', buffer: this.frame.buffer, offset: this.offset, discontinuity: this.discontinuity, level: Math.sqrt(this.energy / 320) }, [this.frame.buffer]);
          this.frame = new Int16Array(320);
          this.discontinuity = false;
        } else this.discontinuity = true;
        this.offset += 320; this.used = 0; this.energy = 0;
      }
    }
    // The output remains silence: never monitor microphone into local speakers.
    return true;
  }
}
class PlaybackProcessor extends AudioWorkletProcessor {
  private ring = new Float32Array(3200);
  private read = 0;
  private length = 0;
  private phase = 0;
  private started = false;
  private fade = 0;
  private ticks = 0;
  private dropped = 0;
  constructor() {
    super();
    this.port.onmessage = ({ data }) => {
      if (data.type === 'reset') { this.read = 0; this.length = 0; this.phase = 0; this.started = false; this.fade = 0; }
      if (data.type !== 'pcm') return;
      const values: Float32Array = data.samples;
      for (let i = 0; i < values.length; i++) {
        if (this.length === this.ring.length) { this.read = (this.read + 1) % this.ring.length; this.length--; this.dropped++; this.fade = 0; }
        this.ring[(this.read + this.length) % this.ring.length] = values[i]; this.length++;
      }
      this.port.postMessage({ type: 'credit', samples: values.length });
    };
  }
  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const output = outputs[0][0];
    if (!this.started && this.length >= 960) { this.started = true; this.fade = 0; }
    for (let i = 0; i < output.length; i++) {
      if (!this.started || this.length < 2) { output[i] = 0; this.started = false; this.fade = 0; continue; }
      const a = this.ring[this.read]; const b = this.ring[(this.read + 1) % this.ring.length];
      this.fade = Math.min(1, this.fade + 1 / (sampleRate * 0.005));
      output[i] = (a + (b - a) * this.phase) * this.fade;
      this.phase += 16000 / sampleRate;
      while (this.phase >= 1 && this.length) { this.phase--; this.read = (this.read + 1) % this.ring.length; this.length--; }
    }
    if (++this.ticks >= Math.ceil(sampleRate / output.length / 10)) {
      this.ticks = 0; this.port.postMessage({ type: 'metrics', queueMs: this.length / 16, dropped: this.dropped, playing: this.started });
    }
    return true;
  }
}
registerProcessor('translator-capture', CaptureProcessor);
registerProcessor('translator-playback', PlaybackProcessor);
