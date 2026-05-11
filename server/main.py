import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketDisconnect
from shared.stt_provider import DummySTTProvider, GladiaSTTProvider, STTProvider
from shared.translation_provider import (
    DeepLTranslationProvider,
    DummyTranslationProvider,
    TranslationProvider,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
MAX_B_QUEUE = 10

_stt_name = os.getenv("STT_PROVIDER", "dummy").lower()
_translation_name = os.getenv("TRANSLATION_PROVIDER", "dummy").lower()

stt: STTProvider = (
    GladiaSTTProvider(os.getenv("GLADIA_API_KEY", ""))
    if _stt_name == "gladia"
    else DummySTTProvider()
)

translator: TranslationProvider = (
    DeepLTranslationProvider(os.getenv("DEEPL_API_KEY", ""))
    if _translation_name == "deepl"
    else DummyTranslationProvider()
)

caption_subscribers: set[WebSocket] = set()
b_audio_queues: set[asyncio.Queue] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await stt.start()
    except Exception as e:
        logger.error(f"STT provider start failed: {e}")
    logger.info(f"STT: {type(stt).__name__}  Translation: {type(translator).__name__}")
    yield
    await stt.close()
    await translator.close()


app = FastAPI(lifespan=lifespan)
app.mount("/a", StaticFiles(directory=BASE_DIR / "web_a", html=True), name="web_a")


async def broadcast_caption(payload: dict):
    dead = set()
    for ws in caption_subscribers:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    for d in dead:
        caption_subscribers.discard(d)


@app.websocket("/audio/from_A")
async def audio_from_a(websocket: WebSocket):
    await websocket.accept()
    logger.info("A connected  [/audio/from_A]")
    chunk_count = 0
    try:
        while True:
            data = await websocket.receive_bytes()
            chunk_count += 1
            for q in b_audio_queues:
                if q.qsize() >= MAX_B_QUEUE:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(data)
            if chunk_count % 50 == 0:
                logger.info(
                    f"[from_A] chunks={chunk_count}  b_subscribers={len(b_audio_queues)}"
                )
    except WebSocketDisconnect:
        logger.info(f"A disconnected  [/audio/from_A]  total_chunks={chunk_count}")


@app.websocket("/audio/to_B")
async def audio_to_b(websocket: WebSocket):
    await websocket.accept()
    q: asyncio.Queue[bytes] = asyncio.Queue()
    b_audio_queues.add(q)
    logger.info(f"B connected  [/audio/to_B]  subscribers={len(b_audio_queues)}")

    async def send_loop():
        while True:
            data = await q.get()
            await websocket.send_bytes(data)

    async def recv_loop():
        while True:
            await websocket.receive_bytes()

    send_task = asyncio.create_task(send_loop())
    recv_task = asyncio.create_task(recv_loop())
    try:
        done, _ = await asyncio.wait(
            {send_task, recv_task}, return_when=asyncio.FIRST_EXCEPTION
        )
        for t in done:
            try:
                t.result()
            except Exception:
                pass
    finally:
        send_task.cancel()
        recv_task.cancel()
        b_audio_queues.discard(q)
        logger.info(f"B disconnected  [/audio/to_B]  subscribers={len(b_audio_queues)}")


@app.websocket("/audio/from_B")
async def audio_from_b(websocket: WebSocket):
    await websocket.accept()
    logger.info("B connected  [/audio/from_B]")
    chunk_count = 0
    try:
        while True:
            data = await websocket.receive_bytes()
            chunk_count += 1
            result = await stt.transcribe(data)
            if result:
                logger.info(f"[STT] {result}")
                original = result["text"]
                translated = await translator.translate(original, "zh", "ja")
                payload = {
                    "original": original,
                    "translated": translated,
                    "is_final": result["is_final"],
                }
                logger.info(f"[caption] {payload}")
                await broadcast_caption(payload)
    except WebSocketDisconnect:
        logger.info(f"B disconnected  [/audio/from_B]  total_chunks={chunk_count}")


@app.websocket("/captions/to_A")
async def captions_to_a(websocket: WebSocket):
    await websocket.accept()
    caption_subscribers.add(websocket)
    logger.info(f"A connected  [/captions/to_A]  subscribers={len(caption_subscribers)}")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        caption_subscribers.discard(websocket)
        logger.info(f"A disconnected  [/captions/to_A]  subscribers={len(caption_subscribers)}")
