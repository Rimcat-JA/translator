import { afterEach, expect, it, vi } from 'vitest';
import { AudioEngine } from './engine';
import { snapshot } from '../test-fixtures';
afterEach(() => vi.unstubAllGlobals());
it('does not resurrect playback after exit during asynchronous worklet loading', async () => {
  let ready!: () => void;
  const loaded = new Promise<void>(resolve => { ready = resolve; });
  class Context { state = 'running'; audioWorklet = { addModule: () => loaded }; resume = vi.fn().mockResolvedValue(undefined); close = vi.fn(async () => { this.state = 'closed'; }); }
  const nodes = vi.fn(); const sockets = vi.fn(); vi.stubGlobal('AudioContext', Context); vi.stubGlobal('AudioWorkletNode', nodes); vi.stubGlobal('WebSocket', sockets);
  const engine = new AudioEngine(); const starting = engine.startPlayback(snapshot()); await engine.stop(); ready(); await starting;
  expect(nodes).not.toHaveBeenCalled(); expect(sockets).not.toHaveBeenCalled(); expect(engine.get().playback).toBe('not_requested');
});
it('releases a late microphone permission result after the user cancels', async () => {
  let permit!: (value: MediaStream) => void;
  const permission = new Promise<MediaStream>(resolve => { permit = resolve; });
  const track = { stop: vi.fn() };
  class Context { state = 'running'; audioWorklet = { addModule: async () => undefined }; resume = vi.fn().mockResolvedValue(undefined); close = vi.fn(async () => { this.state = 'closed'; }); }
  vi.stubGlobal('AudioContext', Context); vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn(() => permission) } });
  const engine = new AudioEngine(); vi.spyOn(engine, 'startPlayback').mockResolvedValue(undefined);
  const starting = engine.start(snapshot()); await engine.stop();
  permit({ getTracks: () => [track] } as unknown as MediaStream); await starting;
  expect(track.stop).toHaveBeenCalledOnce(); expect(engine.get().mic).toBe('not_requested');
});
