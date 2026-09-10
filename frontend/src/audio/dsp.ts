/** Streaming, 95-tap Blackman low-pass filter with persistent phase and history. */
export class Resampler {
  readonly coefficients: Float64Array;
  private history = new Float32Array(95);
  private cursor = 0;
  private inputCount = 0;
  private outputCount = 0;
  private previous = 0;
  constructor(readonly inputRate: number, readonly outputRate = 16000) {
    if (inputRate < outputRate || !Number.isFinite(inputRate)) throw new Error('Unsupported capture sample rate');
    const taps = this.history.length;
    const cutoff = 0.43 * outputRate / inputRate;
    this.coefficients = new Float64Array(taps);
    let sum = 0;
    for (let i = 0; i < taps; i++) {
      const x = i - (taps - 1) / 2;
      const sinc = x === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x);
      const window = 0.42 - 0.5 * Math.cos(2 * Math.PI * i / (taps - 1)) + 0.08 * Math.cos(4 * Math.PI * i / (taps - 1));
      sum += this.coefficients[i] = sinc * window;
    }
    for (let i = 0; i < taps; i++) this.coefficients[i] /= sum;
  }
  push(sample: number): number | null {
    this.history[this.cursor] = sample;
    let filtered = 0;
    let index = this.cursor;
    for (let j = 0; j < this.coefficients.length; j++) {
      filtered += this.coefficients[j] * this.history[index];
      index = index === 0 ? this.history.length - 1 : index - 1;
    }
    this.cursor = (this.cursor + 1) % this.history.length;
    this.inputCount++;
    const next = (this.outputCount + 1) * this.inputRate / this.outputRate;
    let result: number | null = null;
    if (this.inputCount + 1e-9 >= next) {
      const fraction = Math.max(0, Math.min(1, next - (this.inputCount - 1)));
      result = this.previous + fraction * (filtered - this.previous);
      this.outputCount++;
    }
    this.previous = filtered;
    return result;
  }
}

export function pcmFrame(samples: Int16Array, sequence: number, offset: bigint, discontinuity = false): ArrayBuffer {
  const buffer = new ArrayBuffer(24 + samples.length * 2);
  const view = new DataView(buffer);
  view.setUint32(0, 0x314e5254, true); view.setUint8(4, 1); view.setUint8(5, 1);
  view.setUint16(6, discontinuity ? 1 : 0, true); view.setUint32(8, sequence, true);
  view.setBigUint64(12, offset, true); view.setUint32(20, samples.length, true);
  for (let i = 0; i < samples.length; i++) view.setInt16(24 + i * 2, samples[i], true);
  return buffer;
}
export function readFrame(buffer: ArrayBuffer) {
  if (buffer.byteLength < 26 || buffer.byteLength > 4096) throw new Error('Invalid PCM length');
  const view = new DataView(buffer); const count = view.getUint32(20, true);
  if (view.getUint32(0, true) !== 0x314e5254 || view.getUint8(4) !== 1 || view.getUint8(5) !== 1 || (view.getUint16(6, true) & ~1) || count < 1 || count > 1600 || buffer.byteLength !== 24 + count * 2) throw new Error('Invalid PCM format');
  const samples = new Float32Array(count);
  for (let i = 0; i < count; i++) samples[i] = view.getInt16(24 + 2 * i, true) / 32768;
  return { samples, sequence: view.getUint32(8, true), offset: view.getBigUint64(12, true), discontinuity: Boolean(view.getUint16(6, true) & 1) };
}
