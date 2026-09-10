import { useSyncExternalStore } from 'react';
import { z } from 'zod';
import { applyCaption, bootstrapSchema, captionSchema, snapshotSchema, type Bootstrap, type Snapshot } from './model';

export class ApiError extends Error { constructor(message: string, public status: number, public code = '') { super(message); } }
let csrf = '';
export const requestId = () => crypto.randomUUID();
export async function api<T = unknown>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(path, { method, credentials: 'same-origin', headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(csrf ? { 'X-CSRF-Token': csrf } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = z.object({ error: z.object({ message: z.string(), code: z.string().optional() }) }).safeParse(data);
    throw new ApiError(error.success ? error.data.error.message : `接続先が応答できませんでした（${response.status}）`, response.status, error.success ? error.data.error.code : '');
  }
  return data as T;
}
interface State { bootstrap: Bootstrap | null; snapshot: Snapshot | null; connection: string; error: string; ready: boolean }
class SessionStore {
  private state: State = { bootstrap: null, snapshot: null, connection: 'connecting', error: '', ready: false };
  private listeners = new Set<() => void>();
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  get = () => this.state;
  update(update: Partial<State>) { this.state = { ...this.state, ...update }; this.listeners.forEach(fn => fn()); }
  snapshot(value: unknown) {
    const next = value === null ? null : snapshotSchema.parse(value);
    const old = this.state.snapshot;
    if (next && old && old.session_id === next.session_id && (next.session_epoch < old.session_epoch || (next.session_epoch === old.session_epoch && (next.last_event_seq < old.last_event_seq || next.revision < old.revision)))) return;
    if (next) next.captions = Object.fromEntries(Object.entries(next.captions).slice(-100));
    this.update({ snapshot: next });
  }
}
export const store = new SessionStore();
export const useSession = () => useSyncExternalStore(store.subscribe, store.get);
let bootPromise: Promise<void> | undefined;
let socket: WebSocket | undefined;
let retryTimer: ReturnType<typeof setTimeout> | undefined;
let heartbeat: ReturnType<typeof setInterval> | undefined;
let attempt = 0;
let generation = 0;
let disposed = false;
const pending = new Map<string, { resolve: () => void; reject: (err: Error) => void; timer: ReturnType<typeof setTimeout> }>();
export const wsURL = (path: string) => `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${path}`;

export async function initialize() {
  if (bootPromise) return bootPromise;
  bootPromise = (async () => {
    try {
      const fragment = new URLSearchParams(location.hash.slice(1));
      const token = fragment.get('bootstrap');
      const invite = fragment.get('invite');
      if (token || invite) history.replaceState(null, '', location.pathname + location.search);
      const participant = ['/join', '/b'].includes(location.pathname);
      if (token) { const data = await api<{ csrf_token: string }>('/api/local/bootstrap', 'POST', { token }); csrf = data.csrf_token; }
      if (invite) { const data = await api<{ csrf_token: string; snapshot: unknown }>('/api/v1/join', 'POST', { token: invite }); csrf = data.csrf_token; store.snapshot(data.snapshot); }
      const bootstrap = bootstrapSchema.parse(await api(participant ? '/api/v1/bootstrap' : '/api/local/bootstrap'));
      csrf = bootstrap.csrf_token || csrf;
      if (bootstrap.session !== undefined) store.snapshot(bootstrap.session);
      if (bootstrap.surface === 'participant' && bootstrap.authenticated && bootstrap.session_id) store.snapshot(await api(`/api/v1/sessions/${bootstrap.session_id}/snapshot`));
      store.update({ bootstrap, ready: true, connection: 'disconnected', error: '' });
      if (bootstrap.surface === 'local' || bootstrap.authenticated) connect();
    } catch (error) { store.update({ error: error instanceof ApiError && error.status === 401 ? 'アプリからブラウザを開き直してください。参加者は新しい招待リンクで参加してください。' : error instanceof Error ? error.message : '接続できませんでした。', ready: true, connection: 'auth_required' }); }
  })();
  return bootPromise;
}

function connect() {
  if (disposed) return;
  clearTimeout(retryTimer); clearInterval(heartbeat);
  const ownGeneration = ++generation;
  socket?.close();
  const state = store.get();
  const path = state.bootstrap?.surface === 'local' ? '/ws/local/events' : `/ws/v1/sessions/${state.snapshot?.session_id}/events`;
  store.update({ connection: attempt ? 'reconnecting' : 'connecting' });
  socket = new WebSocket(wsURL(path));
  socket.onopen = () => {
    if (ownGeneration !== generation) return;
    attempt = 0;
    // A connection only becomes ready after the server's atomic snapshot arrives.
    heartbeat = setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'command.ping', request_id: requestId() })); }, 3000);
  };
  socket.onmessage = ({ data }) => {
    if (ownGeneration !== generation || typeof data !== 'string') return;
    try {
      const event = z.object({ type: z.string(), data: z.unknown().optional(), request_id: z.string().optional(), error: z.object({ message: z.string() }).optional() }).passthrough().parse(JSON.parse(data));
      if (event.type === 'session.snapshot') { store.snapshot(event.data); store.update({ connection: 'connected', error: '' }); }
      if (event.type === 'caption.upsert') {
        const caption = z.object({ protocol_version: z.literal(1), session_id: z.string(), session_epoch: z.number(), event_seq: z.number(), data: captionSchema }).parse(event);
        store.update({ snapshot: applyCaption(store.get().snapshot, caption) });
      }
      if (event.request_id && pending.has(event.request_id) && ['command.completed', 'command.failed'].includes(event.type)) {
        const item = pending.get(event.request_id)!; clearTimeout(item.timer); pending.delete(event.request_id);
        if (event.type === 'command.failed') item.reject(new Error(event.error?.message || '変更できませんでした。')); else item.resolve();
      }
    } catch { store.update({ error: '受信データを確認できませんでした。再接続してください。' }); }
  };
  socket.onclose = ({ code }) => {
    if (ownGeneration !== generation || disposed) return;
    clearInterval(heartbeat);
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('接続が切れました。設定の反映状態を確認してください。')); } pending.clear();
    if ([1008, 4001, 4003, 4401, 4403].includes(code)) { store.update({ connection: 'auth_required', error: '参加資格が失効しました。新しい招待リンクで参加してください。' }); return; }
    store.update({ connection: 'reconnecting' });
    const delay = Math.min(8000, 500 * 2 ** attempt++) * (0.8 + Math.random() * 0.4);
    retryTimer = setTimeout(connect, delay);
  };
  socket.onerror = () => { if (ownGeneration === generation) socket?.close(); };
}
export function reconnect() { if (store.get().bootstrap && store.get().connection !== 'auth_required') connect(); else { bootPromise = undefined; void initialize(); } }
window.addEventListener('online', () => { if (store.get().connection === 'reconnecting') connect(); });
export function disconnect() { disposed = true; generation++; clearTimeout(retryTimer); clearInterval(heartbeat); socket?.close(); }
window.addEventListener('pagehide', disconnect);
export function command(name: string, data: unknown): Promise<void> {
  if (socket?.readyState !== WebSocket.OPEN || store.get().connection !== 'connected') return Promise.reject(new Error('接続が戻ってから操作してください。'));
  const request_id = requestId();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(request_id); reject(new Error('処理の完了を確認できませんでした。現在の状態を確認してください。')); }, 15000);
    pending.set(request_id, { resolve, reject, timer });
    socket!.send(JSON.stringify({ type: `command.${name}`, request_id, expected_revision: store.get().snapshot?.revision, data }));
  });
}
