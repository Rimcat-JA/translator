import { useSyncExternalStore } from 'react';
import { api, requestId, wsURL } from '../client';
import type { Snapshot } from '../model';
import { pcmFrame, readFrame } from './dsp';
import workletURL from './worklet.ts?worker&url';

interface AudioState { mic: string; playback: string; level: number; queueMs: number; dropped: number; error: string; inputRate: number | null }
export class AudioEngine {
  private state: AudioState = { mic: 'not_requested', playback: 'not_requested', level: 0, queueMs: 0, dropped: 0, error: '', inputRate: null };
  private listeners = new Set<() => void>();
  private context?: AudioContext;
  private moduleReady?: Promise<void>;
  private stream?: MediaStream;
  private capture?: AudioWorkletNode;
  private source?: MediaStreamAudioSourceNode;
  private player?: AudioWorkletNode;
  private gain?: GainNode;
  private input?: WebSocket;
  private output?: WebSocket;
  private generation = 0;
  private playbackGeneration = 0;
  private lease = '';
  private sessionId = '';
  private renew?: ReturnType<typeof setInterval>;
  private muteTimer?: ReturnType<typeof setTimeout>;
  private lastMeter = 0;
  private volume = 0.8;
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  get = () => this.state;
  private update(update: Partial<AudioState>) { this.state = { ...this.state, ...update }; this.listeners.forEach(fn => fn()); }
  private audioContext() {
    if (!this.context || this.context.state === 'closed') {
      this.context = new AudioContext({ latencyHint: 'interactive' });
      this.moduleReady = this.context.audioWorklet.addModule(workletURL);
    }
    // Called synchronously from the user's start/test click, before any network await.
    void this.context.resume();
    return this.context;
  }
  async start(snapshot: Snapshot, deviceId = '') {
    if (['requesting_permission', 'ready', 'streaming', 'muted'].includes(this.state.mic)) return;
    const generation = ++this.generation;
    this.sessionId = snapshot.session_id;
    this.update({ mic: 'requesting_permission', error: '' });
    try {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error('マイクの利用には HTTPS または同じ PC の localhost が必要です。');
      const context = this.audioContext();
      const permission = navigator.mediaDevices.getUserMedia({ audio: { ...(deviceId ? { deviceId: { exact: deviceId } } : {}), channelCount: { ideal: 1 }, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      // Playback can remain available even when microphone permission is denied.
      void this.startPlayback(snapshot).catch(error => this.update({ playback: 'error', error: error instanceof Error ? error.message : '音声再生を開始できませんでした。' }));
      const stream = await permission;
      if (generation !== this.generation) { stream.getTracks().forEach(track => track.stop()); return; }
      this.stream = stream;
      stream.getTracks().forEach(track => { track.onended = () => { void this.stopMic(); this.update({ mic: 'missing', error: 'マイクが取り外されたか、使用許可が取り消されました。機器を確認して再開してください。' }); }; });
      this.update({ mic: 'ready', inputRate: context.sampleRate });
      const lease = await api<{ lease_id: string }>(`/api/v1/sessions/${snapshot.session_id}/publisher-lease`, 'POST', { request_id: requestId() });
      if (generation !== this.generation) { await api(`/api/v1/sessions/${snapshot.session_id}/publisher-lease`, 'POST', { request_id: requestId(), lease_id: lease.lease_id, release: true }); return; }
      this.lease = lease.lease_id;
      await this.moduleReady;
      if (generation !== this.generation) return;
      const socket = new WebSocket(wsURL(`/ws/v1/sessions/${snapshot.session_id}/audio-input`)); this.input = socket;
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(() => { socket.close(); reject(new Error('音声送信先の準備がタイムアウトしました。')); }, 12000);
        socket.onopen = () => socket.send(JSON.stringify({ type: 'stream.start', protocol_version: 1, stream_id: requestId(), session_epoch: snapshot.session_epoch, publisher_lease_id: lease.lease_id, source_kind: 'microphone', format: 'pcm_s16le', sample_rate: 16000, channels: 1, frame_samples: 320 }));
        socket.onmessage = ({ data }) => {
          try { const event = JSON.parse(data); if (event.type === 'stream.accepted') { clearTimeout(timeout); resolve(); } else if (event.error) { clearTimeout(timeout); reject(new Error(event.error.message || '音声の準備に失敗しました。')); } } catch { clearTimeout(timeout); reject(new Error('音声接続の応答が不正です。')); }
        };
        socket.onerror = () => { clearTimeout(timeout); reject(new Error('音声送信先に接続できませんでした。')); };
        socket.onclose = () => { clearTimeout(timeout); reject(new Error('音声接続が閉じられました。')); };
      });
      if (generation !== this.generation) { socket.close(); return; }
      this.capture = new AudioWorkletNode(context, 'translator-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      this.source = context.createMediaStreamSource(stream); this.source.connect(this.capture); this.capture.connect(context.destination);
      let sequence = 0; let discontinuity = true; let congestedAt = 0;
      const capture = this.capture;
      capture.port.onmessage = ({ data }) => {
        if (data.type !== 'frame' || generation !== this.generation) return;
        capture.port.postMessage({ type: 'credit' });
        if (performance.now() - this.lastMeter > 100) { this.lastMeter = performance.now(); this.update({ level: data.level }); }
        if (socket.readyState !== WebSocket.OPEN) return;
        if (socket.bufferedAmount > 3320) {
          discontinuity = true;
          if (!congestedAt) congestedAt = performance.now();
          if (socket.bufferedAmount > 6640 && performance.now() - congestedAt > 400) { void this.stopMic(); this.update({ error: '回線が混雑したためマイクを停止しました。接続を確認して再開してください。' }); }
          return;
        }
        congestedAt = 0;
        if (sequence >= 0xfffffff0) { void this.stopMic(); this.update({ error: '音声接続を更新するため、マイクを再開してください。' }); return; }
        socket.send(pcmFrame(new Int16Array(data.buffer), sequence++, BigInt(data.offset), discontinuity || data.discontinuity)); discontinuity = false;
      };
      capture.onprocessorerror = () => { void this.stopMic(); this.update({ mic: 'error', error: 'マイクの音声処理を再開してください。' }); };
      socket.onclose = () => { if (generation === this.generation) { void this.stopMic(); this.update({ error: '音声送信が切断されました。マイクを再開してください。' }); } };
      this.renew = setInterval(() => { void api(`/api/v1/sessions/${snapshot.session_id}/publisher-lease`, 'POST', { request_id: requestId(), lease_id: lease.lease_id }).catch(() => { void this.stopMic(); this.update({ error: 'マイク送信権が失効しました。再開してください。' }); }); }, 5000);
      this.update({ mic: 'streaming' });
    } catch (error) {
      if (generation !== this.generation) return;
      await this.stopMic();
      const name = error instanceof Error ? error.name : '';
      this.update({ mic: name === 'NotAllowedError' ? 'denied' : name === 'NotFoundError' ? 'missing' : 'error', error: name === 'NotAllowedError' ? 'マイクが許可されていません。ブラウザのサイト設定で許可して、もう一度開始してください。' : error instanceof Error ? error.message : 'マイクを開始できませんでした。' });
    }
  }
  async startPlayback(snapshot: Snapshot) {
    const playbackGeneration = ++this.playbackGeneration;
    const context = this.audioContext(); await this.moduleReady;
    if (playbackGeneration !== this.playbackGeneration || this.context !== context || context.state === 'closed') return;
    if (this.output && this.output.readyState <= WebSocket.OPEN) return;
    this.player?.disconnect(); this.gain?.disconnect();
    const player = new AudioWorkletNode(context, 'translator-playback', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
    this.player = player; this.gain = context.createGain(); this.gain.gain.value = this.volume; player.connect(this.gain); this.gain.connect(context.destination);
    let queuedSamples = 0; let sequence = -1; let expectedOffset: bigint | undefined;
    const output = new WebSocket(wsURL(`/ws/v1/sessions/${snapshot.session_id}/audio-output`)); this.output = output; output.binaryType = 'arraybuffer';
    this.update({ playback: 'connecting' });
    player.port.onmessage = ({ data }) => { if (this.player !== player) return; if (data.type === 'credit') queuedSamples = Math.max(0, queuedSamples - data.samples); if (data.type === 'metrics') this.update({ queueMs: data.queueMs, dropped: data.dropped, playback: data.playing ? 'playing' : 'buffering' }); };
    output.onmessage = ({ data }) => {
      if (this.output !== output) return;
      if (typeof data === 'string') { try { if (JSON.parse(data).type === 'stream.accepted') this.update({ playback: 'buffering' }); } catch { output.close(); } return; }
      try {
        const frame = readFrame(data);
        if (frame.discontinuity) { sequence = -1; expectedOffset = undefined; player.port.postMessage({ type: 'reset' }); }
        if (frame.sequence <= sequence) return;
        if ((sequence !== -1 && frame.sequence !== sequence + 1) || (expectedOffset !== undefined && expectedOffset !== frame.offset)) player.port.postMessage({ type: 'reset' });
        sequence = frame.sequence; expectedOffset = frame.offset + BigInt(frame.samples.length);
        if (queuedSamples + frame.samples.length > 3200) { output.close(); this.update({ error: '再生処理が混雑しました。音声受信を再開してください。' }); return; }
        queuedSamples += frame.samples.length; player.port.postMessage({ type: 'pcm', samples: frame.samples }, [frame.samples.buffer]);
      } catch { output.close(); this.update({ error: '受信した音声形式を確認できませんでした。' }); }
    };
    output.onclose = () => { player.port.postMessage({ type: 'reset' }); player.disconnect(); player.port.close(); if (this.output === output) this.update({ playback: 'disconnected', queueMs: 0 }); };
    output.onerror = () => { output.close(); };
  }
  mute() {
    const muted = this.state.mic !== 'muted'; this.capture?.port.postMessage({ type: 'mute', muted });
    this.update({ mic: muted ? 'muted' : 'streaming', level: 0 }); clearTimeout(this.muteTimer);
    if (muted) this.muteTimer = setTimeout(() => { void this.stopMic(); this.update({ error: '30 秒間のミュートで音声認識を停止しました。再開するときはマイクを開始してください。' }); }, 30000);
  }
  async stopMic() {
    this.generation++; clearInterval(this.renew); clearTimeout(this.muteTimer);
    this.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); }); this.stream = undefined;
    this.source?.disconnect(); this.source = undefined;
    if (this.capture) { this.capture.port.onmessage = null; this.capture.port.close(); this.capture.disconnect(); this.capture = undefined; }
    const socket = this.input; this.input = undefined;
    if (socket) { socket.onclose = null; if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'stream.stop' })); socket.close(); }
    const lease = this.lease; this.lease = '';
    this.update({ mic: 'not_requested', level: 0 });
    if (lease) await api(`/api/v1/sessions/${this.sessionId}/publisher-lease`, 'POST', { request_id: requestId(), lease_id: lease, release: true }).catch(() => undefined);
  }
  async stop() {
    this.playbackGeneration++;
    const stopping = this.stopMic(); this.output?.close(); this.output = undefined; this.player?.disconnect(); this.player?.port.close(); this.player = undefined; this.gain?.disconnect(); this.gain = undefined;
    const context = this.context; this.context = undefined; this.moduleReady = undefined; if (context && context.state !== 'closed') await context.close(); await stopping;
    this.update({ playback: 'not_requested', queueMs: 0 });
  }
  setVolume(volume: number) { this.volume = volume; if (this.gain) this.gain.gain.value = volume; }
  async testTone() {
    const context = this.audioContext(); const tone = context.createOscillator(); const gain = context.createGain();
    gain.gain.setValueAtTime(0, context.currentTime); gain.gain.linearRampToValueAtTime(0.07 * this.volume, context.currentTime + 0.02); gain.gain.linearRampToValueAtTime(0, context.currentTime + 0.25);
    tone.frequency.value = 440; tone.connect(gain); gain.connect(context.destination); tone.onended = () => { tone.disconnect(); gain.disconnect(); }; tone.start(); tone.stop(context.currentTime + 0.3);
  }
}
export const audioEngine = new AudioEngine();
export const useAudio = () => useSyncExternalStore(audioEngine.subscribe, audioEngine.get);
window.addEventListener('pagehide', () => { void audioEngine.stop(); });
