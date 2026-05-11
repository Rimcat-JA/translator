import asyncio
import logging
from fastapi import FastAPI, WebSocket
from fastapi.websockets import WebSocketDisconnect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.websocket("/audio/from_A")
async def audio_from_a(websocket: WebSocket):
    await websocket.accept()
    logger.info("A connected  [/audio/from_A]")
    try:
        while True:
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        logger.info("A disconnected  [/audio/from_A]")


@app.websocket("/audio/to_B")
async def audio_to_b(websocket: WebSocket):
    await websocket.accept()
    logger.info("B connected  [/audio/to_B]")
    try:
        while True:
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info("B disconnected  [/audio/to_B]")


@app.websocket("/audio/from_B")
async def audio_from_b(websocket: WebSocket):
    await websocket.accept()
    logger.info("B connected  [/audio/from_B]")
    chunk_count = 0
    try:
        while True:
            data = await websocket.receive_bytes()
            chunk_count += 1
            logger.info(f"[from_B] chunk #{chunk_count:05d}  {len(data):,} bytes")
    except WebSocketDisconnect:
        logger.info(f"B disconnected  [/audio/from_B]  total_chunks={chunk_count}")


@app.websocket("/captions/to_A")
async def captions_to_a(websocket: WebSocket):
    await websocket.accept()
    logger.info("A connected  [/captions/to_A]")
    try:
        while True:
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info("A disconnected  [/captions/to_A]")
