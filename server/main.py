import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketDisconnect
from shared.languages import SUPPORTED_LANGUAGES
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


class SessionState:
    def __init__(self):
        self.source_language = "zh"
        self.target_language = "ja"
        self.translation_enabled = True
        self.utterance_id = uuid.uuid4().hex[:8]

    def new_utterance(self):
        self.utterance_id = uuid.uuid4().hex[:8]


_stt_name = os.getenv("STT_PROVIDER", "dummy").lower()
_translation_name = os.getenv("TRANSLATION_PROVIDER", "dummy").lower()

a_control_websockets: set[WebSocket] = set()
b_control_websockets: set[WebSocket] = set()


async def notify_stt_disconnected():
    payload = {"type": "stt_status", "value": "disconnected"}
    for ws in list(b_control_websockets):
        try:
            await ws.send_json(payload)
        except Exception:
            pass


stt: STTProvider = (
    GladiaSTTProvider(
        os.getenv("GLADIA_API_KEY", ""),
        on_disconnect=notify_stt_disconnected,
    )
    if _stt_name == "gladia"
    else DummySTTProvider()
)

translator: TranslationProvider = (
    DeepLTranslationProvider(os.getenv("DEEPL_API_KEY", ""))
    if _translation_name == "deepl"
    else DummyTranslationProvider()
)

session_state = SessionState()
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
app.mount("/b", StaticFiles(directory=BASE_DIR / "web_b", html=True), name="web_b")


async def broadcast_caption(payload: dict):
    dead = set()
    for ws in caption_subscribers:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    for d in dead:
        caption_subscribers.discard(d)


async def broadcast_a_state():
    payload = {
        "type": "state",
        "source_language": session_state.source_language,
        "target_language": session_state.target_language,
        "translation_enabled": session_state.translation_enabled,
    }
    for ws in list(a_control_websockets):
        try:
            await ws.send_json(payload)
        except Exception:
            pass


async def broadcast_b_state():
    payload = {
        "type": "state",
        "language": session_state.source_language,
        "target_language": session_state.target_language,
        "translation_enabled": session_state.translation_enabled,
        "stt_connected": (not isinstance(stt, GladiaSTTProvider)) or stt.connected,
        "supported_languages": SUPPORTED_LANGUAGES,
    }
    for ws in list(b_control_websockets):
        try:
            await ws.send_json(payload)
        except Exception:
            pass


async def translate_and_broadcast(
    utt_id: str, text: str, is_final: bool, source: str, target: str
):
    translated = await translator.translate(text, source, target)
    await broadcast_caption({
        "kind": "translated",
        "utterance_id": utt_id,
        "text": translated,
        "is_final": is_final,
    })


async def change_source_language(language: str):
    session_state.source_language = language
    if isinstance(stt, GladiaSTTProvider):
        await stt.reconfigure(language)
    logger.info(f"Source language changed to {language}")


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
                text = result["text"]
                is_final = result["is_final"]
                utt_id = session_state.utterance_id
                logger.info(f"[STT] text={text!r} is_final={is_final}")

                await broadcast_caption({
                    "kind": "original",
                    "utterance_id": utt_id,
                    "text": text,
                    "is_final": is_final,
                    "language": session_state.source_language,
                })

                should_translate = (
                    session_state.translation_enabled
                    and session_state.source_language != session_state.target_language
                )
                if should_translate:
                    asyncio.create_task(
                        translate_and_broadcast(
                            utt_id, text, is_final,
                            session_state.source_language,
                            session_state.target_language,
                        )
                    )

                if is_final:
                    session_state.new_utterance()
    except WebSocketDisconnect:
        logger.info(f"B disconnected  [/audio/from_B]  total_chunks={chunk_count}")


@app.websocket("/captions/to_A")
async def captions_to_a(websocket: WebSocket):
    await websocket.accept()
    caption_subscribers.add(websocket)
    logger.info(f"subscriber connected  [/captions/to_A]  total={len(caption_subscribers)}")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        caption_subscribers.discard(websocket)
        logger.info(f"subscriber disconnected  [/captions/to_A]  total={len(caption_subscribers)}")


@app.websocket("/control/A")
async def control_a(websocket: WebSocket):
    await websocket.accept()
    a_control_websockets.add(websocket)
    logger.info("A connected  [/control/A]")
    await websocket.send_json({
        "type": "state",
        "source_language": session_state.source_language,
        "target_language": session_state.target_language,
        "translation_enabled": session_state.translation_enabled,
        "supported_languages": SUPPORTED_LANGUAGES,
    })
    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type", "")
            if msg_type == "set_target_language":
                session_state.target_language = msg["value"]
                await broadcast_a_state()
                await broadcast_b_state()
            elif msg_type == "set_translation":
                session_state.translation_enabled = bool(msg["value"])
                await broadcast_a_state()
                await broadcast_b_state()
            elif msg_type == "get_state":
                await websocket.send_json({
                    "type": "state",
                    "source_language": session_state.source_language,
                    "target_language": session_state.target_language,
                    "translation_enabled": session_state.translation_enabled,
                    "supported_languages": SUPPORTED_LANGUAGES,
                })
    except WebSocketDisconnect:
        logger.info("A disconnected  [/control/A]")
    finally:
        a_control_websockets.discard(websocket)


@app.websocket("/control/B")
async def control_b(websocket: WebSocket):
    await websocket.accept()
    b_control_websockets.add(websocket)
    logger.info("B connected  [/control/B]")
    stt_connected = (not isinstance(stt, GladiaSTTProvider)) or stt.connected
    await websocket.send_json({
        "type": "state",
        "language": session_state.source_language,
        "target_language": session_state.target_language,
        "translation_enabled": session_state.translation_enabled,
        "stt_connected": stt_connected,
        "supported_languages": SUPPORTED_LANGUAGES,
    })
    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type", "")
            if msg_type == "set_language":
                await change_source_language(msg["value"])
                await broadcast_b_state()
                await broadcast_a_state()
            elif msg_type == "get_state":
                stt_connected = (not isinstance(stt, GladiaSTTProvider)) or stt.connected
                await websocket.send_json({
                    "type": "state",
                    "language": session_state.source_language,
                    "target_language": session_state.target_language,
                    "translation_enabled": session_state.translation_enabled,
                    "stt_connected": stt_connected,
                    "supported_languages": SUPPORTED_LANGUAGES,
                })
            elif msg_type == "restart":
                if isinstance(stt, GladiaSTTProvider):
                    try:
                        await stt.restart()
                        await websocket.send_json({"type": "stt_status", "value": "connected"})
                    except Exception as e:
                        await websocket.send_json({"type": "stt_status", "value": "error", "message": str(e)})
    except WebSocketDisconnect:
        logger.info("B disconnected  [/control/B]")
    except Exception as e:
        logger.error(f"control_b error: {e}")
    finally:
        b_control_websockets.discard(websocket)
