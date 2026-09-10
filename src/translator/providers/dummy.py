"""Deterministic local demo providers; elapsed audio samples drive the script."""
from __future__ import annotations

from uuid import uuid4

from .base import EventBuffer, ProviderError, STTConfig, STTEvent, TranslationResult, validate_pcm

PHRASES = {
    "zh": ["你好，欢迎来到我们的对话。", "这是完全在本机运行的演示。", "说话的内容会在这里显示。"],
    "en": ["Hello, welcome to our conversation.", "This demo runs entirely on your computer.", "Your words will appear here as captions."],
    "ja": ["こんにちは。会話へようこそ。", "これはパソコン内だけで動くデモです。", "話した内容がここに字幕で表示されます。"],
}


class DummySTTProvider:
    def __init__(self):
        self._buffer = EventBuffer()
        self.connected = False
        self._finished = True

    async def start(self, config: STTConfig = STTConfig()) -> None:
        await self.close()
        self._buffer = EventBuffer()
        self._phrases = PHRASES.get(config.language, PHRASES["en"])
        self._prefix = uuid4().hex
        self._samples = 0
        self._next_emit = 16000
        self._emit = 0
        self.connected = True
        self._finished = False

    def _event(self, final: bool = False) -> STTEvent:
        phrase_index, phase = divmod(self._emit, 3)
        phrase = self._phrases[phrase_index % len(self._phrases)]
        is_final = final or phase == 2
        length = len(phrase) if is_final else max(1, len(phrase) * (phase + 1) // 3)
        return STTEvent(phrase[:length], is_final, f"demo_{self._prefix}_{phrase_index}")

    async def send_audio(self, pcm: bytes) -> None:
        if not self.connected or self._finished:
            raise ProviderError("STT_NOT_READY", "文字起こしを開始してください。")
        validate_pcm(pcm)
        self._samples += len(pcm) // 2
        while self._samples >= self._next_emit:
            self._buffer.put(self._event())
            self._emit += 1
            self._next_emit += 16000

    def events(self):
        return self._buffer.events()

    async def finish_input(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._samples % 48000:
            self._buffer.put(self._event(final=True))
        self._buffer.finish()

    async def close(self) -> None:
        self.connected = False
        self._finished = True
        self._buffer.finish()


class DummyTranslationProvider:
    async def translate(self, text: str, source: str, target: str) -> TranslationResult:
        if source == target:
            return TranslationResult(status="same_language")
        source_phrases = PHRASES.get(source, PHRASES["en"])
        target_phrases = PHRASES.get(target, PHRASES["en"])
        for i, phrase in enumerate(source_phrases):
            if phrase.startswith(text):
                result = target_phrases[i]
                if text != phrase:
                    result = result[:max(1, len(result) * len(text) // len(phrase))]
                return TranslationResult(result)
        return TranslationResult(f"［デモ訳］{text}")

    async def close(self) -> None:
        pass
