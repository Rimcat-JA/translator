"""Network-free contracts shared by the runtime and provider adapters."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Literal, Protocol


class ProviderError(Exception):
    """An intentionally safe public error; never include response bodies or URLs."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class STTConfig:
    language: str = "zh"
    sample_rate: int = 16000
    channels: int = 1


@dataclass(frozen=True)
class STTEvent:
    text: str
    is_final: bool
    utterance_id: str


@dataclass(frozen=True)
class TranslationResult:
    text: str = ""
    status: Literal["ready", "same_language", "disabled", "error"] = "ready"
    error_code: str | None = None


class STTProvider(Protocol):
    async def start(self, config: STTConfig) -> None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    def events(self) -> AsyncIterator[STTEvent]: ...
    async def finish_input(self) -> None: ...
    async def close(self) -> None: ...


class TranslationProvider(Protocol):
    async def translate(self, text: str, source: str, target: str) -> TranslationResult: ...
    async def close(self) -> None: ...


class EventBuffer:
    """Bounded receive buffer: coalesce partials, preserve finals, report saturation.

    Completion is out of band so stop can never hang trying to enqueue a sentinel.
    A slow consumer that fills the buffer with finals receives an explicit error.
    """

    def __init__(self, limit: int = 128):
        self.items: deque[STTEvent] = deque()
        self.limit = limit
        self.done = False
        self.error: ProviderError | None = None
        self.changed = asyncio.Event()

    def put(self, event: STTEvent) -> None:
        if self.done:
            return
        self.items = deque(item for item in self.items if not (
            item.utterance_id == event.utterance_id and not item.is_final
        ))
        if len(self.items) >= self.limit:
            partial = next((i for i, item in enumerate(self.items) if not item.is_final), None)
            if partial is not None:
                del self.items[partial]
            elif not event.is_final:
                return
            else:
                self.finish(ProviderError("STT_BACKPRESSURE", "字幕の受信が混雑しています。再接続してください。", True))
                return
        self.items.append(event)
        self.changed.set()

    def finish(self, error: ProviderError | None = None) -> None:
        self.done = True
        self.error = self.error or error
        self.changed.set()

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            if self.items:
                yield self.items.popleft()
            elif self.done:
                if self.error:
                    raise self.error
                return
            else:
                self.changed.clear()
                await self.changed.wait()


def validate_pcm(pcm: bytes) -> None:
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 3200:
        raise ProviderError("INVALID_AUDIO", "音声は16 kHz・mono PCMの100 ms以下のフレームが必要です。")


def http_error(provider: str, status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError(f"{provider}_AUTH_FAILED", "APIキーとFree / Proの設定を確認してください。")
    if status == 456:
        return ProviderError(f"{provider}_QUOTA", "サービスの利用上限に達しました。")
    if status == 429:
        return ProviderError(f"{provider}_RATE_LIMIT", "サービスの要求回数制限に達しました。", True)
    if status >= 500:
        return ProviderError(f"{provider}_UNAVAILABLE", "サービスで一時的な障害が発生しています。", True)
    return ProviderError(f"{provider}_REQUEST_REJECTED", "サービスが設定または要求を受け付けませんでした。")
