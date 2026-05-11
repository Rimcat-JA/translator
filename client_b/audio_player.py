import asyncio
import os
import queue
import threading
import numpy as np
import sounddevice as sd
import websockets
from dotenv import load_dotenv

load_dotenv()

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = int(SAMPLE_RATE * 0.1)
MAX_QUEUE = 5
SERVER_URL = os.getenv("SERVER_URL", "ws://localhost:8000") + "/audio/to_B"


def playback_thread(audio_queue: queue.Queue):
    with sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16") as stream:
        while True:
            chunk = audio_queue.get()
            if chunk is None:
                break
            stream.write(chunk)


async def run():
    audio_queue: queue.Queue = queue.Queue()

    thread = threading.Thread(target=playback_thread, args=(audio_queue,), daemon=True)
    thread.start()

    async with websockets.connect(SERVER_URL) as ws:
        print(f"Connected to {SERVER_URL}")
        print("Playing audio from A — press Ctrl+C to stop")
        while True:
            data = await ws.recv()
            chunk = np.frombuffer(data, dtype="int16")
            if audio_queue.qsize() >= MAX_QUEUE:
                try:
                    audio_queue.get_nowait()
                except queue.Empty:
                    pass
            audio_queue.put_nowait(chunk)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Stopped")
