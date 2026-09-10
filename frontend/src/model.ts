import { z } from 'zod';

export const captionSchema = z.object({
  utterance_id: z.string(), stt_epoch: z.number().int(), source_language: z.string(), target_language: z.string(),
  original: z.object({ text: z.string(), revision: z.number().int(), is_final: z.boolean() }),
  translation: z.object({ text: z.string(), status: z.enum(['pending', 'ready', 'disabled', 'same_language', 'error']), revision: z.number().int(), based_on_original_revision: z.number().int(), translation_epoch: z.number().int(), is_final: z.boolean(), error_code: z.string().nullable().optional() }),
});
export type Caption = z.infer<typeof captionSchema>;
export const snapshotSchema = z.object({
  session_id: z.string(), session_epoch: z.number().int(), revision: z.number().int(), status: z.string(), demo: z.boolean(), source_language: z.string(), target_language: z.string(), translation_enabled: z.boolean(), components: z.record(z.string(), z.string()), captions: z.record(z.string(), captionSchema), last_event_seq: z.number().int(),
});
export type Snapshot = z.infer<typeof snapshotSchema>;
export const settingsSchema = z.object({
  revision: z.number().default(0), source_language: z.string().default('zh'), target_language: z.string().default('ja'), translation_enabled: z.boolean().default(true), deepl_mode: z.enum(['free', 'pro']).default('free'), loopback_device_id: z.string().nullable().default(null), remote_domain: z.string().nullish().transform(value => value || ''), theme: z.string().default('system'), caption_font_size: z.number().default(32), show_original: z.boolean().default(true), secrets: z.record(z.string(), z.object({ configured: z.boolean(), storage: z.string().optional() })).default({}),
}).passthrough();
export type Settings = z.infer<typeof settingsSchema>;
export const languagesSchema = z.array(z.object({ code: z.string(), name_ja: z.string(), name_en: z.string().optional() }).passthrough());
export type Language = z.infer<typeof languagesSchema>[number];
export const bootstrapSchema = z.object({ protocol_version: z.literal(1), surface: z.enum(['local', 'participant']), csrf_token: z.string().optional(), authenticated: z.boolean().optional(), demo: z.boolean().optional(), env_available: z.boolean().optional(), session_id: z.string().optional(), settings: settingsSchema.optional(), session: snapshotSchema.nullable().optional(), languages: languagesSchema.optional(), capabilities: z.unknown().optional() });
export type Bootstrap = z.infer<typeof bootstrapSchema>;

/** Independent original/translation revisions, never arrival order, decide visible text. */
export function mergeCaption(previous: Caption | undefined, incoming: Caption): Caption {
  if (!previous) return incoming;
  if (incoming.stt_epoch < previous.stt_epoch) return previous;
  if (incoming.stt_epoch > previous.stt_epoch) return incoming;
  const original = incoming.original.revision > previous.original.revision && !(previous.original.is_final && !incoming.original.is_final) ? incoming.original : previous.original;
  const next = incoming.translation;
  const old = previous.translation;
  const generationChanged = next.translation_epoch > old.translation_epoch;
  const valid = incoming.source_language === previous.source_language && (generationChanged || incoming.target_language === previous.target_language) && next.translation_epoch >= old.translation_epoch && next.based_on_original_revision === original.revision && (generationChanged || (next.revision > old.revision && !(old.is_final && !next.is_final)));
  let translation = valid ? next : old;
  if (translation.based_on_original_revision !== original.revision) translation = { ...translation, text: '', status: 'pending', is_final: false };
  return { ...previous, original, translation, target_language: valid ? incoming.target_language : previous.target_language };
}

export function applyCaption(snapshot: Snapshot | null, event: { session_id: string; session_epoch: number; event_seq: number; data: Caption }): Snapshot | null {
  if (!snapshot || event.session_id !== snapshot.session_id || event.session_epoch !== snapshot.session_epoch || event.event_seq <= snapshot.last_event_seq) return snapshot;
  const captions = { ...snapshot.captions, [event.data.utterance_id]: mergeCaption(snapshot.captions[event.data.utterance_id], event.data) };
  const ids = Object.keys(captions);
  for (const id of ids.slice(0, Math.max(0, ids.length - 100))) delete captions[id];
  return { ...snapshot, captions, last_event_seq: event.event_seq };
}

export const componentLabels: Record<string, string> = { idle: '待機中', not_configured: '未設定', connecting: '接続中', ready: '準備完了', active: '通信中', streaming: '送信中', running: '通訳中', reconnecting: '再接続中', error: 'エラー', disabled: '停止中', not_requested: '未開始', requesting_permission: '許可を確認中', denied: 'マイク権限なし', missing: '機器が見つかりません', muted: 'ミュート中', waiting_for_peer: '相手の参加待ち', preparing: '準備中', stopping: '終了処理中', ended: '終了', degraded: '一部機能に問題', connected: '接続済み', disconnected: '接続なし', auth_required: '再認証が必要', buffering: '受信待ち', playing: '再生中' };
export const statusText = (value: string) => componentLabels[value] || value;
