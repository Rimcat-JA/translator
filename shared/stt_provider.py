import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Optional
import httpx
import websockets

logger = logging.getLogger(__name__)


class STTProvider(ABC):
    async def start(self):
        pass

    async def close(self):
        pass

    @abstractmethod
    async def transcribe(self, audio_chunk: bytes) -> Optional[dict]:
        pass


class DummySTTProvider(STTProvider):
    _PHRASES = ["你好世界", "这是一个测试", "欢迎使用翻译系统", "今天天气很好"]
    _CHUNKS_PER_EMIT = 30

    def __init__(self):
        self._chunk_count = 0
        self._phrase_index = 0
        self._emit_count = 0

    async def transcribe(self, audio_chunk: bytes) -> Optional[dict]:
        self._chunk_count += 1
        if self._chunk_count % self._CHUNKS_PER_EMIT != 0:
            return None
        text = self._PHRASES[self._phrase_index % len(self._PHRASES)]
        if self._emit_count < 2:
            self._emit_count += 1
            return {"text": text, "is_final": False}
        else:
            self._emit_count = 0
            self._phrase_index += 1
            return {"text": text, "is_final": True}


class GladiaSTTProvider(STTProvider):
    _INIT_URL = "https://api.gladia.io/v2/live"

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._ws = None
        self._result_queue: asyncio.Queue = asyncio.Queue()
        self._recv_task: Optional[asyncio.Task] = None

    async def start(self):
        await self._connect()

    async def _connect(self):
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
        if self._ws and self._ws.close_code is None:
            await self._ws.close()

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                self._INIT_URL,
                headers={
                    "x-gladia-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "encoding": "wav/pcm",
                    "sample_rate": 16000,
                    "bit_depth": 16,
                    "channels": 1,
                    "language_config": {
                        "languages": ["zh"],
                        "code_switching": False,
                    },
                    "messages_config": {"receive_partial_transcripts": True},
                },
            )
            resp.raise_for_status()
            ws_url = resp.json()["url"]

        self._ws = await websockets.connect(ws_url)
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info(f"Gladia session started")

    async def _recv_loop(self):
        try:
            async for message in self._ws:
                if not isinstance(message, str):
                    continue
                data = json.loads(message)
                if data.get("type") != "transcript":
                    continue
                utterance = data["data"].get("utterance", {})
                text = utterance.get("text", "").strip()
                if not text:
                    continue
                await self._result_queue.put({
                    "text": text,
                    "is_final": data["data"].get("is_final", False),
                })
        except Exception as e:
            logger.error(f"Gladia recv error: {e}")

    async def transcribe(self, audio_chunk: bytes) -> Optional[dict]:
        if self._ws is None or self._ws.close_code is not None:
            logger.warning("Gladia WS closed, reconnecting...")
            try:
                await self._connect()
            except Exception:
                return None

        try:
            await self._ws.send(audio_chunk)
        except Exception as e:
            logger.error(f"Gladia send error: {e}")
            return None

        try:
            return self._result_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def close(self):
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send(json.dumps({"type": "stop_recording"}))
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task:
            self._recv_task.cancel()
