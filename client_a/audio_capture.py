import asyncio
import os
import threading
import numpy as np
import soundcard as sc
import websockets
from dotenv import load_dotenv

load_dotenv()

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = int(SAMPLE_RATE * 0.1)
SERVER_URL = os.getenv("SERVER_URL", "ws://localhost:8000") + "/audio/from_A"


async def run():
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    speaker = sc.default_speaker()
    loopback = sc.get_microphone(speaker.id, include_loopback=True)
    print(f"Loopback device: {loopback.name}")

    def record_thread():
        with loopback.recorder(samplerate=SAMPLE_RATE, channels=2) as rec:
            print("Loopback capture active — press Ctrl+C to stop")
            while True:
                data = rec.record(numframes=CHUNK_SIZE)
                mono = data.mean(axis=1)
                pcm = (mono * 32767).clip(-32768, 32767).astype("int16").tobytes()
                loop.call_soon_threadsafe(queue.put_nowait, pcm)

    t = threading.Thread(target=record_thread, daemon=True)
    t.start()

    async with websockets.connect(SERVER_URL) as ws:
        print(f"Connected to {SERVER_URL}")
        while True:
            data = await queue.get()
            await ws.send(data)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Stopped")
