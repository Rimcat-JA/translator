"""Streaming Gladia v2 adapter with an independent receive task and final drain.

API contract: https://docs.gladia.io/api-reference/v2/live/websocket
No network or background work is started by constructing this adapter.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
import json
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import websockets

from .base import EventBuffer, ProviderError, STTConfig, STTEvent, http_error, validate_pcm
from .languages import stt_language


class GladiaSTTProvider:
    def __init__(self, api_key: str, *, transport=None, connect=None, drain_timeout: float = 3.0):
        self._api_key = api_key
        self._transport = transport
        self._connect = connect or websockets.connect
        self._drain_timeout = drain_timeout
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._buffer = EventBuffer()
        self._finished = True
        self._closing = False
        self.connected = False

    async def start(self, config: STTConfig = STTConfig()) -> None:
        await self.close()
        if not self._api_key:
            raise ProviderError("GLADIA_NOT_CONFIGURED", "文字起こしサービスのAPIキーを設定してください。")
        if config.sample_rate != 16000 or config.channels != 1:
            raise ProviderError("INVALID_AUDIO_FORMAT", "文字起こしの音声形式は16 kHz・monoが必要です。")
        try:
            language = stt_language(config.language)
        except ValueError:
            raise ProviderError("UNSUPPORTED_LANGUAGE", "対応する入力言語を選択してください。") from None
        self._buffer = EventBuffer()
        self._ids: OrderedDict[tuple, str] = OrderedDict()
        self._active_id: str | None = None
        self._closing = False
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
                resp = await client.post(
                    "https://api.gladia.io/v2/live",
                    headers={"x-gladia-key": self._api_key},
                    json={
                        "encoding": "wav/pcm", "sample_rate": 16000,
                        "bit_depth": 16, "channels": 1,
                        "language_config": {"languages": [language], "code_switching": False},
                        "messages_config": {
                            "receive_partial_transcripts": True, "receive_final_transcripts": True,
                            "receive_errors": True, "receive_lifecycle_events": True,
                        },
                    },
                )
                if not resp.is_success:
                    raise http_error("GLADIA", resp.status_code)
                url = resp.json()["url"]
                parsed = urlparse(url)
                if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("Invalid session URL")
            self._ws = await self._connect(url, open_timeout=10, close_timeout=2, max_size=1024 * 1024, max_queue=16)
            self._finished = False
            self.connected = True
            self._receiver = asyncio.create_task(self._receive(), name="gladia-receive")
        except ProviderError:
            raise
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderError("GLADIA_TIMEOUT", "文字起こしサービスへの接続がタイムアウトしました。", True) from None
        except (ValueError, KeyError, TypeError):
            raise ProviderError("GLADIA_INVALID_RESPONSE", "文字起こしサービスの応答を確認できませんでした。") from None
        except Exception:
            # websockets exceptions may embed its credential-bearing URL.
            raise ProviderError("GLADIA_UNAVAILABLE", "文字起こしサービスに接続できません。", True) from None

    def _utterance_id(self, data: dict, utterance: dict) -> str:
        # Timestamp + channel is more stable than assuming each partial result's
        # external message ID stays fixed. Retain a bounded map for late finals.
        start = utterance.get("start")
        external_id = data.get("id")
        if isinstance(start, (int, float)):
            key = ("start", utterance.get("channel", 0), round(start, 4))
        elif isinstance(external_id, str) and external_id:
            key = ("external", external_id)
        else:
            key = None
        if key in self._ids:
            self._ids.move_to_end(key)
            return self._ids[key]
        identity = self._active_id or "utt_" + uuid4().hex
        if key is not None:
            # Different known starts are distinct utterances even if a provider
            # omitted the final boundary for the preceding utterance.
            if key[0] == "start":
                identity = "utt_" + uuid4().hex
            self._ids[key] = identity
            if len(self._ids) > 128:
                self._ids.popitem(last=False)
        return identity

    async def _receive(self) -> None:
        try:
            async for message in self._ws:
                if not isinstance(message, str):
                    continue
                payload = json.loads(message)
                if not isinstance(payload, dict):
                    raise ValueError("Invalid event")
                kind = payload.get("type")
                if kind == "error" or payload.get("error"):
                    raise ProviderError("GLADIA_STREAM_ERROR", "文字起こしサービスが音声を処理できませんでした。", True)
                if kind == "end_session":
                    # end_recording can precede final transcripts: only
                    # end_session / socket completion ends the drain.
                    break
                if kind != "transcript":
                    continue
                data = payload.get("data", {})
                utterance = data.get("utterance", {})
                text = utterance.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    continue
                if len(text) > 16000:
                    raise ProviderError("GLADIA_INVALID_RESPONSE", "文字起こしの応答が大きすぎます。")
                identity = self._utterance_id(data, utterance)
                final = data.get("is_final") is True
                self._active_id = None if final else identity
                self._buffer.put(STTEvent(text.strip(), final, identity))
                if self._buffer.done:
                    break
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            self._buffer.finish(exc)
        except (ValueError, TypeError, AttributeError):
            self._buffer.finish(ProviderError("GLADIA_INVALID_RESPONSE", "文字起こしの応答を確認できませんでした。"))
        except Exception:
            if not self._closing and not self._finished:
                self._buffer.finish(ProviderError("GLADIA_DISCONNECTED", "文字起こしサービスとの接続が切れました。", True))
        finally:
            was_active = self.connected and not self._closing and not self._finished
            self.connected = False
            self._buffer.finish(ProviderError("GLADIA_DISCONNECTED", "文字起こしサービスとの接続が終了しました。", True) if was_active else None)

    async def send_audio(self, pcm: bytes) -> None:
        if not self.connected or self._finished:
            raise ProviderError("STT_NOT_READY", "文字起こしの再接続が必要です。", True)
        validate_pcm(pcm)
        try:
            await asyncio.wait_for(self._ws.send(pcm), timeout=2.0)
        except Exception:
            raise ProviderError("GLADIA_SEND_FAILED", "文字起こしサービスへ音声を送信できませんでした。", True) from None

    def events(self):
        return self._buffer.events()

    async def finish_input(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._ws and self.connected:
            try:
                await asyncio.wait_for(self._ws.send(json.dumps({"type": "stop_recording"})), timeout=2.0)
                if self._receiver:
                    await asyncio.wait_for(asyncio.shield(self._receiver), timeout=self._drain_timeout)
            except TimeoutError:
                self._buffer.finish(ProviderError("GLADIA_DRAIN_TIMEOUT", "最後の文字起こしの待機時間を超えました。", True))
            except Exception:
                self._buffer.finish(ProviderError("GLADIA_DISCONNECTED", "最後の文字起こしを受信できませんでした。", True))
        self._buffer.finish()

    async def close(self) -> None:
        self._closing = True
        self._finished = True
        self.connected = False
        # Cancel the owned task before waiting on a possibly unresponsive socket;
        # an outer runtime close deadline must not strand the receive task.
        if self._receiver:
            if not self._receiver.done():
                self._receiver.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._receiver
            self._receiver = None
        self._buffer.finish()
        if self._ws:
            socket, self._ws = self._ws, None
            with suppress(Exception):
                await asyncio.wait_for(socket.close(), timeout=1.0)
