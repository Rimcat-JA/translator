import { describe, expect, it } from 'vitest';
import { pcmFrame, readFrame, Resampler } from './dsp';
function signal(rate: number, frequency: number, seconds = 1) { const resampler = new Resampler(rate); const result: number[] = []; for (let i = 0; i < rate * seconds; i++) { const value = resampler.push(Math.sin(2 * Math.PI * frequency * i / rate)); if (value !== null) result.push(value); } return result; }
function rms(values: number[]) { return Math.sqrt(values.slice(300).reduce((sum, v) => sum + v * v, 0) / (values.length - 300)); }
describe('streaming resampler', () => {
  for (const rate of [44100, 48000]) {
    it(`${rate} Hz produces exact sample counts over 3 seconds`, () => { expect(signal(rate, 1000, 3)).toHaveLength(48000); });
    it(`${rate} Hz retains 1 kHz speech-band tone`, () => { expect(rms(signal(rate, 1000))).toBeGreaterThan(.69); expect(rms(signal(rate, 1000))).toBeLessThan(.72); });
    it(`${rate} Hz suppresses aliasing from 10/12 kHz by at least 45 dB`, () => { const pass = rms(signal(rate, 1000)); for (const frequency of [10000, 12000]) expect(20 * Math.log10(rms(signal(rate, frequency)) / pass)).toBeLessThan(-45); });
  }
  it('is continuous across arbitrary input chunk boundaries', () => { const resampler = new Resampler(44100); const output: number[] = []; for (let chunk = 0; chunk < 441; chunk++) for (let j = 0; j < 100; j++) { const i = chunk * 100 + j; const v = resampler.push(Math.sin(2 * Math.PI * 1000 * i / 44100)); if (v !== null) output.push(v); } expect(output).toEqual(signal(44100, 1000)); });
});
describe('TRN1 PCM protocol', () => {
  it('uses exact little-endian offsets and roundtrips clipped extremes', () => { const frame = pcmFrame(new Int16Array([-32768, 0, 32767]), 42, 1234567890123n, true); const bytes = new Uint8Array(frame); expect([...bytes.slice(0, 8)]).toEqual([84, 82, 78, 49, 1, 1, 1, 0]); const result = readFrame(frame); expect(result.sequence).toBe(42); expect(result.offset).toBe(1234567890123n); expect(result.discontinuity).toBe(true); expect([...result.samples]).toEqual([-1, 0, 32767 / 32768]); });
  it('rejects raw PCM, oversize and malformed frame counts', () => { expect(() => readFrame(new ArrayBuffer(640))).toThrow(); expect(() => readFrame(pcmFrame(new Int16Array(1601), 0, 0n))).toThrow(); const frame = pcmFrame(new Int16Array(320), 0, 0n); new DataView(frame).setUint32(20, 100, true); expect(() => readFrame(frame)).toThrow(); });
});
